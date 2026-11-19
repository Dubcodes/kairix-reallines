"""Web-process facade for external Pico UART owner; only latest snapshots retained."""
from __future__ import annotations

import json
import socket
import threading
import time
from copy import deepcopy
from typing import Any

from .pico_processing import DEFAULT_PICO_PROCESSING, active_processing_rate


class PicoOwnerUnavailable(RuntimeError):
    pass


def rpc(path: str, request: dict, timeout: float = 0.15) -> dict:
    payload = json.dumps(request, separators=(',', ':')).encode() + b'\n'
    if len(payload) > 128_000:
        raise ValueError('IPC command too large')
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(path)
            client.sendall(payload)
            response = bytearray()
            while len(response) <= 256_000:
                piece = client.recv(4096)
                if not piece:
                    break
                response.extend(piece)
                if b'\n' in piece:
                    break
        if not response or len(response) > 256_000:
            raise PicoOwnerUnavailable('No valid owner response')
        answer = json.loads(bytes(response).split(b'\n', 1)[0])
        if not answer.get('ok'):
            raise ValueError(str(answer.get('error', 'Pico owner rejected command')))
        return answer['result']
    except (OSError, json.JSONDecodeError) as exc:
        raise PicoOwnerUnavailable(f'Pico owner unavailable: {exc}') from exc


class PicoProxy:
    """Poll on a background thread. A stalled IPC endpoint never blocks 200 Hz mapping."""
    AXES = ('pan', 'tilt', 'zoom')

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._samples: dict[str, dict[str, Any]] = {}
        self._status: dict[str, Any] = {'connected': False}
        self._received = 0.0
        self._processing = deepcopy(DEFAULT_PICO_PROCESSING)
        self._axes: tuple[str, ...] = ()

    def _call(self, action: str, **kwargs) -> dict:
        return rpc(self.path, {'action': action, **kwargs})

    def configure_pico_processing(self, values):
        # Actual owner change happens in configure() or through processing().
        self._processing = deepcopy(values)
        if self._thread and self._thread.is_alive():
            self._call('processing', values=values)
        return deepcopy(values)

    @property
    def acquisition_rate_hz(self):
        return active_processing_rate(self._processing) if self._axes else 200

    def configure(self, profile_id, profile):
        # This first-stage proxy only supports Pico axes. Never silently
        # discard other configured hardware input sources.
        if any(cfg.get('source') in ('quadrature_gpio', 'linux_rotary')
               for cfg in profile['axes'].values()):
            raise ValueError('Isolated Pico owner mode does not yet support mixed GPIO/rotary hardware')
        axes = tuple(axis for axis in self.AXES if profile['axes'][axis]['source'] == 'pico_uart')
        # A confirmed round-trip is required before claiming configuration success.
        self._call('configure', profile_id=profile_id, profile=profile, processing=self._processing)
        self._axes = axes
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll, daemon=True, name='reallines-pico-ipc')
            self._thread.start()

    def _poll(self):
        next_tick = time.monotonic()
        while not self._stop.is_set():
            try:
                answer = self._call('snapshot')
                now = time.monotonic()
                with self._lock:
                    self._samples = answer['samples']
                    self._status = answer['status']
                    self._received = now
            except (PicoOwnerUnavailable, ValueError, KeyError):
                # Do not reuse stale references or pose as valid.
                with self._lock:
                    self._status = {'connected': False, 'last_error': 'Pico owner IPC unavailable'}
                    self._received = 0.0
            period = 1.0 / max(1, self.acquisition_rate_hz)
            next_tick = max(next_tick + period, time.monotonic())
            self._stop.wait(max(0.0, next_tick - time.monotonic()))

    def snapshots(self):
        with self._lock:
            samples = deepcopy(self._samples)
            fresh = self._received and time.monotonic() - self._received <= 0.05
        if not fresh:
            for axis in self._axes:
                sample = samples.setdefault(axis, {})
                sample.update(driver_alive=False, referenced=False, reference_id=None,
                              availability='UNAVAILABLE', integrity='UNKNOWN', health='STALE',
                              status='Isolated Pico owner not responding')
        return samples

    def pico_status(self):
        with self._lock:
            status = deepcopy(self._status)
            fresh = self._received and time.monotonic() - self._received <= 0.05
        if not fresh:
            status['connected'] = False
        status['enabled_axes'] = list(self._axes)
        status['isolated_owner'] = True
        status.setdefault('processing_settings', deepcopy(self._processing))
        return status

    def set_reference(self, axis, angle):
        if axis not in self._axes:
            raise ValueError('Axis is not configured for isolated Pico')
        return self._call('reference', axis=axis, angle=angle)['reference_id']

    def clear_reference(self, axis):
        if axis not in self._axes:
            raise ValueError('Axis is not configured for isolated Pico')
        self._call('clear', axis=axis)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self._thread = None
