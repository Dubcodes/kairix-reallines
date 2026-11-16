from __future__ import annotations

import asyncio
import time
from typing import Any


async def sample_tracking_once(store: Any, source_manager: Any, timestamp: float | None = None) -> None:
    """Canonical acquisition step. It deliberately has no publication responsibility."""
    now = time.monotonic() if timestamp is None else float(timestamp)
    profiler = getattr(store, "runtime_profiler", None)
    if profiler is None:
        samples = source_manager.snapshots()
    else:
        with profiler.measure("source_snapshot"):
            samples = source_manager.snapshots()
    async with store.lock:
        if profiler is None:
            store.sample_tick(samples, now)
        else:
            with profiler.measure("tracking_mapping_and_pose"):
                store.sample_tick(samples, now)
        recorder = getattr(store, "stability_recorder", None)
        if recorder is not None:
            render_pose, _sync = store._render_pose_state(now)
            recorder.record_acquisition(now, samples, store.tracking, store.camera, render_pose)


async def publish_pose_once(store: Any) -> None:
    """Compact pose publication step, isolated from acquisition latency."""
    profiler = getattr(store, "runtime_profiler", None)
    if profiler is None:
        await store.broadcast_pose()
    else:
        with profiler.measure("pose_snapshot_and_enqueue"):
            await store.broadcast_pose()


async def tracking_loop(store: Any, source_manager: Any, frequency_hz: float = 200.0) -> None:
    next_tick = time.monotonic()
    while True:
        actual = time.monotonic()
        profiler = getattr(store, "runtime_profiler", None)
        if profiler is not None:
            profiler.observe("acquisition_loop_jitter_ms", (actual - next_tick) * 1000.0)
        await sample_tracking_once(store, source_manager, actual)
        active_rate = float(getattr(source_manager, "acquisition_rate_hz", frequency_hz))
        interval = 1.0 / max(1.0, active_rate)
        next_tick += interval
        if next_tick < time.monotonic() - interval:
            next_tick = time.monotonic()
        await asyncio.sleep(max(0.0, next_tick - time.monotonic()))


async def pose_publication_loop(store: Any, frequency_hz: float = 50.0) -> None:
    interval = 1.0 / frequency_hz
    next_tick = time.monotonic()
    while True:
        actual = time.monotonic()
        profiler = getattr(store, "runtime_profiler", None)
        if profiler is not None:
            profiler.observe("publication_loop_jitter_ms", (actual - next_tick) * 1000.0)
        await publish_pose_once(store)
        next_tick += interval
        if next_tick < time.monotonic() - interval:
            next_tick = time.monotonic()
        await asyncio.sleep(max(0.0, next_tick - time.monotonic()))


async def imu_status_publication_loop(store: Any, imu_service: Any, interval_seconds: float = 0.5) -> None:
    """Publish infrequent full state only when optional sensor connectivity changes."""
    previous: tuple[Any, ...] | None = None
    while True:
        snapshot = imu_service.snapshot()
        current = (snapshot.get("enabled"), snapshot.get("source"), snapshot.get("health"), snapshot.get("last_error"))
        if previous is not None and current != previous:
            await store.broadcast_state()
        previous = current
        await asyncio.sleep(interval_seconds)
