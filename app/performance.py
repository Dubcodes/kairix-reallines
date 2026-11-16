from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from typing import Any, Iterator


class RuntimeProfiler:
    """Low-overhead rolling wall/CPU timing summaries for runtime stages."""

    def __init__(self, capacity: int = 512) -> None:
        self.capacity = max(32, int(capacity))
        self._lock = threading.Lock()
        self._values: dict[str, deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=self.capacity)
        )
        self._gauges: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.capacity))

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

    @staticmethod
    def _statistics(values: list[float]) -> dict[str, float | int | None]:
        if not values:
            return {"samples": 0, "mean": None, "p95": None, "max": None}
        ordered = sorted(values)
        index = min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)
        return {
            "samples": len(values),
            "mean": round(sum(values) / len(values), 4),
            "p95": round(ordered[index], 4),
            "max": round(ordered[-1], 4),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            timings = {name: list(values) for name, values in self._values.items()}
            gauges = {name: list(values) for name, values in self._gauges.items()}
        return {
            "timings_ms": {
                name: {
                    "wall": self._statistics([sample[0] for sample in values]),
                    "process_cpu": self._statistics([sample[1] for sample in values]),
                }
                for name, values in timings.items()
            },
            "gauges": {name: self._statistics(values) for name, values in gauges.items()},
            "window_capacity": self.capacity,
        }
