"""Read-only owner readiness/health check for systemd and field diagnostics.

Owner readiness is distinct from position referencing: the owner starts
unconfigured, then Uvicorn sends the full calibration model through IPC.
"""
from __future__ import annotations

import argparse
import json
import time

from .pico_proxy import PicoOwnerUnavailable, rpc


def probe(socket_path: str, *, deadline: float, require_configured: bool = False, now=time.monotonic, pause=time.sleep):
    error = 'owner socket not ready'
    while True:
        try:
            result = rpc(socket_path, {'action': 'health'}, timeout=0.3)
            if not isinstance(result, dict) or 'configured' not in result:
                raise ValueError('owner health reply missing configured flag')
            if not require_configured or result['configured'] is True:
                return result
            error = 'owner listening but not configured by web service'
        except (PicoOwnerUnavailable, ValueError, KeyError, TypeError) as exc:
            error = str(exc)
        if now() >= deadline:
            raise RuntimeError(error)
        pause(0.1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', default='/run/kairix-reallines/owner.sock')
    parser.add_argument('--wait', type=float, default=0.0, help='Seconds to wait for owner readiness')
    parser.add_argument('--configured', action='store_true', help='Also require web configuration acknowledgement')
    args = parser.parse_args(argv)
    try:
        result = probe(args.socket, deadline=time.monotonic() + max(args.wait, 0.0),
                       require_configured=args.configured)
    except RuntimeError as exc:
        parser.exit(1, f'Tracking owner not ready: {exc}\n')
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
