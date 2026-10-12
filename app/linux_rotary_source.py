from __future__ import annotations

import os
import platform
import selectors
import struct
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from .tracking import degrees_per_count


EV_SYN = 0
EV_REL = 2
SYN_REPORT = 0
SYN_DROPPED = 3
REL_CODES = {"REL_X": 0, "REL_Y": 1}
INPUT_EVENT_STRUCT = struct.Struct("@llHHi")
READ_EVENT_COUNT = 1024
# _IOW('E', 0xa0, int), from linux/input.h. The ioctl is issued only from
# the Linux acquisition thread, keeping this module import-safe on Windows.
EVIOCSCLOCKID = 0x400445A0


class LinuxRotaryUnavailable(RuntimeError):
    pass


def _configure_event_clock(fd: int, clock_id: int | None = None) -> None:
    import fcntl

    selected = time.CLOCK_MONOTONIC if clock_id is None else int(clock_id)
    fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", selected))


def _relative_capabilities(path: Path) -> int:
    value = path.read_text(encoding="ascii").strip().replace(" ", "")
    return int(value or "0", 16)


def discover_rotary_device(
    rel_axis: str,
    device_override: str = "",
    sysfs_root: Path = Path("/sys/class/input"),
    dev_root: Path = Path("/dev/input"),
) -> tuple[str, str]:
    """Resolve one rotary-encoder device by stable name and EV_REL capability."""
    if rel_axis not in REL_CODES:
        raise LinuxRotaryUnavailable(f"Unsupported Linux relative axis: {rel_axis}")
    wanted_code = REL_CODES[rel_axis]
    override_name = Path(device_override).name if device_override else ""
    matches: list[tuple[str, str]] = []
    for event_dir in sorted(sysfs_root.glob("event*")):
        if override_name and event_dir.name != override_name:
            continue
        try:
            name = (event_dir / "device" / "name").read_text(encoding="utf-8").strip()
            capabilities = _relative_capabilities(event_dir / "device" / "capabilities" / "rel")
        except (FileNotFoundError, OSError, ValueError):
            continue
        normalised_name = name.lower().replace("_", "-")
        # The upstream driver uses the DT platform-device name (for the
        # Raspberry Pi overlay this is typically rotary@<pin>), not a fixed
        # human-readable "rotary-encoder" input name.
        if "rotary" not in normalised_name:
            continue
        if not capabilities & (1 << wanted_code):
            continue
        device = dev_root / event_dir.name
        matches.append((str(device), name))
    if not matches:
        suffix = f" at {device_override}" if device_override else ""
        raise LinuxRotaryUnavailable(
            f"No rotary-encoder input device exposing {rel_axis}{suffix}; enable the Raspberry Pi overlay and reboot"
        )
    if len(matches) != 1:
        devices = ", ".join(device for device, _ in matches)
        raise LinuxRotaryUnavailable(f"Ambiguous rotary-encoder devices for {rel_axis}: {devices}")
    return matches[0]


class LinuxRotaryWorker:
    """Linux evdev adapter for a kernel-decoded quarter-period rotary encoder."""

    RATE_WINDOW_NS = 1_000_000_000
    RATE_BUCKET_NS = 10_000_000
    RATE_BUCKET_CAPACITY = RATE_WINDOW_NS // RATE_BUCKET_NS + 2
    PEAK_MIN_WINDOW_NS = 100_000_000
    PEAK_MIN_EVENTS = 16

    def __init__(
        self,
        axis: str,
        config: dict[str, Any],
        sysfs_root: Path = Path("/sys/class/input"),
        dev_root: Path = Path("/dev/input"),
    ) -> None:
        self.axis = axis
        self.config = deepcopy(config)
        self._sysfs_root = Path(sysfs_root)
        self._dev_root = Path(dev_root)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._pending_bytes = b""
        self._dropping = False
        self._motion_buckets: deque[list[int]] = deque(maxlen=self.RATE_BUCKET_CAPACITY)
        source = self.config["source_config"]
        self._rel_axis = str(source["rel_axis"])
        self._rel_code = REL_CODES[self._rel_axis]
        self._snapshot: dict[str, Any] = {
            "axis": axis, "driver_alive": False, "configured": True, "raw_count": 0,
            "referenced": False, "reference_count": None, "reference_angle": 0.0, "reference_id": None,
            "health": "REFERENCE_REQUIRED", "status": "Waiting for Linux rotary-encoder device", "last_error": None,
            "last_event_timestamp": None, "last_event_timestamp_ns": None,
            "events": 0, "event_rate_hz": 0.0, "peak_event_rate_hz": 0.0,
            "integrity_lost": False, "integrity_status": "TRUSTED", "integrity_loss_events": 0,
            "restarts": 0, "device_identity": None, "resolved_event_device": None,
            "rel_axis": self._rel_axis, "rel_code": self._rel_code, "kernel_driver": "rotary-encoder",
            "steps_per_period": 4, "evdev_dropped_events": 0, "read_event_count": READ_EVENT_COUNT,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"linux-rotary-{self.axis}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._close()

    def begin_acquisition(self, device: str, identity: str) -> None:
        with self._lock:
            self._pending_bytes = b""
            self._dropping = False
            self._motion_buckets.clear()
            self._snapshot.update({
                "driver_alive": True, "configured": True, "health": "REFERENCE_REQUIRED",
                "status": "Set an encoder reference", "last_error": None,
                "device_identity": identity, "resolved_event_device": device,
            })
            self._invalidate_reference_locked("Set an encoder reference")

    def set_reference(self, angle: float) -> str:
        with self._lock:
            if not self._snapshot["driver_alive"]:
                raise ValueError("Linux rotary driver must be healthy before setting a reference")
            reference_id = str(uuid.uuid4())
            self._snapshot.update({
                "referenced": True, "reference_count": self._snapshot["raw_count"],
                "reference_angle": float(angle), "reference_id": reference_id,
                "integrity_lost": False, "integrity_status": "TRUSTED",
                "health": "VALID", "status": "Referenced",
            })
            return reference_id

    def clear_reference(self) -> None:
        with self._lock:
            self._invalidate_reference_locked("Set an encoder reference")

    def _invalidate_reference_locked(self, status: str, integrity_loss: bool = False) -> None:
        already_lost = self._snapshot["integrity_lost"]
        self._snapshot.update({
            "referenced": False, "reference_count": None, "reference_angle": 0.0, "reference_id": None,
            "health": "REFERENCE_REQUIRED" if self._snapshot["driver_alive"] else self._snapshot["health"],
            "status": status,
        })
        if integrity_loss:
            self._snapshot.update({"integrity_lost": True, "integrity_status": "LOST — REFERENCE REQUIRED"})
            if not already_lost:
                self._snapshot["integrity_loss_events"] += 1

    def _record_motion_locked(self, timestamp_ns: int) -> None:
        bucket_id = timestamp_ns // self.RATE_BUCKET_NS
        count = self._snapshot["raw_count"]
        if self._motion_buckets and self._motion_buckets[-1][0] == bucket_id:
            bucket = self._motion_buckets[-1]
            bucket[2] = timestamp_ns
            bucket[3] += 1
            bucket[5] = count
        else:
            self._motion_buckets.append([bucket_id, timestamp_ns, timestamp_ns, 1, count, count])

    def _rates_locked(self, now_ns: int) -> tuple[float, float, int, int]:
        cutoff = now_ns - self.RATE_WINDOW_NS
        while len(self._motion_buckets) > 1 and self._motion_buckets[1][2] < cutoff:
            self._motion_buckets.popleft()
        last_event = self._snapshot["last_event_timestamp_ns"]
        if last_event is None or now_ns - last_event > self.RATE_WINDOW_NS or not self._motion_buckets:
            return 0.0, 0.0, 0, 0
        first, last = self._motion_buckets[0], self._motion_buckets[-1]
        event_count = sum(bucket[3] for bucket in self._motion_buckets)
        span_ns = last[2] - first[1]
        if span_ns <= 0:
            return 0.0, 0.0, event_count, span_ns
        span = span_ns / 1_000_000_000
        return (event_count - 1) / span, (last[5] - first[4]) / span, event_count, span_ns

    def _update_peak_locked(self, timestamp_ns: int) -> None:
        event_rate, _, event_count, span_ns = self._rates_locked(timestamp_ns)
        if event_count >= self.PEAK_MIN_EVENTS and span_ns >= self.PEAK_MIN_WINDOW_NS:
            self._snapshot["peak_event_rate_hz"] = max(self._snapshot["peak_event_rate_hz"], event_rate)

    def snapshot(self, now_ns: int | None = None) -> dict[str, Any]:
        now_ns = time.monotonic_ns() if now_ns is None else int(now_ns)
        with self._lock:
            event_rate, count_rate, _, _ = self._rates_locked(now_ns)
            result = deepcopy(self._snapshot)
            result.update({
                "event_rate_hz": round(event_rate, 2),
                "peak_event_rate_hz": round(self._snapshot["peak_event_rate_hz"], 2),
                "counts_per_second": round(count_rate, 2),
                "degrees_per_second": round(count_rate * degrees_per_count(self.config), 3),
                "last_event_age_ms": round(max(0, now_ns - result["last_event_timestamp_ns"]) / 1_000_000, 3)
                if result["last_event_timestamp_ns"] is not None else None,
            })
            return result

    def process_input_events(self, events: Iterable[tuple[int, int, int, int]]) -> None:
        """Process ordered ``(timestamp_ns, type, code, value)`` evdev records."""
        with self._lock:
            last_timestamp: int | None = None
            for timestamp_ns, event_type, code, value in events:
                timestamp_ns = int(timestamp_ns)
                if event_type == EV_SYN and code == SYN_DROPPED:
                    self._snapshot["evdev_dropped_events"] += 1
                    self._dropping = True
                    self._snapshot["last_event_timestamp"] = timestamp_ns
                    self._snapshot["last_event_timestamp_ns"] = timestamp_ns
                    self._invalidate_reference_locked(
                        "Evdev queue overrun (SYN_DROPPED); set a new reference", integrity_loss=True,
                    )
                    continue
                if self._dropping:
                    if event_type == EV_SYN and code == SYN_REPORT:
                        self._dropping = False
                    continue
                if event_type != EV_REL or code != self._rel_code:
                    continue
                self._snapshot["raw_count"] += int(value)
                self._snapshot["events"] += 1
                self._snapshot["last_event_timestamp"] = timestamp_ns
                self._snapshot["last_event_timestamp_ns"] = timestamp_ns
                self._record_motion_locked(timestamp_ns)
                last_timestamp = timestamp_ns
            if last_timestamp is not None:
                self._update_peak_locked(last_timestamp)

    def process_input_bytes(self, data: bytes) -> None:
        self._pending_bytes += data
        size = INPUT_EVENT_STRUCT.size
        complete = len(self._pending_bytes) // size * size
        if not complete:
            return
        records = []
        for offset in range(0, complete, size):
            seconds, microseconds, event_type, code, value = INPUT_EVENT_STRUCT.unpack_from(self._pending_bytes, offset)
            records.append((seconds * 1_000_000_000 + microseconds * 1_000, event_type, code, value))
        self._pending_bytes = self._pending_bytes[complete:]
        self.process_input_events(records)

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._snapshot.update({
                "driver_alive": False, "health": "ERROR", "status": message, "last_error": message,
                "resolved_event_device": None,
            })
            self._invalidate_reference_locked(message)

    def _close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _run(self) -> None:
        backoff = 0.25
        while not self._stop.is_set():
            try:
                self._open_and_consume()
                backoff = 0.25
            except Exception as exc:
                self._set_error(str(exc))
                self._close()
                if self._stop.wait(backoff):
                    break
                backoff = min(5.0, backoff * 2.0)
                with self._lock:
                    self._snapshot["restarts"] += 1
        self._close()
        with self._lock:
            self._snapshot["driver_alive"] = False

    def _open_and_consume(self) -> None:
        if platform.system() != "Linux":
            raise LinuxRotaryUnavailable("Linux rotary encoder acquisition is available on Linux only")
        source = self.config["source_config"]
        device, identity = discover_rotary_device(
            self._rel_axis, str(source.get("device_override", "")), self._sysfs_root, self._dev_root,
        )
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        self._fd = fd
        _configure_event_clock(fd)
        self.begin_acquisition(device, identity)
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while not self._stop.is_set():
                if not selector.select(timeout=0.25):
                    continue
                while not self._stop.is_set():
                    try:
                        data = os.read(fd, INPUT_EVENT_STRUCT.size * READ_EVENT_COUNT)
                    except BlockingIOError:
                        break
                    if not data:
                        raise LinuxRotaryUnavailable(f"Linux input device disconnected: {device}")
                    self.process_input_bytes(data)
