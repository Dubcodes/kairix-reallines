from __future__ import annotations

import platform
import threading
import time
from collections import deque
from copy import deepcopy
from typing import Any

from .quadrature import QuadratureDecoder


class GpioUnavailable(RuntimeError):
    pass


class QuadratureGpioWorker:
    """One-axis libgpiod v2 event worker. Hardware imports stay in this adapter."""

    def __init__(self, axis: str, config: dict[str, Any]) -> None:
        self.axis = axis
        self.config = deepcopy(config)
        multiplier = int(config["mapping"]["quadrature_multiplier"])
        self.decoder = QuadratureDecoder(multiplier)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._request: Any = None
        self._event_times: deque[int] = deque(maxlen=256)
        self._snapshot: dict[str, Any] = {
            "axis": axis,
            "driver_alive": False,
            "configured": False,
            "raw_count": 0,
            "referenced": False,
            "reference_count": None,
            "reference_angle": 0.0,
            "health": "UNCONFIGURED",
            "status": "GPIO worker has not started",
            "last_event_mono_ns": None,
            "events": 0,
            "event_rate_hz": 0.0,
            "global_seqno": None,
            "line_seqno": {"a": None, "b": None},
            "sequence_gaps": 0,
            "illegal_transitions": 0,
            "restarts": 0,
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

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = deepcopy(self._snapshot)
            result["raw_count"] = self.decoder.count
            result["illegal_transitions"] = self.decoder.diagnostics.illegal_transitions
            return result

    def set_reference(self, angle: float) -> None:
        with self._lock:
            self._snapshot.update({
                "referenced": True,
                "reference_count": self.decoder.count,
                "reference_angle": float(angle),
                "health": "VALID" if self._snapshot["driver_alive"] else self._snapshot["health"],
                "status": "Referenced" if self._snapshot["driver_alive"] else self._snapshot["status"],
            })

    def clear_reference(self) -> None:
        with self._lock:
            self._snapshot.update({"referenced": False, "reference_count": None, "reference_angle": 0.0})
            if self._snapshot["configured"] and self._snapshot["driver_alive"]:
                self._snapshot.update({"health": "REFERENCE_REQUIRED", "status": "Set an encoder reference"})

    def _release(self) -> None:
        request, self._request = self._request, None
        if request is not None:
            try:
                request.release()
            except Exception:
                pass

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._snapshot.update({"driver_alive": False, "configured": False, "health": "ERROR", "status": message})

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
        bias_name = source.get("bias", "as_is")
        bias = {
            "as_is": gpiod.line.Bias.AS_IS,
            "pull_up": gpiod.line.Bias.PULL_UP,
            "pull_down": gpiod.line.Bias.PULL_DOWN,
        }[bias_name]
        settings = gpiod.LineSettings(
            direction=gpiod.line.Direction.INPUT,
            edge_detection=gpiod.line.Edge.BOTH,
            bias=bias,
        )
        request = gpiod.request_lines(chip, consumer=f"kairix-reallines-{self.axis}", config={(line_a, line_b): settings})
        self._request = request
        values = request.get_values([line_a, line_b])
        self.decoder.state = (int(bool(values[0].value)) << 1) | int(bool(values[1].value))
        levels = {line_a: int(bool(values[0].value)), line_b: int(bool(values[1].value))}
        with self._lock:
            health = "VALID" if self._snapshot["referenced"] else "REFERENCE_REQUIRED"
            self._snapshot.update({"driver_alive": True, "configured": True, "health": health, "status": "Referenced" if health == "VALID" else "Set an encoder reference"})
        last_global: int | None = None
        last_line = {line_a: None, line_b: None}
        while not self._stop.is_set():
            if not request.wait_edge_events(timeout=0.25):
                continue
            for event in request.read_edge_events():
                global_seq = getattr(event, "global_seqno", None)
                line_seq = getattr(event, "line_seqno", None)
                offset = getattr(event, "line_offset", None)
                if offset in levels:
                    levels[offset] = 1 if event.event_type == gpiod.EdgeEvent.Type.RISING_EDGE else 0
                with self._lock:
                    self.decoder.update(levels[line_a], levels[line_b])
                    event_mono_ns = time.monotonic_ns()
                    self._event_times.append(event_mono_ns)
                    event_span = (self._event_times[-1] - self._event_times[0]) / 1_000_000_000 if len(self._event_times) > 1 else 0.0
                    if global_seq is not None and last_global is not None and global_seq != last_global + 1:
                        self._snapshot["sequence_gaps"] += max(1, global_seq - last_global - 1)
                    if offset in last_line and line_seq is not None and last_line[offset] is not None and line_seq != last_line[offset] + 1:
                        self._snapshot["sequence_gaps"] += max(1, line_seq - last_line[offset] - 1)
                    last_global = global_seq if global_seq is not None else last_global
                    if offset in last_line:
                        last_line[offset] = line_seq
                    self._snapshot.update({
                        "raw_count": self.decoder.count,
                        "last_event_mono_ns": event_mono_ns,
                        "events": self._snapshot["events"] + 1,
                        "event_rate_hz": round((len(self._event_times) - 1) / event_span, 2) if event_span > 0 else 0.0,
                        "global_seqno": global_seq,
                        "line_seqno": {"a": last_line[line_a], "b": last_line[line_b]},
                        "illegal_transitions": self.decoder.diagnostics.illegal_transitions,
                    })

