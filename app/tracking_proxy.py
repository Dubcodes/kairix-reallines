"""Web facade for dedicated tracking; no per-tick RPC or serial ownership."""
from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from pathlib import Path

from .pico_proxy import PicoProxy, rpc
from .tracking_mmap import TrackingMmapError, TrackingReader


def configuration(store):
    return {
        'profile': deepcopy(store.profile),
        'profile_index': deepcopy(store.profile_index),
        'world_calibration': deepcopy(store.world_calibration),
        'world_calibration_index': deepcopy(store.world_calibration_index),
        'zoom_fov_mapping': deepcopy(store.zoom_fov_mapping),
        'zoom_lens_profiles': deepcopy(store.zoom_lens_profiles),
    }


def token(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class DedicatedTrackingProxy(PicoProxy):
    def __init__(self, socket_path: str, mmap_path: str, store):
        super().__init__(socket_path)
        self.store = store
        self.reader = TrackingReader(Path(mmap_path), stale_seconds=.1)
        self._token = None
        self._generation = None
        self._producer = None
        self._last_valid = None
        self._last_status = {'connected': False}

    def _call(self, action: str, **kwargs):
        # Configuration/reference RPC is low frequency, never a 200 Hz path.
        return rpc(self.path, {'action': action, **kwargs}, timeout=1.0)

    def configure_pico_processing(self, values):
        self._processing = deepcopy(values)
        if self._token is not None:
            self._call('processing', values=values)
        return deepcopy(values)

    def configure(self, profile_id, profile):
        if any(cfg.get('source') in ('quadrature_gpio', 'linux_rotary')
               for cfg in profile['axes'].values()):
            raise ValueError('Dedicated tracking does not support mixed hardware sources yet')
        self._axes = tuple(axis for axis in self.AXES if profile['axes'][axis]['source'] == 'pico_uart')
        config = configuration(self.store)
        expected = token(config)
        result = self._call('configure', profile_id=profile_id, profile=profile,
                            processing=self._processing, config=config)
        if result.get('config_token') != expected:
            raise RuntimeError('Dedicated tracking configuration acknowledgement mismatch')
        self._token = expected
        self._generation = None

    def sync_configuration(self, config):
        expected = token(config)
        if expected == self._token:
            return
        result = self._call('sync', config=config)
        if result.get('config_token') != expected:
            raise RuntimeError('Dedicated tracking configuration acknowledgement mismatch')
        self._token = expected
        self._generation = None

    def latest(self):
        value = self.reader.read()
        if value['config_token'] != self._token:
            raise TrackingMmapError('dedicated tracking configuration mismatch')
        return value

    def snapshots(self):
        try:
            return self.latest()['samples']
        except TrackingMmapError:
            return {}

    def pico_status(self):
        try:
            status = self.latest()['status']
        except TrackingMmapError:
            status = {'connected': False, 'last_error': 'Dedicated pose missing, stale or incompatible'}
        status['isolated_owner'] = True
        status['dedicated_tracking'] = True
        status['enabled_axes'] = list(self._axes)
        status.setdefault('processing_settings', deepcopy(self._processing))
        return status

    def stop(self):
        self.reader.close()
        super().stop()
