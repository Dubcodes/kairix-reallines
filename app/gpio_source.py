from __future__ import annotations

import platform
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from typing import Any, Iterable

from .quadrature import QuadratureDecoder
from .tracking import degrees_per_count


KERNEL_EVENT_BUFFER_SIZE = 4096
READ_BATCH_SIZE = 1024
SEQUENCE_MODULUS = 1 << 32
SEQUENCE_HALF_RANGE = SEQUENCE_MODULUS >> 1


def _sequence_gap(previous: int, current: int) -> tuple[int, bool]:
    """Return missing forward values and whether the counter moved backwards."""
    delta = (int(current) - int(previous)) % SEQUENCE_MODULUS
    if delta <= 1:
        return 0, False
    if delta < SEQUENCE_HALF_RANGE:
        return delta - 1, False
    return 0, True


class GpioUnavailable(RuntimeError):
    pass


class QuadratureGpioWorker:
    """One-axis libgpiod v2 worker with hardware-independent edge processing."""

    RATE_WINDOW_NS = 1_000_000_000
    RATE_BUCKET_NS = 10_000_000
    RATE_BUCKET_CAPACITY = RATE_WINDOW_NS // RATE_BUCKET_NS + 2
    PEAK_MIN_WINDOW_NS = 100_000_000
    PEAK_MIN_EVENTS = 16

    def __init__(self, axis: str, config: dict[str, Any]) -> None:
        self.axis = axis
        self.config = deepcopy(config)
        self.decoder = QuadratureDecoder(int(config["mapping"]["quadrature_multiplier"]))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._request: Any = None
        # Ten-millisecond aggregates retain a useful one-second rate window
        # without allocating or scanning one entry per high-rate edge.
        self._motion_buckets: deque[list[int]] = deque(maxlen=self.RATE_BUCKET_CAPACITY)
        self._last_global_seqno: int | None = None
        self._last_line_seqno: dict[int, int | None] = {}
        self._snapshot: dict[str, Any] = {
            "axis": axis, "driver_alive": False, "configured": False,
            "raw_count": 0, "a_state": None, "b_state": None,
            "referenced": False, "reference_count": None, "reference_angle": 0.0, "reference_id": None,
            "health": "UNCONFIGURED", "status": "GPIO worker has not started", "last_error": None,
            "last_edge_timestamp_ns": None, "events": 0, "event_rate_hz": 0.0,
            "peak_event_rate_hz": 0.0, "largest_read_batch": 0,
            "kernel_event_buffer_size": KERNEL_EVENT_BUFFER_SIZE, "read_batch_size": READ_BATCH_SIZE,
            "counts_per_second": 0.0, "degrees_per_second": 0.0,
            "global_seqno": None, "line_seqno": {"a": None, "b": None},
            "global_sequence_gaps": 0, "line_sequence_gaps": 0,
            "confirmed_global_gaps": 0, "global_only_sequence_gaps": 0,
            "global_out_of_order_events": 0, "line_out_of_order_events": 0,
            "integrity_loss_events": 0, "integrity_lost": False, "integrity_status": "TRUSTED",
            "illegal_transitions": 0, "restarts": 0,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"quadrature-{self.axis}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._release()

    def _rates_locked(self, now_ns: int) -> tuple[float, float, float]:
        cutoff = now_ns - self.RATE_WINDOW_NS
        while len(self._motion_buckets) > 1 and self._motion_buckets[1][2] < cutoff:
            self._motion_buckets.popleft()
        last_edge = self._snapshot["last_edge_timestamp_ns"]
        if last_edge is None or now_ns - last_edge > self.RATE_WINDOW_NS or not self._motion_buckets:
            return 0.0, 0.0, 0.0
        first, last = self._motion_buckets[0], self._motion_buckets[-1]
        event_count = sum(bucket[3] for bucket in self._motion_buckets)
        span = (last[2] - first[1]) / 1_000_000_000
        if span <= 0:
            return 0.0, 0.0, 0.0
        edge_rate = (event_count - 1) / span
        counts_rate = (last[5] - first[4]) / span
        return edge_rate, counts_rate, counts_rate * degrees_per_count(self.config)

    def _record_motion_locked(self, timestamp_ns: int) -> None:
        timestamp_ns = int(timestamp_ns)
        bucket_id = timestamp_ns // self.RATE_BUCKET_NS
        count = self.decoder.count
        if self._motion_buckets and self._motion_buckets[-1][0] == bucket_id:
            bucket = self._motion_buckets[-1]
            bucket[2] = timestamp_ns
            bucket[3] += 1
            bucket[5] = count
        else:
            # bucket id, first timestamp, last timestamp, events, first count, last count
            self._motion_buckets.append([bucket_id, timestamp_ns, timestamp_ns, 1, count, count])

    def snapshot(self, now_ns: int | None = None) -> dict[str, Any]:
        now_ns = time.monotonic_ns() if now_ns is None else int(now_ns)
        with self._lock:
            result = deepcopy(self._snapshot)
            result["raw_count"] = self.decoder.count
            result["illegal_transitions"] = self.decoder.diagnostics.illegal_transitions
            edge_rate, counts_rate, degrees_rate = self._rates_locked(now_ns)
            self._update_peak_locked(now_ns, edge_rate)
            result.update({
                "event_rate_hz": round(edge_rate, 2),
                "peak_event_rate_hz": round(self._snapshot["peak_event_rate_hz"], 2),
                "counts_per_second": round(counts_rate, 2),
                "degrees_per_second": round(degrees_rate, 3),
                "last_edge_age_ms": round(max(0, now_ns - result["last_edge_timestamp_ns"]) / 1_000_000, 3) if result["last_edge_timestamp_ns"] is not None else None,
            })
            return result

    def set_reference(self, angle: float) -> str:
        with self._lock:
            if not self._snapshot["driver_alive"] or not self._snapshot["configured"]:
                raise ValueError("GPIO driver must be healthy before setting a reference")
            reference_id = str(uuid.uuid4())
            self._snapshot.update({
                "referenced": True, "reference_count": self.decoder.count,
                "reference_angle": float(angle), "reference_id": reference_id,
                "integrity_lost": False, "integrity_status": "TRUSTED",
                "health": "VALID" if self._snapshot["driver_alive"] else self._snapshot["health"],
                "status": "Referenced" if self._snapshot["driver_alive"] else self._snapshot["status"],
            })
            return reference_id

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

    def clear_reference(self) -> None:
        with self._lock:
            self._invalidate_reference_locked("Set an encoder reference")

    def begin_acquisition(self, a_state: int, b_state: int) -> None:
        """Initialise/reinitialise a GPIO request; never restores an old reference."""
        with self._lock:
            self.decoder.state = (int(bool(a_state)) << 1) | int(bool(b_state))
            source = self.config["source_config"]
            self._last_global_seqno = None
            self._last_line_seqno = {int(source["line_a"]): None, int(source["line_b"]): None}
            self._motion_buckets.clear()
            self._snapshot.update({
                "driver_alive": True, "configured": True,
                "a_state": int(bool(a_state)), "b_state": int(bool(b_state)),
                "health": "REFERENCE_REQUIRED", "status": "Set an encoder reference",
            })
            self._invalidate_reference_locked("Set an encoder reference")

    def _process_edge_locked(self, line_offset: int, is_rising: bool, timestamp_ns: int, global_seqno: int | None, line_seqno: int | None) -> bool:
        source = self.config["source_config"]
        line_a, line_b = int(source["line_a"]), int(source["line_b"])
        if line_offset == line_a:
            self._snapshot["a_state"] = int(is_rising)
        elif line_offset == line_b:
            self._snapshot["b_state"] = int(is_rising)
        else:
            return False
        before_illegal = self.decoder.diagnostics.illegal_transitions
        self.decoder.update(self._snapshot["a_state"], self._snapshot["b_state"])
        illegal = self.decoder.diagnostics.illegal_transitions > before_illegal
        global_missing, global_backward = (0, False)
        if global_seqno is not None and self._last_global_seqno is not None:
            global_missing, global_backward = _sequence_gap(self._last_global_seqno, global_seqno)
        global_gap = global_missing > 0
        previous_line = self._last_line_seqno.get(line_offset)
        line_missing, line_backward = (0, False)
        if line_seqno is not None and previous_line is not None:
            line_missing, line_backward = _sequence_gap(previous_line, line_seqno)
        line_gap = line_missing > 0
        if global_gap:
            self._snapshot["global_sequence_gaps"] += global_missing
            self._snapshot["confirmed_global_gaps"] += global_missing
            if not line_gap:
                self._snapshot["global_only_sequence_gaps"] += global_missing
        if line_gap:
            self._snapshot["line_sequence_gaps"] += line_missing
        if global_backward:
            self._snapshot["global_out_of_order_events"] += 1
        if line_backward:
            self._snapshot["line_out_of_order_events"] += 1
        if illegal or global_gap or line_gap or global_backward or line_backward:
            causes = (["illegal transition"] if illegal else []) + (["global sequence gap"] if global_gap else []) + (["line sequence gap"] if line_gap else []) + (["global sequence regression"] if global_backward else []) + (["line sequence regression"] if line_backward else [])
            self._invalidate_reference_locked(f"Count integrity lost ({', '.join(causes)}); set a new reference", integrity_loss=True)
        self._last_global_seqno = global_seqno if global_seqno is not None else self._last_global_seqno
        if line_seqno is not None:
            self._last_line_seqno[line_offset] = line_seqno
        self._record_motion_locked(timestamp_ns)
        self._snapshot.update({
            "raw_count": self.decoder.count, "last_edge_timestamp_ns": int(timestamp_ns),
            "events": self._snapshot["events"] + 1, "global_seqno": global_seqno,
            "line_seqno": {"a": self._last_line_seqno[line_a], "b": self._last_line_seqno[line_b]},
            "illegal_transitions": self.decoder.diagnostics.illegal_transitions,
        })
        return True

    def _finish_batch_locked(self, batch_size: int, timestamp_ns: int | None) -> None:
        self._snapshot["largest_read_batch"] = max(self._snapshot["largest_read_batch"], batch_size)
        if timestamp_ns is not None:
            event_rate, _, _ = self._rates_locked(timestamp_ns)
            self._update_peak_locked(timestamp_ns, event_rate)

    def _update_peak_locked(self, now_ns: int, event_rate: float) -> None:
        if not self._motion_buckets:
            return
        first, last = self._motion_buckets[0], self._motion_buckets[-1]
        event_count = sum(bucket[3] for bucket in self._motion_buckets)
        if event_count >= self.PEAK_MIN_EVENTS and last[2] - first[1] >= self.PEAK_MIN_WINDOW_NS:
            self._snapshot["peak_event_rate_hz"] = max(self._snapshot["peak_event_rate_hz"], event_rate)

    def process_edge(self, line_offset: int, is_rising: bool, timestamp_ns: int, global_seqno: int | None = None, line_seqno: int | None = None) -> None:
        """Apply one ordered kernel event without importing or requiring gpiod."""
        with self._lock:
            if self._process_edge_locked(line_offset, is_rising, timestamp_ns, global_seqno, line_seqno):
                self._snapshot["largest_read_batch"] = max(self._snapshot["largest_read_batch"], 1)

    def process_edge_batch(self, events: Iterable[tuple[int, bool, int, int | None, int | None]]) -> None:
        """Apply ordered ``(line, rising, timestamp, global seq, line seq)`` records under one lock."""
        with self._lock:
            processed = 0
            last_timestamp: int | None = None
            for line_offset, is_rising, timestamp_ns, global_seqno, line_seqno in events:
                if self._process_edge_locked(line_offset, is_rising, timestamp_ns, global_seqno, line_seqno):
                    processed += 1
                    last_timestamp = int(timestamp_ns)
            self._finish_batch_locked(processed, last_timestamp)

    def process_levels(self, a_state: int, b_state: int, timestamp_ns: int) -> None:
        """Testable sampled-level path for detecting impossible two-bit transitions."""
        with self._lock:
            before_illegal = self.decoder.diagnostics.illegal_transitions
            self._snapshot.update({"a_state": int(bool(a_state)), "b_state": int(bool(b_state))})
            self.decoder.update(a_state, b_state)
            illegal = self.decoder.diagnostics.illegal_transitions > before_illegal
            if illegal:
                self._invalidate_reference_locked("Count integrity lost (illegal transition); set a new reference", integrity_loss=True)
            self._record_motion_locked(timestamp_ns)
            self._snapshot.update({
                "raw_count": self.decoder.count, "last_edge_timestamp_ns": int(timestamp_ns),
                "events": self._snapshot["events"] + 1,
                "illegal_transitions": self.decoder.diagnostics.illegal_transitions,
            })

    def _release(self) -> None:
        request, self._request = self._request, None
        if request is not None:
            try:
                request.release()
            except Exception:
                pass

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._snapshot.update({"driver_alive": False, "configured": False, "health": "ERROR", "status": message, "last_error": message})
            self._invalidate_reference_locked(message)

    def _run(self) -> None:
        backoff = 0.25
        while not self._stop.is_set():
            try:
                self._open_and_consume()
                backoff = 0.25
            except Exception as exc:
                self._set_error(str(exc))
                self._release()
                if self._stop.wait(backoff):
                    break
                backoff = min(5.0, backoff * 2.0)
                with self._lock:
                    self._snapshot["restarts"] += 1
        self._release()
        with self._lock:
            self._snapshot["driver_alive"] = False

    def _process_gpiod_batch(self, events: list[Any], rising_edge_type: Any) -> None:
        with self._lock:
            processed = 0
            last_timestamp: int | None = None
            for event in events:
                timestamp_ns = int(event.timestamp_ns)
                if self._process_edge_locked(
                    int(event.line_offset), event.event_type == rising_edge_type, timestamp_ns,
                    int(event.global_seqno), int(event.line_seqno),
                ):
                    processed += 1
                    last_timestamp = timestamp_ns
            self._finish_batch_locked(processed, last_timestamp)

    def _drain_ready_events(self, request: Any, rising_edge_type: Any) -> None:
        """Consume all currently queued batches before returning to the blocking wait."""
        while not self._stop.is_set():
            events = request.read_edge_events(max_events=READ_BATCH_SIZE)
            if not events:
                return
            self._process_gpiod_batch(events, rising_edge_type)
            if not request.wait_edge_events(timeout=0):
                return

    def _open_and_consume(self) -> None:
        if platform.system() != "Linux":
            raise GpioUnavailable("libgpiod GPIO acquisition is available on Linux only")
        try:
            import gpiod  # type: ignore
        except ImportError as exc:
            raise GpioUnavailable("python3-libgpiod v2 is not installed") from exc
        source = self.config["source_config"]
        chip = str(source["chip"])
        line_a, line_b = int(source["line_a"]), int(source["line_b"])
        bias = {"as_is": gpiod.line.Bias.AS_IS, "pull_up": gpiod.line.Bias.PULL_UP, "pull_down": gpiod.line.Bias.PULL_DOWN}[source.get("bias", "as_is")]
        settings = gpiod.LineSettings(
            direction=gpiod.line.Direction.INPUT, edge_detection=gpiod.line.Edge.BOTH,
            bias=bias, event_clock=gpiod.line.Clock.MONOTONIC,
        )
        request = gpiod.request_lines(
            chip, consumer=f"kairix-reallines-{self.axis}", config={(line_a, line_b): settings},
            event_buffer_size=KERNEL_EVENT_BUFFER_SIZE,
        )
        self._request = request
        values = request.get_values([line_a, line_b])
        self.begin_acquisition(values[0].value, values[1].value)
        while not self._stop.is_set():
            if not request.wait_edge_events(timeout=0.25):
                continue
            self._drain_ready_events(request, gpiod.EdgeEvent.Type.RISING_EDGE)
