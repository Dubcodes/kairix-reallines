"""Shared RP2040 UART tracking source for Pan/Tilt/Zoom.

The RP2040 counts every quadrature transition in PIO. A delayed or dropped
UART packet cannot erase movement because the next packet contains cumulative
counts. References are held in Pi RAM and invalidated on a Pico reboot.
"""
from __future__ import annotations

import os
import select
from collections import deque
import threading
import time
import uuid
from copy import deepcopy

from .pico_protocol import PicoPacket, PicoStreamDecoder
from .pico_processing import DEFAULT_PICO_PROCESSING, active_processing_rate, normalise_pico_processing

AXIS_NAMES = ("pan", "tilt", "zoom")
DEFAULT_DEVICE = "/dev/serial0"
BAUD = 1_000_000
STALE_SECONDS = 0.35
FIRMWARE_NOMINAL_RATE_HZ = 500


class PicoUartWorker:
    def __init__(self, device: str | None = None) -> None:
        self.device = device or os.getenv("KAIRIX_PICO_UART_DEVICE", DEFAULT_DEVICE)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._packet: PicoPacket | None = None
        self._received_at: float | None = None
        self._connected = False
        self._last_error: str | None = None
        self._decoder = PicoStreamDecoder()
        self._references: dict[str, dict] = {}
        self._packets = 0
        self._missed_packets = 0
        self._out_of_order = 0
        self._restarts = 0
        self._last_sequence: int | None = None
        self._session_id: int | None = None
        self._last_press_counts: tuple[int, int, int] | None = None
        self._press_events = [0, 0, 0]
        self._started_at = time.monotonic()
        self._recent_packets = deque(maxlen=1024)
        self._recent_bytes = deque(maxlen=2048)
        self._processing_latencies = deque(maxlen=1024)
        self._processing_intervals = deque(maxlen=1024)
        self._last_consumed_at: float | None = None
        self._processing = dict(DEFAULT_PICO_PROCESSING)
        self._queue: deque[tuple[PicoPacket, float]] = deque()
        self._bytes_received = 0
        self._samples_consumed = 0
        self._processing_discards = 0
        self._buffer_overflows = 0
        self._link_timeouts = 0

    def configure_processing(self, values: dict | None) -> dict:
        settings = normalise_pico_processing(values)
        with self._lock:
            capacity = settings["buffer_capacity"]
            while len(self._queue) > capacity:
                self._queue.popleft()
                self._buffer_overflows += 1
                self._processing_discards += 1
            self._processing = settings
        return deepcopy(settings)

    @property
    def processing_rate_hz(self) -> int:
        with self._lock:
            return active_processing_rate(self._processing)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="reallines-pico-uart", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self._thread = None
        with self._lock:
            self._connected = False
            self._received_at = None
            self._references.clear()

    def _open_uart(self) -> int:
        # Import only on Linux; packet parsing/tests must also run on Windows.
        import termios
        fd = os.open(self.device, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0  # Ignore all input translations, software flow control.
            attrs[1] = 0
            attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
            attrs[3] = 0
            attrs[4] = termios.B1000000
            attrs[5] = termios.B1000000
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIFLUSH)
            return fd
        except Exception:
            os.close(fd)
            raise

    def _run(self) -> None:
        while not self._stop.is_set():
            fd = None
            try:
                fd = self._open_uart()
                with self._lock:
                    self._connected = True
                    self._last_error = None
                while not self._stop.is_set():
                    readable, _, _ = select.select([fd], [], [], 0.1)
                    if readable:
                        data = os.read(fd, 4096)
                        if data:
                            self.feed(data)
                        else:
                            raise OSError("UART returned EOF")
            except Exception as exc:
                with self._lock:
                    self._connected = False
                    self._received_at = None
                    self._references.clear()  # A broken serial connection must require re-reference.
                    self._last_error = str(exc)[:300]
            finally:
                if fd is not None:
                    os.close(fd)
            self._stop.wait(0.5)

    def feed(self, data: bytes, now: float | None = None) -> None:
        """Accept bytes from UART, or inject fixture data in tests."""
        arrival = time.monotonic() if now is None else float(now)
        with self._lock:
            self._bytes_received += len(data)
            self._recent_bytes.append((arrival, len(data)))
            for packet in self._decoder.feed(data):
                # Long silence means the camera might have moved or reset while
                # the host wasn't observing it. Keep measurements but invalidate
                # pose references until deliberately re-established.
                if self._received_at is not None and arrival - self._received_at > STALE_SECONDS:
                    self._references.clear()
                    self._link_timeouts += 1
                # Even a pathological reused random session must not preserve
                # references if the Pico time goes backwards while sequence rises.
                if (self._packet is not None and self._session_id == packet.session_id
                        and packet.pico_us < self._packet.pico_us
                        and ((packet.sequence - self._packet.sequence) & 0xFFFFFFFF) < 0x80000000):
                    self._references.clear()
                    self._restarts += 1
                    self._last_sequence = None
                    self._last_press_counts = None
                if self._session_id != packet.session_id:
                    if self._session_id is not None:
                        self._restarts += 1
                    self._session_id = packet.session_id
                    self._references.clear()
                    self._last_sequence = None
                    self._last_press_counts = None
                if self._last_sequence is not None:
                    diff = (packet.sequence - self._last_sequence) & 0xFFFFFFFF
                    if diff == 0 or diff >= 0x80000000:
                        self._out_of_order += 1
                        continue
                    if diff > 1:
                        self._missed_packets += diff - 1
                self._last_sequence = packet.sequence
                if self._last_press_counts is not None:
                    for i, (new, old) in enumerate(zip(packet.button_presses, self._last_press_counts)):
                        delta = (new - old) & 0xFFFF
                        if delta < 0x8000:  # Longer outages are ambiguous modulo 65536.
                            self._press_events[i] += delta
                self._last_press_counts = packet.button_presses
                self._packet = packet
                self._received_at = arrival
                self._packets += 1
                self._recent_packets.append(arrival)
                if len(self._queue) >= self._processing["buffer_capacity"]:
                    self._queue.popleft()
                    self._buffer_overflows += 1
                    self._processing_discards += 1
                self._queue.append((packet, arrival))
                self._connected = True
                self._last_error = None

    def _fresh(self, now: float) -> bool:
        return self._connected and self._packet is not None and self._received_at is not None and 0 <= now - self._received_at <= STALE_SECONDS

    def set_reference(self, axis: str, angle: float) -> str:
        if axis not in AXIS_NAMES:
            raise ValueError("Pico supports Pan, Tilt and Zoom only")
        with self._lock:
            if not self._fresh(time.monotonic()):
                raise ValueError("Pico has no fresh UART count; verify wiring before referencing")
            count = getattr(self._packet, axis)
            reference_id = uuid.uuid4().hex
            self._references[axis] = {"reference_count": count, "reference_angle": angle, "reference_id": reference_id}
            return reference_id

    def clear_reference(self, axis: str) -> None:
        with self._lock:
            self._references.pop(axis, None)

    def _snapshot(self, axis: str, packet: PicoPacket | None, received_at: float | None, now: float) -> dict:
        fresh = self._connected and packet is not None and received_at is not None and 0 <= now - received_at <= STALE_SECONDS
        reference = self._references.get(axis) or {}
        count = getattr(packet, axis) if packet else 0
        age = (now - received_at) * 1000 if received_at is not None else None
        integrity = "TRUSTED" if fresh else "UNKNOWN"
        status = ("Referenced" if fresh and reference else
                  "Set a new encoder reference" if fresh else
                  "Pico UART stale or disconnected")
        return {
            "raw_count": count, "configured": True, "driver_alive": fresh,
            "referenced": bool(reference), "reference_count": reference.get("reference_count"),
            "reference_angle": reference.get("reference_angle", 0.0), "reference_id": reference.get("reference_id"),
            "sample_mono": received_at, "availability": "AVAILABLE" if fresh else "UNAVAILABLE",
            "integrity": integrity, "integrity_reason": None,
            "calibration_confidence": "TRUSTED" if reference and fresh else "UNREFERENCED",
            "integrity_policy": "strict", "health": "VALID" if fresh else "STALE", "status": status,
            "pico_session": self._session_id, "packet_seq": packet.sequence if packet else None,
            "packet_age_ms": round(age, 2) if age is not None else None,
            "packet_gaps": self._missed_packets, "crc_errors": self._decoder.crc_errors,
            "protocol_errors": self._decoder.protocol_errors,
            "firmware_flags": packet.flags if packet else None,
            "firmware_version": packet.firmware_version if packet else None, "device": self.device,
        }

    def snapshot(self, axis: str, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            return self._snapshot(axis, self._packet, self._received_at, now)

    def consume_snapshots(self, axes: tuple[str, ...], now: float | None = None) -> dict[str, dict]:
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            packet, received_at = self._packet, self._received_at
            if self._queue:
                if self._processing["mode"] == "latest":
                    packet, received_at = self._queue[-1]
                    skipped = len(self._queue) - 1
                    self._processing_discards += skipped
                    self._queue.clear()
                else:
                    packet, received_at = self._queue.popleft()
                self._samples_consumed += 1
                self._processing_latencies.append(max(0.0, (now - received_at) * 1000.0))
                if self._last_consumed_at is not None:
                    self._processing_intervals.append((now - self._last_consumed_at) * 1000.0)
                self._last_consumed_at = now
            return {axis: self._snapshot(axis, packet, received_at, now) for axis in axes}

    def status(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            packet = self._packet
            age = (now - self._received_at) * 1000 if self._received_at is not None else None
            cutoff = now - 1.0
            rates = [stamp for stamp in self._recent_packets if stamp >= cutoff]
            byte_rate = sum(size for stamp, size in self._recent_bytes if stamp >= cutoff)
            rate_hz = (round((len(rates) - 1) / max(0.001, rates[-1] - rates[0]), 1)
                       if len(rates) > 1 and self._fresh(now) else 0.0)
            consumed_rate = 0.0
            if len(self._processing_intervals) > 1:
                mean_interval = sum(self._processing_intervals) / len(self._processing_intervals)
                consumed_rate = round(1000.0 / mean_interval, 1) if mean_interval > 0 else 0.0
            target_interval = 1000.0 / active_processing_rate(self._processing)
            jitter = sorted(abs(value - target_interval) for value in self._processing_intervals)
            latencies = sorted(self._processing_latencies)
            percentile = lambda values: round(values[min(len(values) - 1, int((len(values) - 1) * .95))], 3) if values else None
            return {
                "device": self.device,
                "baud": BAUD,
                "connected": self._fresh(now),
                "port_open": self._connected,
                "last_packet_ms": round(age, 2) if age is not None else None,
                "session_id": self._session_id,
                "sequence": packet.sequence if packet else None,
                "firmware_protocol": 1 if packet else None,
                "uptime_ms": packet.uptime_ms if packet else None,
                "pico_timestamp_us": packet.pico_us if packet else None,
                "firmware_version": packet.firmware_version if packet else None,
                "watchdog_reset": packet.watchdog_reset if packet else None,
                "focus_reserved": packet.focus_reserved if packet else None,
                "focus_count": packet.focus if packet else None,
                "unknown_flags": packet.unknown_flags if packet else None,
                "packet_rate_hz": rate_hz,
                "incoming_bytes_per_second": byte_rate,
                "firmware_nominal_rate_hz": FIRMWARE_NOMINAL_RATE_HZ,
                "processing_mode": self._processing["mode"],
                "processing_target_hz": active_processing_rate(self._processing),
                "processed_sample_rate_hz": consumed_rate,
                "samples_consumed": self._samples_consumed,
                "samples_discarded": self._processing_discards,
                "buffer_occupancy": len(self._queue),
                "buffer_capacity": self._processing["buffer_capacity"],
                "buffer_overflows": self._buffer_overflows,
                "processing_latency_ms_p95": percentile(latencies),
                "processing_jitter_ms_p95": percentile(jitter),
                "processing_settings": deepcopy(self._processing),
                "link_timeouts": self._link_timeouts,
                "counts": {axis: getattr(packet, axis) if packet else None for axis in AXIS_NAMES},
                "button_bits": packet.button_bits if packet else 0,
                "button_presses": deepcopy(self._press_events),
                "packet_gaps": self._missed_packets,
                "out_of_order_packets": self._out_of_order,
                "crc_errors": self._decoder.crc_errors,
                "protocol_errors": self._decoder.protocol_errors,
                "resync_bytes": self._decoder.dropped_bytes,
                "restarts": self._restarts,
                "firmware_flags": packet.flags if packet else None,
                "fault_flags": None,  # Not implemented by the flashed v1 firmware.
                "last_error": self._last_error,
                "received_packets": self._packets,
                "monitor_uptime_s": round(now - self._started_at, 1),
            }
