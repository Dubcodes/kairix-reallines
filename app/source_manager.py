from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any

from .gpio_source import QuadratureGpioWorker
from .linux_rotary_source import LinuxRotaryWorker
from .tracking import AXES


class TrackingSourceManager:
    """Owns hardware source lifecycles independently of persistent application state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[str, Any] = {}
        self._profile_id: str | None = None

    def configure(self, profile_id: str, profile: dict[str, Any]) -> None:
        wanted: dict[str, dict[str, Any]] = {}
        for axis in AXES:
            config = profile["axes"][axis]
            source = config["source"]
            source_config = config.get("source_config", {})
            if source == "quadrature_gpio" and bool(source_config.get("chip")) and source_config.get("line_a") is not None and source_config.get("line_b") is not None:
                wanted[axis] = deepcopy(config)
            elif source == "linux_rotary":
                wanted[axis] = deepcopy(config)
        with self._lock:
            old = self._workers
            same_profile = self._profile_id == profile_id
            self._workers = {}
            self._profile_id = profile_id
        created: dict[str, Any] = {}
        for axis, config in wanted.items():
            worker = old.pop(axis, None)
            worker_type = QuadratureGpioWorker if config["source"] == "quadrature_gpio" else LinuxRotaryWorker
            if worker is None or not isinstance(worker, worker_type) or not same_profile or worker.config != config:
                if worker is not None:
                    worker.stop()
                worker = worker_type(axis, config)
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

    def set_reference(self, axis: str, angle: float) -> str:
        with self._lock:
            worker = self._workers.get(axis)
        if worker is None:
            raise ValueError(f"{axis.title()} is not using a hardware encoder")
        return worker.set_reference(angle)

    def clear_reference(self, axis: str) -> None:
        with self._lock:
            worker = self._workers.get(axis)
        if worker is None:
            raise ValueError(f"{axis.title()} is not using a hardware encoder")
        worker.clear_reference()

