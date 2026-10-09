from __future__ import annotations

import asyncio
import time
from typing import Any


async def sample_tracking_once(store: Any, source_manager: Any, timestamp: float | None = None) -> None:
    """Canonical acquisition step. It deliberately has no publication responsibility."""
    now = time.monotonic() if timestamp is None else float(timestamp)
    async with store.lock:
        store.sample_tick(source_manager.snapshots(), now)


async def publish_pose_once(store: Any) -> None:
    """Compact pose publication step, isolated from acquisition latency."""
    await store.broadcast_pose()


async def tracking_loop(store: Any, source_manager: Any, frequency_hz: float = 200.0) -> None:
    interval = 1.0 / frequency_hz
    next_tick = time.monotonic()
    while True:
        await sample_tracking_once(store, source_manager, time.monotonic())
        next_tick += interval
        if next_tick < time.monotonic() - interval:
            next_tick = time.monotonic()
        await asyncio.sleep(max(0.0, next_tick - time.monotonic()))


async def pose_publication_loop(store: Any, frequency_hz: float = 50.0) -> None:
    interval = 1.0 / frequency_hz
    next_tick = time.monotonic()
    while True:
        await publish_pose_once(store)
        next_tick += interval
        if next_tick < time.monotonic() - interval:
            next_tick = time.monotonic()
        await asyncio.sleep(max(0.0, next_tick - time.monotonic()))
