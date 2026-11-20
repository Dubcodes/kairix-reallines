"""Opt-in authoritative 200-Hz Pico tracking process.

One process owns serial, canonical StateStore mapping and both latest-pose
shared-memory files. Web only acknowledges configuration/reference commands.
"""
from __future__ import annotations

import argparse
import math
import hashlib
import json
import os
import tempfile
import threading
import time
from collections import deque
from copy import deepcopy
from pathlib import Path

from .isolated_tracking_probe import load_shadow_store
from .native_pose import LatestPoseWriter
from .pico_owner import Handler, Owner, Server
from .tracking_mmap import TrackingWriter

DEFAULT_TRACKING = '/dev/shm/kairix-reallines-tracking-v1'
DEFAULT_NATIVE = '/dev/shm/kairix-reallines-pose-v1'


def config_token(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class TrackingOwner(Owner):
    def __init__(self, data_dir: Path, scratch: Path, tracking_path: Path, native_path: Path):
        super().__init__()
        self.store = load_shadow_store(data_dir, scratch)
        self.tracking_writer = TrackingWriter(tracking_path)
        self.native_writer = LatestPoseWriter(native_path)
        self._stop = threading.Event()
        self._worker = None
        self._config_token = None
        self._profile_identity = None
        self._last_error = None
        self._late_max_ms = 0.0
        self._ticks = 0
        self._started_at = None
        self._deadline_misses = 0
        self._processing_overruns = 0
        self._lateness_ms = deque(maxlen=12000)
        self._processing_ms = deque(maxlen=12000)
        self._cached_status = {"connected": False}
        # Exactly one rich-state pending slot: old snapshots are discarded, not queued.
        # Encoding JSON and reading diagnostics must not execute in the 5-ms tick.
        self._rich_cv = threading.Condition()
        self._rich_pending = None
        self._rich_worker = None
        self._rich_drops = 0

    def start(self):
        self.tracking_writer.open()
        self.native_writer.open()
        self._rich_worker = threading.Thread(target=self._rich_loop, name='reallines-rich-state-50hz', daemon=True)
        self._rich_worker.start()
        self._worker = threading.Thread(target=self._loop, name='reallines-dedicated-tracking', daemon=True)
        self._worker.start()

    def close(self):
        self._stop.set()
        with self._rich_cv:
            self._rich_cv.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=2)
        if self._rich_worker is not None:
            self._rich_worker.join(timeout=2)
        self.manager.stop()
        self.native_writer.close()
        self.tracking_writer.close()

    def _sync(self, config):
        if not isinstance(config, dict) or any(key not in config for key in (
            'profile', 'profile_index', 'world_calibration', 'world_calibration_index',
            'zoom_fov_mapping', 'zoom_lens_profiles',
        )):
            raise ValueError('Required tracking calibration fields missing')
        token = config_token(config)
        if token == self._config_token:
            return token
        profile_identity = (config['profile_index']['active_id'], config_token(config['profile']))
        self.store.profile_index = deepcopy(config['profile_index'])
        self.store.profile = deepcopy(config['profile'])
        self.store.world_calibration_index = deepcopy(config['world_calibration_index'])
        self.store.world_calibration = deepcopy(config['world_calibration'])
        self.store.zoom_lens_profiles = deepcopy(config['zoom_lens_profiles'])
        if profile_identity != self._profile_identity:
            self.store._refresh_tracking_from_profile()
            self._profile_identity = profile_identity
        # Restore operator-captured Zoom endpoints after applying source/profile
        # defaults; the next real sample validates current reference identity.
        self.store.zoom_fov_mapping = deepcopy(config['zoom_fov_mapping'])
        self._config_token = token
        return token

    @staticmethod
    def _stats(values):
        if not values:
            return {'p95_ms': None, 'p99_ms': None, 'max_ms': None}
        data = sorted(values)
        return {'p95_ms': round(data[math.ceil(len(data) * .95) - 1], 3),
                'p99_ms': round(data[math.ceil(len(data) * .99) - 1], 3),
                'max_ms': round(data[-1], 3)}

    def execute(self, request):
        action = request.get('action')
        if action == 'health':
            # Sorting up to 12,000 samples while holding the real-time owner lock
            # caused health polling to compete with the 5-ms producer deadline.
            with self._lock:
                elapsed = max(0.001, time.monotonic() - (self._started_at or time.monotonic()))
                result = {'configured': self.configured,
                          'pose_ticks': self._ticks,
                          'observed_pose_hz': round(self._ticks / elapsed, 3),
                          'missed_wakeup_deadlines': self._deadline_misses,
                          'processing_overruns': self._processing_overruns,
                          'last_error': self._last_error,
                          'config_token': self._config_token,
                          'native_generation': self.native_writer.generation,
                          'web_generation': self.tracking_writer.generation,
                          'rich_snapshot_drops': self._rich_drops}
                lateness = tuple(self._lateness_ms)
                processing = tuple(self._processing_ms)
            result['wake_lateness'] = self._stats(lateness)
            result['pose_processing_time'] = self._stats(processing)
            return result
        if action == 'sync':
            with self._lock:
                if not self.configured:
                    raise ValueError('Pico owner is not configured')
                try:
                    return {'config_token': self._sync(request['config'])}
                except Exception:
                    self.configured = False
                    raise
        if action == 'configure':
            if 'config' not in request:
                raise ValueError('Dedicated mapping requires full configuration')
            with self._lock:
                result = super().execute(request)
                if self._started_at is None:
                    self._started_at = time.monotonic()
                try:
                    result['config_token'] = self._sync(request['config'])
                except Exception:
                    self.configured = False
                    raise
                return result
        return super().execute(request)

    def _rich_loop(self):
        # Never publish a queue of obsolete camera states. A slow JSON encode
        # can drop intermediate rich snapshots; the 200-Hz native pose continues.
        last_status = 0.0
        while True:
            with self._rich_cv:
                self._rich_cv.wait_for(lambda: self._rich_pending is not None or self._stop.is_set())
                if self._stop.is_set():
                    return
                snapshot = self._rich_pending
                self._rich_pending = None
            try:
                if time.monotonic() - last_status >= .1:
                    self._cached_status = self.manager.pico_status()
                    last_status = time.monotonic()
                snapshot['status'] = self._cached_status
                self.tracking_writer.write(snapshot)
            except Exception as exc:
                # A failed rich-state writer is not permitted to silently leave
                # the owner claiming valid fresh tracking indefinitely.
                with self._lock:
                    self._last_error = 'rich-state publication: ' + repr(exc)[:240]
                    self.configured = False
                return

    def _loop(self):
        period = .005
        next_tick = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_tick:
                self._stop.wait(next_tick - now)
                continue
            lateness_ms = max(0.0, (now - next_tick) * 1000)
            self._late_max_ms = max(self._late_max_ms, lateness_ms)
            next_tick += period
            if next_tick < now - period:
                next_tick = now + period
            with self._lock:
                if not self.configured or not self._config_token:
                    continue
                try:
                    begin = time.perf_counter()
                    self._lateness_ms.append(lateness_ms)
                    if lateness_ms >= period * 1000:
                        self._deadline_misses += 1
                    samples = self.manager.snapshots()
                    self.store.sample_tick(samples, now)
                    self.native_writer.publish_store(self.store, samples, now)
                    self._ticks += 1
                    # Only capture an immutable latest snapshot inside the
                    # 5-ms lock. JSON/CRC/mmap writing occurs on another thread.
                    if self._ticks % 4 == 1:
                        rich = {
                            'config_token': self._config_token,
                            'samples': deepcopy(samples),
                            'tracking': deepcopy(self.store.tracking),
                            'camera': deepcopy(self.store.camera),
                            'zoom_fov': deepcopy(self.store.zoom_fov_mapping),
                        }
                        with self._rich_cv:
                            if self._rich_pending is not None:
                                self._rich_drops += 1
                            self._rich_pending = rich
                            self._rich_cv.notify()
                    processing_ms = (time.perf_counter() - begin) * 1000
                    self._processing_ms.append(processing_ms)
                    if processing_ms >= period * 1000:
                        self._processing_overruns += 1
                except Exception as exc:
                    # Never publish a falsely fresh pose when mapping fails.
                    self._last_error = repr(exc)[:300]
                    self.configured = False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', default='/dev/shm/kairix-pico-owner.sock')
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    parser.add_argument('--tracking-mmap', type=Path, default=Path(DEFAULT_TRACKING))
    parser.add_argument('--native-pose', type=Path, default=Path(DEFAULT_NATIVE))
    args = parser.parse_args(argv)
    socket_path = Path(args.socket)
    if socket_path.exists():
        parser.error(f'Existing owner socket: {socket_path}; verify owner is stopped before removing it')
    if not socket_path.parent.is_dir() or not args.tracking_mmap.parent.is_dir():
        parser.error('IPC/shared-memory parent directory is missing')
    if args.tracking_mmap.resolve() == args.native_pose.resolve():
        parser.error('Tracking and native pose files must be different')
    old_umask = os.umask(0o077)
    try:
        scratch_base = Path('/dev/shm') if Path('/dev/shm').is_dir() else None
        with tempfile.TemporaryDirectory(prefix='kairix-owner-', dir=scratch_base) as scratch:
            owner = TrackingOwner(args.data_dir, Path(scratch), args.tracking_mmap, args.native_pose)
            with Server(str(socket_path), Handler) as server:
                server.owner = owner
                try:
                    owner.start()
                    server.serve_forever(poll_interval=.1)
                finally:
                    owner.close()
    except KeyboardInterrupt:
        pass
    finally:
        os.umask(old_umask)
        if socket_path.exists() and socket_path.is_socket():
            socket_path.unlink()


if __name__ == '__main__':
    main()
