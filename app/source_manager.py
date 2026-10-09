from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any

from .gpio_source import QuadratureGpioWorker
from .tracking import AXES


class TrackingSourceManager:
    """Owns hardware source lifecycles independently of persistent application state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[str, QuadratureGpioWorker] = {}
        self._profile_id: str | None = None

    def configure(self, profile_id: str, profile: dict[str, Any]) -> None:
        wanted = {
            axis: deepcopy(profile["axes"][axis])
            for axis in AXES
            if profile["axes"][axis]["source"] == "quadrature_gpio"
            and bool(profile["axes"][axis].get("source_config", {}).get("chip"))
            and profile["axes"][axis].get("source_config", {}).get("line_a") is not None
            and profile["axes"][axis].get("source_config", {}).get("line_b") is not None
        }
        with self._lock:
            old = self._workers
            same_profile = self._profile_id == profile_id
            self._workers = {}
            self._profile_id = profile_id
        created: dict[str, QuadratureGpioWorker] = {}
        for axis, config in wanted.items():
            worker = old.pop(axis, None)
            if worker is None or not same_profile or worker.config != config:
                if worker is not None:
                    worker.stop()
                worker = QuadratureGpioWorker(axis, config)
                worker.start()
            created[axis] = worker
        for worker in old.values():
            worker.stop()
        with self._lock:
            self._workers = created

    def stop(self) -> None:
        with self._lock:
            workers, self._workers = self._workers, {}
        for worker in workers.values():
            worker.stop()

    def snapshots(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            workers = list(self._workers.items())
        return {axis: worker.snapshot() for axis, worker in workers}

    def set_reference(self, axis: str, angle: float) -> None:
        with self._lock:
            worker = self._workers.get(axis)
        if worker is None:
            raise ValueError(f"{axis.title()} is not using quadrature GPIO")
        worker.set_reference(angle)

    def clear_reference(self, axis: str) -> None:
        with self._lock:
            worker = self._workers.get(axis)
        if worker is None:
            raise ValueError(f"{axis.title()} is not using quadrature GPIO")
        worker.clear_reference()

