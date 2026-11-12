from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any

from .gpio_source import QuadratureGpioWorker
from .linux_rotary_source import LinuxRotaryWorker
from .pico_source import PicoUartWorker
from .tracking import AXES, acquisition_signature


class TrackingSourceManager:
    """Own independent GPIO workers and one shared Pan/Tilt/Zoom Pico reader."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[str, Any] = {}
        self._pico: PicoUartWorker | None = None
        self._pico_axes: tuple[str, ...] = ()
        self._profile_id: str | None = None

    def configure(self, profile_id: str, profile: dict[str, Any]) -> None:
        wanted: dict[str, dict[str, Any]] = {}
        pico_axes = tuple(axis for axis in ("pan", "tilt", "zoom")
                          if profile["axes"][axis]["source"] == "pico_uart")
        for axis in AXES:
            config = profile["axes"][axis]
            source = config["source"]
            sc = config.get("source_config", {})
            if source == "quadrature_gpio" and bool(sc.get("chip")) and sc.get("line_a") is not None and sc.get("line_b") is not None:
                wanted[axis] = deepcopy(config)
            elif source == "linux_rotary":
                wanted[axis] = deepcopy(config)
        with self._lock:
            old = self._workers
            old_pico = self._pico
            old_pico_axes = self._pico_axes
            same_profile = self._profile_id == profile_id
            self._workers = {}
            self._pico = None
            self._pico_axes = ()
            self._profile_id = profile_id
        created: dict[str, Any] = {}
        for axis, config in wanted.items():
            worker = old.pop(axis, None)
            worker_type = QuadratureGpioWorker if config["source"] == "quadrature_gpio" else LinuxRotaryWorker
            restart = (
                worker is None
                or not isinstance(worker, worker_type)
                or not same_profile
                or acquisition_signature(worker.config) != acquisition_signature(config)
            )
            if restart:
                if worker is not None:
                    worker.stop()
                worker = worker_type(axis, config)
                worker.start()
            else:
                worker.config = deepcopy(config)
            created[axis] = worker
        for worker in old.values():
            worker.stop()
        pico: PicoUartWorker | None = None
        if pico_axes:
            if old_pico is not None and same_profile:
                pico = old_pico
                for changed_axis in set(old_pico_axes) ^ set(pico_axes):
                    pico.clear_reference(changed_axis)  # Never reuse a source-switched reference.
            else:
                if old_pico is not None:
                    old_pico.stop()
                pico = PicoUartWorker()
                pico.start()
        elif old_pico is not None:
            old_pico.stop()
        with self._lock:
            self._workers = created
            self._pico = pico
            self._pico_axes = pico_axes

    def stop(self) -> None:
        with self._lock:
            workers, self._workers = self._workers, {}
            pico, self._pico = self._pico, None
            self._pico_axes = ()
        for worker in workers.values():
            worker.stop()
        if pico:
            pico.stop()

    def snapshots(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            workers = list(self._workers.items())
            pico = self._pico
            pico_axes = self._pico_axes
        result = {axis: worker.snapshot() for axis, worker in workers}
        if pico:
            result.update({axis: pico.snapshot(axis) for axis in pico_axes})
        return result

    def pico_status(self) -> dict[str, Any]:
        with self._lock:
            pico = self._pico
            axes = self._pico_axes
        result = pico.status() if pico is not None else {"connected": False, "device": None}
        result["enabled_axes"] = list(axes)
        return result

    def set_reference(self, axis: str, angle: float) -> str:
        with self._lock:
            pico = self._pico if axis in self._pico_axes else None
            worker = self._workers.get(axis)
        if pico is not None:
            return pico.set_reference(axis, angle)
        if worker is None:
            raise ValueError(f"{axis.title()} is not using a hardware encoder")
        return worker.set_reference(angle)

    def clear_reference(self, axis: str) -> None:
        with self._lock:
            pico = self._pico if axis in self._pico_axes else None
            worker = self._workers.get(axis)
        if pico is not None:
            pico.clear_reference(axis)
            return
        if worker is None:
            raise ValueError(f"{axis.title()} is not using a hardware encoder")
        worker.clear_reference()
