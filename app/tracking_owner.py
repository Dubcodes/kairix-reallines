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

    def start(self):
        self.tracking_writer.open()
        self.native_writer.open()
        self._worker = threading.Thread(target=self._loop, name='reallines-dedicated-tracking', daemon=True)
        self._worker.start()

    def close(self):
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=2)
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
            with self._lock:
                elapsed = max(0.001, time.monotonic() - (self._started_at or time.monotonic()))
                return {'configured': self.configured,
                        'pose_ticks': self._ticks,
                        'observed_pose_hz': round(self._ticks / elapsed, 3),
                        'missed_wakeup_deadlines': self._deadline_misses,
                        'processing_overruns': self._processing_overruns,
                        'wake_lateness': self._stats(self._lateness_ms),
                        'pose_processing_time': self._stats(self._processing_ms),
                        'last_error': self._last_error,
                        'config_token': self._config_token,
                        'native_generation': self.native_writer.generation,
                        'web_generation': self.tracking_writer.generation}
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
                    # Keep rich web state at 50 Hz; the compact native pose is
                    # published at full 200 Hz and stays off the JSON path.
                    if self._ticks % 20 == 1:
                        self._cached_status = self.manager.pico_status()
                    if self._ticks % 4 == 1:
                        self.tracking_writer.write({
                            'config_token': self._config_token,
                            'samples': samples,
                            'tracking': self.store.tracking,
                            'camera': self.store.camera,
                            'zoom_fov': self.store.zoom_fov_mapping,
                            'status': self._cached_status,
                        })
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
