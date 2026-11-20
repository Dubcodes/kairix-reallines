"""50-Hz web presentation of independently calculated camera state.

This loop never opens UART, runs camera mapping, or sends an RPC per frame.
Browser WebSocket publication still belongs to the existing web event loop.
"""
from __future__ import annotations

import asyncio
import time
from copy import deepcopy

from .tracking_mmap import TrackingMmapError
from .runtime import publish_pose_once


def mark_unavailable(store):
    store.tracking['valid'] = False
    store.tracking['available'] = False
    store.tracking['integrity'] = 'UNKNOWN'
    for runtime in store.tracking['axes'].values():
        if runtime.get('source') == 'pico_uart':
            runtime['valid'] = False
            runtime['driver_alive'] = False
            runtime['availability'] = 'UNAVAILABLE'
            runtime['integrity'] = 'UNKNOWN'
            runtime['health'] = 'ERROR'
            runtime['status'] = 'Dedicated tracking unavailable or stale'
    store.camera['valid'] = False
    store.camera['world_valid'] = False
    store.camera['tracking_integrity'] = 'UNKNOWN'


def adopt(store, state, last_identity):
    identity = (state['producer_id'], state['generation'])
    if identity == last_identity:
        return identity
    if last_identity is not None and identity[0] != last_identity[0]:
        # Never interpolate across two producer lifetimes.
        store.pose_history.reset('dedicated tracking producer changed')
        store._pose_reference_key = None
    store.tracking = deepcopy(state['tracking'])
    store.camera = deepcopy(state['camera'])
    # A local operator Zoom endpoint edit can be awaiting owner ACK. While
    # that transaction is in flight, do not overwrite its pending calibration
    # with an older owner snapshot (the owner token is still the old token).
    if not getattr(store, '_dedicated_config_syncing', False):
        store.zoom_fov_mapping = deepcopy(state['zoom_fov'])
    store._refresh_pose_reference_and_sample()
    return identity


async def dedicated_pose_consumer_loop(store, proxy, rate_hz=50):
    period = 1.0 / rate_hz
    next_tick = time.monotonic()
    last_identity = None
    unavailable = False
    while True:
        try:
            value = proxy.latest()
        except (TrackingMmapError, OSError, ValueError, KeyError):
            async with store.lock:
                if not unavailable:
                    mark_unavailable(store)
                    store.pose_history.reset('dedicated tracking producer unavailable')
                    store._pose_reference_key = None
                    store._refresh_pose_reference_and_sample()
                    unavailable = True
                    last_identity = None
        else:
            async with store.lock:
                last_identity = adopt(store, value, last_identity)
                unavailable = False
        # In dedicated mode adoption and publishing share the same 50-Hz
        # deadline. Never run two free-running web timers for these stages.
        await publish_pose_once(store)
        next_tick += period
        if next_tick < time.monotonic() - period:
            next_tick = time.monotonic()
        await asyncio.sleep(max(0, next_tick - time.monotonic()))
