"""Opt-in sole owner of Pico UART, exposing bounded local IPC to RealLines.

This is TRANSPORT isolation, not yet authoritative pose/GFX process isolation.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import socketserver
import threading
from pathlib import Path

from .source_manager import TrackingSourceManager

MAX_REQUEST = 128_000


class Owner:
    def __init__(self):
        self.manager = TrackingSourceManager()
        self._lock = threading.RLock()
        self.configured = False

    def execute(self, request: dict) -> dict:
        action = request.get('action')
        with self._lock:
            if action == 'configure':
                profile = request['profile']
                profile_id = request['profile_id']
                self.manager.configure_pico_processing(request['processing'])
                self.manager.configure(profile_id, profile)
                self.configured = True
                return {'ok': True}
            if not self.configured:
                raise ValueError('Pico owner has not been configured')
            if action == 'snapshot':
                return {'samples': self.manager.snapshots(), 'status': self.manager.pico_status()}
            if action == 'status':
                return {'status': self.manager.pico_status()}
            if action == 'reference':
                axis = request['axis']
                if axis not in ('pan', 'tilt', 'zoom'):
                    raise ValueError('Unsupported Pico reference axis')
                return {'reference_id': self.manager.set_reference(axis, float(request['angle']))}
            if action == 'clear':
                axis = request['axis']
                if axis not in ('pan', 'tilt', 'zoom'):
                    raise ValueError('Unsupported Pico reference axis')
                self.manager.clear_reference(axis)
                return {'ok': True}
            if action == 'processing':
                return {'processing': self.manager.configure_pico_processing(request['values'])}
            raise ValueError('Unknown Pico IPC command')


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            line = self.rfile.readline(MAX_REQUEST + 1)
            if not line:
                return
            if len(line) > MAX_REQUEST or not line.endswith(b'\n'):
                return
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError('Expected request object')
                result = self.server.owner.execute(req)
                response = {'ok': True, 'result': result}
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                response = {'ok': False, 'error': str(exc)[:200]}
            self.wfile.write(json.dumps(response, separators=(',', ':')).encode() + b'\n')


# Unix-domain stream servers are unavailable on native Windows Python.
# Keep import-time unit tests cross-platform without exposing a TCP fallback.
if hasattr(socketserver, 'UnixStreamServer'):
    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        allow_reuse_address = False
else:
    class Server:
        def __init__(self, *args, **kwargs):
            raise RuntimeError('Pico owner IPC requires a Unix-domain socket (Linux)')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', default='/run/kairix-pico-owner.sock')
    args = parser.parse_args(argv)
    path = Path(args.socket)
    if not path.parent.is_dir():
        parser.error('Socket parent directory does not exist')
    if path.exists():
        # A stale socket is not silently replaced: avoid a second owner.
        parser.error(f'Existing owner socket: {path}; investigate service before clearing')
    old_umask = os.umask(0o077)
    owner = Owner()
    try:
        with Server(str(path), Handler) as server:
            server.owner = owner
            try:
                server.serve_forever(poll_interval=0.1)
            finally:
                owner.manager.stop()
    finally:
        os.umask(old_umask)
        # Only remove the socket that this process created.
        if path.exists() and path.is_socket():
            path.unlink()


if __name__ == '__main__':
    main()
