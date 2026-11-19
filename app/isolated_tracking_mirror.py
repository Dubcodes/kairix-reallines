"""Real-UART shadow tracking benchmark, without competing for the serial device.

Requires KAIRIX_PICO_MIRROR_SOCKET on the *existing* service. Its Pico reader
non-blockingly sends copies of raw serial chunks over a Unix datagram socket.
This separate process decodes and maps those chunks, using independent
SESSION-LOCAL SHADOW references. It NEVER controls the real Pico, edits the
live setup data, or writes the live pose file. NOT a production GFX producer.

Run on the Pi: python -m app.isolated_tracking_mirror --seconds 60
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import socket
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any

from .isolated_tracking_probe import (
    DEFAULT_SHADOW_PATH, SilentDebug, copy_setup, load_shadow_store,
    summarize_intervals, validate_pose_path,
)
from .native_pose import LatestPoseWriter
from .pico_protocol import PicoPacket, PicoStreamDecoder

DEFAULT_MIRROR_SOCKET = Path('/dev/shm/kairix-pico-shadow.sock')
AXES = ('pan', 'tilt', 'zoom')
STALE_SECONDS = 0.35


class MirrorSource:
    """Decode raw serial bytes in a separate process; retain newest packet only."""

    def __init__(self) -> None:
        self.decoder = PicoStreamDecoder()
        self.last_packet: PicoPacket | None = None
        self.last_arrival: float | None = None
        self.reference_counts: dict[str, int] = {}
        self.reference_ids: dict[str, str] = {}
        self.session_generation = 0
        self.packet_count = 0
        self.sequence_gaps = 0
        self.session_restarts = 0
        self.datagrams = 0
        self.last_session: int | None = None
        self.previous_sequence: int | None = None
        self.press_counters: tuple[int, int, int] = (0, 0, 0)
        self.min_counts: dict[str, int] = {}
        self.max_counts: dict[str, int] = {}
        self.movement_events: dict[str, int] = {axis: 0 for axis in AXES}

    def feed(self, data: bytes, arrival: float | None = None) -> int:
        now = time.monotonic() if arrival is None else float(arrival)
        self.datagrams += 1
        accepted = 0
        for packet in self.decoder.feed(data):
            previous_session = self.last_session
            if self.last_session != packet.session_id:
                if self.last_session is not None:
                    self.session_restarts += 1
                self.session_generation += 1
                self.last_session = packet.session_id
                self.reference_counts = {axis: int(getattr(packet, axis)) for axis in AXES}
                self.reference_ids = {
                    axis: f'shadow-{packet.session_id}-{self.session_generation}-{axis}' for axis in AXES
                }
                self.previous_sequence = None
            if self.previous_sequence is not None:
                diff = (packet.sequence - self.previous_sequence) & 0xFFFFFFFF
                if diff == 0 or diff >= 0x80000000:
                    continue
                if diff > 1:
                    self.sequence_gaps += diff - 1
            if self.last_packet is not None and previous_session == packet.session_id:
                for axis in AXES:
                    if getattr(self.last_packet, axis) != getattr(packet, axis):
                        self.movement_events[axis] += 1
            for axis in AXES:
                count = int(getattr(packet, axis))
                self.min_counts[axis] = min(count, self.min_counts.get(axis, count))
                self.max_counts[axis] = max(count, self.max_counts.get(axis, count))
            self.previous_sequence = packet.sequence
            self.last_packet = packet
            self.last_arrival = now
            self.press_counters = packet.button_presses
            self.packet_count += 1
            accepted += 1
        return accepted

    def snapshots(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        now = time.monotonic() if now is None else float(now)
        packet = self.last_packet
        age = max(0.0, now - self.last_arrival) if self.last_arrival is not None else float('inf')
        fresh = packet is not None and age <= STALE_SECONDS
        result = {}
        for axis in AXES:
            count = int(getattr(packet, axis)) if packet else 0
            result[axis] = {
                'raw_count': count, 'input_raw_count': count, 'filtered_count': count,
                'configured': True, 'driver_alive': fresh,
                'referenced': fresh and axis in self.reference_counts,
                'reference_count': self.reference_counts.get(axis),
                'reference_angle': 0.0, 'reference_id': self.reference_ids.get(axis),
                'sample_mono': self.last_arrival,
                'availability': 'AVAILABLE' if fresh else 'UNAVAILABLE',
                'integrity': 'TRUSTED' if fresh else 'UNKNOWN',
                'integrity_reason': None,
                'calibration_confidence': 'TRUSTED' if fresh else 'UNREFERENCED',
                'integrity_policy': 'strict',
                'health': 'VALID' if fresh else 'STALE',
                'status': 'Shadow-local reference, NOT operator calibrated',
                'pico_session': packet.session_id if packet else None,
                'packet_seq': packet.sequence if packet else None,
                'pico_timestamp_us': packet.pico_us if packet else None,
                'packet_age_ms': age * 1000.0 if math.isfinite(age) else None,
                'packet_gaps': self.sequence_gaps,
                'crc_errors': self.decoder.crc_errors,
            }
        return result


async def run_mirror(store, source: MirrorSource, writer: LatestPoseWriter, receiver: socket.socket,
                     *, seconds: float = 60.0, output_rate_hz: int = 200) -> dict[str, Any]:
    if not math.isfinite(seconds) or seconds <= 0 or not 20 <= output_rate_hz <= 1000:
        raise ValueError('Invalid runtime or output rate')
    receiver.setblocking(False)
    loop = asyncio.get_running_loop()
    input_gaps = deque(maxlen=40000)
    last_input_at: float | None = None
    bounded_drain_exhaustions = 0

    def drain() -> None:
        nonlocal last_input_at, bounded_drain_exhaustions
        for _ in range(128):
            try:
                chunk = receiver.recv(8192)
            except BlockingIOError:
                return
            except OSError:
                return
            now = time.monotonic()
            if last_input_at is not None:
                input_gaps.append((now - last_input_at) * 1000.0)
            last_input_at = now
            source.feed(chunk, now)
        bounded_drain_exhaustions += 1

    loop.add_reader(receiver.fileno(), drain)
    start = time.monotonic()
    target = start
    interval = 1.0 / output_rate_hz
    lateness = deque(maxlen=40000)
    execution = deque(maxlen=40000)
    sample_age = deque(maxlen=40000)
    ticks = late_deadlines = stale_ticks = 0
    try:
        while True:
            now = time.monotonic()
            if now - start >= seconds:
                break
            if now >= target:
                lag_ms = max(0.0, (now - target) * 1000.0)
                lateness.append(lag_ms)
                if lag_ms >= interval * 1000.0:
                    late_deadlines += 1
                begin = time.perf_counter()
                samples = source.snapshots(now)
                store.sample_tick(samples, now)
                writer.publish_store(store, samples, now)
                execution.append((time.perf_counter() - begin) * 1000.0)
                if source.last_arrival is not None:
                    sample_age.append((now - source.last_arrival) * 1000.0)
                if not store.tracking.get('valid'):
                    stale_ticks += 1
                ticks += 1
                # Skip missed deadlines rather than replaying obsolete poses.
                target += max(1, int((time.monotonic() - target) / interval) + 1) * interval
            await asyncio.sleep(max(0.0, min(target, start + seconds) - time.monotonic()))
    finally:
        loop.remove_reader(receiver.fileno())
    elapsed = time.monotonic() - start
    return {
        'mode': 'real_uart_shadow_mirror',
        'note': 'This is NOT live operator referencing or a production graphics producer',
        'elapsed_seconds': round(elapsed, 3),
        'target_pose_hz': output_rate_hz,
        'observed_pose_hz': round(ticks / max(elapsed, 1e-9), 3),
        'pose_writes': ticks,
        'missed_wakeup_deadlines': late_deadlines,
        'invalid_or_stale_ticks': stale_ticks,
        'serial_datagrams_received': source.datagrams,
        'pico_packets_decoded': source.packet_count,
        'pico_sequence_gaps_in_mirror': source.sequence_gaps,
        'crc_errors_in_mirror': source.decoder.crc_errors,
        'pico_session_restarts_in_mirror': source.session_restarts,
        'encoder_count_span': {axis: source.max_counts[axis] - source.min_counts[axis]
                               for axis in source.min_counts},
        'movement_events': dict(source.movement_events),
        'socket_drain_limit_hits': bounded_drain_exhaustions,
        'wake_lateness': summarize_intervals(lateness),
        'pose_processing_time': summarize_intervals(execution),
        'sample_age_ms': summarize_intervals(sample_age),
        'socket_interarrival_ms': summarize_intervals(input_gaps),
        'final_tracking_valid': bool(store.tracking.get('valid')),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=60.0)
    parser.add_argument('--pose-rate', type=int, default=200)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    parser.add_argument('--pose-file', type=Path, default=DEFAULT_SHADOW_PATH)
    parser.add_argument('--socket', type=Path, default=DEFAULT_MIRROR_SOCKET)
    args = parser.parse_args(argv)
    if sys.platform != 'linux':
        parser.error('Unix datagram socket / fcntl runner is Linux-only; unit tests run on Windows')
    import fcntl
    path = validate_pose_path(args.pose_file)
    sock_path = args.socket.expanduser().absolute()
    if sock_path == path or sock_path == Path('/dev/serial0') or sock_path == Path('/dev/shm/kairix-reallines-pose-v1'):
        parser.error('Unsafe mirror socket path')
    if sock_path.is_symlink() or not sock_path.parent.is_dir():
        parser.error('Mirror socket must be a non-symlink path with an existing parent')
    lock_path = path.with_name(path.name + '.lock')
    with lock_path.open('a+b') as locked:
        try:
            fcntl.flock(locked.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit('Shadow pose file is already owned by another producer') from exc
        if sock_path.exists():
            parser.error(f'Mirror socket path already exists: {sock_path}; do not remove an active socket')
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            listener.bind(str(sock_path))
            os.chmod(sock_path, 0o600)
            scratch_parent = Path('/dev/shm') if Path('/dev/shm').is_dir() else None
            with tempfile.TemporaryDirectory(prefix='kairix-shadow-', dir=scratch_parent) as scratch:
                store = load_shadow_store(args.data_dir, Path(scratch))
                writer = LatestPoseWriter(path)
                try:
                    writer.open()
                    result = asyncio.run(run_mirror(store, MirrorSource(), writer, listener,
                                                    seconds=args.seconds, output_rate_hz=args.pose_rate))
                finally:
                    writer.close()
            print(json.dumps(result, indent=2))
            print(f'Shadow pose: {path} (NOT used by live GFX or HDMI)')
        finally:
            listener.close()
            try:
                sock_path.unlink()
            except FileNotFoundError:
                pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
