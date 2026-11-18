from __future__ import annotations

import math
import gc
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from typing import Any, Iterator


class RuntimeProfiler:
    """Low-overhead rolling wall/CPU timing summaries for runtime stages."""

    def __init__(self, capacity: int = 6000, loop_window_seconds: float = 30.0) -> None:
        self.capacity = max(32, int(capacity))
        self.loop_window_seconds = max(5.0, float(loop_window_seconds))
        self._lock = threading.Lock()
        self._values: dict[str, deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=self.capacity)
        )
        self._gauges: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.capacity))
        self._loop_ticks: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.capacity))
        self._loop_targets: dict[str, float] = {}
        self._missed_deadlines: dict[str, int] = defaultdict(int)
        self._gc_started: float | None = None
        self._gc_collections = 0
        self._gc_uncollectable = 0
        self._gc_installed = False
        self._cpu_wall_started = time.perf_counter()
        self._cpu_process_started = time.process_time()

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        wall_start = time.perf_counter()
        cpu_start = time.process_time()
        try:
            yield
        finally:
            wall_ms = (time.perf_counter() - wall_start) * 1000.0
            cpu_ms = (time.process_time() - cpu_start) * 1000.0
            with self._lock:
                self._values[name].append((wall_ms, cpu_ms))

    def observe(self, name: str, value: float) -> None:
        number = float(value)
        if not math.isfinite(number):
            return
        with self._lock:
            self._gauges[name].append(number)

    def observe_loop(self, name: str, timestamp: float, target_hz: float,
                     lateness_ms: float) -> None:
        """Record one scheduler iteration without doing work proportional to history."""
        now = float(timestamp)
        target = max(1.0, float(target_hz))
        with self._lock:
            self._loop_ticks[name].append(now)
            while self._loop_ticks[name] and now - self._loop_ticks[name][0] > self.loop_window_seconds:
                self._loop_ticks[name].popleft()
            self._loop_targets[name] = target
            if float(lateness_ms) > 1000.0 / target:
                self._missed_deadlines[name] += 1

    def _gc_callback(self, phase: str, info: dict[str, Any]) -> None:
        if phase == "start":
            self._gc_started = time.perf_counter()
            return
        started, self._gc_started = self._gc_started, None
        if started is not None:
            self.observe("gc_pause_ms", (time.perf_counter() - started) * 1000.0)
        with self._lock:
            self._gc_collections += 1
            self._gc_uncollectable += int(info.get("uncollectable", 0) or 0)

    def install_gc_monitoring(self) -> None:
        if not self._gc_installed:
            gc.callbacks.append(self._gc_callback)
            self._gc_installed = True

    def uninstall_gc_monitoring(self) -> None:
        if self._gc_installed:
            try:
                gc.callbacks.remove(self._gc_callback)
            except ValueError:
                pass
            self._gc_installed = False

    @staticmethod
    def _statistics(values: list[float]) -> dict[str, float | int | None]:
        if not values:
            return {"samples": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
        ordered = sorted(values)
        percentile = lambda fraction: ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]
        return {
            "samples": len(values),
            "mean": round(sum(values) / len(values), 4),
            "p50": round(percentile(0.50), 4),
            "p95": round(percentile(0.95), 4),
            "p99": round(percentile(0.99), 4),
            "max": round(ordered[-1], 4),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            timings = {name: list(values) for name, values in self._values.items()}
            gauges = {name: list(values) for name, values in self._gauges.items()}
            loops = {name: list(values) for name, values in self._loop_ticks.items()}
            targets = dict(self._loop_targets)
            missed = dict(self._missed_deadlines)
            gc_collections = self._gc_collections
            gc_uncollectable = self._gc_uncollectable
        wall_elapsed = max(1e-9, time.perf_counter() - self._cpu_wall_started)
        process_elapsed = max(0.0, time.process_time() - self._cpu_process_started)
        return {
            "timings_ms": {
                name: {
                    "wall": self._statistics([sample[0] for sample in values]),
                    "process_cpu": self._statistics([sample[1] for sample in values]),
                }
                for name, values in timings.items()
            },
            "gauges": {name: self._statistics(values) for name, values in gauges.items()},
            "loops": {
                name: {
                    "target_hz": targets.get(name),
                    "actual_hz": round((len(values) - 1) / (values[-1] - values[0]), 3)
                    if len(values) > 1 and values[-1] > values[0] else None,
                    "iterations_in_window": len(values),
                    "window_seconds": self.loop_window_seconds,
                    "missed_deadlines_since_start": missed.get(name, 0),
                }
                for name, values in loops.items()
            },
            "process_cpu_percent_since_start": round(process_elapsed / wall_elapsed * 100.0, 2),
            "garbage_collection": {
                "collections_since_monitor_start": gc_collections,
                "uncollectable_since_monitor_start": gc_uncollectable,
                "monitoring": self._gc_installed,
            },
            "window_capacity": self.capacity,
        }
