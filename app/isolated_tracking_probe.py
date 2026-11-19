"""Independent tracking-process proof for RealLines (SHADOW ONLY).

Does not open UART, change live references, serve HTTP, or write the live pose mmap.
Loads a temporary copy of setup data and exercises the existing StateStore
tracking/pose mathematics at 200 Hz with synthetic 500 Hz cumulative counts.

Run: python -m app.isolated_tracking_probe --seconds 30
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import math
import os
import shutil
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any

from .native_pose import LatestPoseWriter, LatestPoseReader, NativePoseError
from .state import StateStore

LIVE_POSE_PATH = Path('/dev/shm/kairix-reallines-pose-v1')
DEFAULT_SHADOW_PATH = Path('/dev/shm/kairix-reallines-shadow-pose-v1')
CONFIG_FILES = (
    'calibration.json', 'engineering.json', 'pico_processing.json',
    'measured_targets.json', 'zoom_lens_profiles.json', 'sync.json',
    'scene.json', 'imu.json',
)
CONFIG_DIRS = ('layouts', 'profiles', 'world_calibrations')
AXES = ('pan', 'tilt', 'zoom')


class SilentDebug:
    """Keep shadow ticks out of production logs and SD-card writes."""
    def __getattr__(self, name: str):
        def noop(*args: Any, **kwargs: Any) -> None:
            return None
        return noop


def validate_pose_path(path: Path, live_path: Path = LIVE_POSE_PATH) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError('Shadow pose path must not be a symlink')
    path = path.resolve()
    if path == live_path.resolve() or (path.exists() and live_path.exists() and os.path.samefile(path, live_path)):
        raise ValueError('Refusing to overwrite live RealLines pose shared memory')
    if not path.parent.is_dir():
        raise ValueError('Shadow pose parent directory does not exist')
    return path


def copy_setup(source: Path, target: Path) -> None:
    """Copy just configuration into an isolated temporary directory.

    No assets, debug sessions, or camera media are copied. Missing files are
    created by StateStore ONLY inside the temporary directory.
    """
    source = source.resolve()
    target.mkdir(parents=True, exist_ok=True)
    for name in CONFIG_FILES:
        path = source / name
        if path.is_file() and not path.is_symlink():
            shutil.copy2(path, target / name)
    for name in CONFIG_DIRS:
        folder = source / name
        if folder.is_dir() and not folder.is_symlink():
            shutil.copytree(folder, target / name, ignore=shutil.ignore_patterns('*.tmp'))


class ShadowPico:
    """Synthetic cumulative-count snapshots, never touches /dev/serial0."""
    def __init__(self, *, input_rate_hz: int = 500) -> None:
        self.rate = int(input_rate_hz)
        self.sequence = 0
        self.counts = {axis: 0 for axis in AXES}
        self.sample_mono = time.monotonic()
        self.pico_timestamp_us = 0
        self.session = 0x53484457  # SHDW; reserved for synthetic evidence only

    def tick(self, now: float, elapsed: float) -> None:
        self.sequence += 1
        self.sample_mono = now
        self.pico_timestamp_us = int(elapsed * 1_000_000)
        # Deliberately quantised to emulate PIO integer counts, including
        # reversals; no smoothing or intentional buffering.
        self.counts['pan'] = round(750 * math.sin(2 * math.pi * elapsed / 7.0))
        self.counts['tilt'] = round(180 * math.sin(2 * math.pi * elapsed / 11.0))
        self.counts['zoom'] = 0

    def snapshots(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        now = time.monotonic() if now is None else now
        age = max(0.0, now - self.sample_mono)
        fresh = age <= .35
        return {
            axis: {
                'raw_count': count, 'input_raw_count': count, 'filtered_count': count,
                'configured': True, 'driver_alive': fresh, 'referenced': fresh,
                'reference_count': 0, 'reference_angle': 0.0,
                'reference_id': f'shadow-{axis}-reference',
                'sample_mono': self.sample_mono,
                'availability': 'AVAILABLE' if fresh else 'UNAVAILABLE',
                'integrity': 'TRUSTED' if fresh else 'UNKNOWN',
                'integrity_reason': None,
                'calibration_confidence': 'TRUSTED' if fresh else 'UNREFERENCED',
                'integrity_policy': 'strict',
                'health': 'VALID' if fresh else 'STALE',
                'status': 'Synthetic position (shadow process)',
                'pico_session': self.session,
                'packet_seq': self.sequence,
                'pico_timestamp_us': self.pico_timestamp_us,
                'packet_age_ms': age * 1000,
            }
            for axis, count in self.counts.items()
        }


def load_shadow_store(data_dir: Path, scratch_dir: Path) -> StateStore:
    copy_setup(data_dir, scratch_dir)
    store = StateStore(scratch_dir, SilentDebug())
    # Only the scratch copy is altered. Use existing encoder mapping code even
    # if the original profile is configured for a simulator.
    for axis in AXES:
        store.profile['axes'][axis]['source'] = 'pico_uart'
    store._refresh_tracking_from_profile(initial=True)
    store.runtime_profiler = None
    store.stability_recorder = None
    store.native_pose_writer = None
    return store


def summarize_intervals(values: deque[float]) -> dict[str, float | int | None]:
    if not values:
        return {'samples': 0, 'p95_ms': None, 'p99_ms': None, 'max_ms': None}
    ordered = sorted(values)
    def percentile(q: float) -> float:
        return round(ordered[max(0, math.ceil(len(ordered) * q) - 1)], 3)
    return {'samples': len(values), 'p95_ms': percentile(.95),
            'p99_ms': percentile(.99), 'max_ms': round(ordered[-1], 3)}


async def run_shadow(
    store: StateStore, pico: ShadowPico, writer: LatestPoseWriter,
    *, seconds: float, output_rate_hz: int = 200,
) -> dict[str, Any]:
    """One independently scheduled producer loop for synthetic counts + pose.

    Counts are generated according to an absolute 500-Hz clock, then mapping
    takes the newest completed count at each 200-Hz deadline. No accumulated
    queue is replayed after a late tick.
    """
    if seconds <= 0 or not math.isfinite(seconds):
        raise ValueError('seconds must be finite and greater than zero')
    if not 20 <= output_rate_hz <= 1000 or not 20 <= pico.rate <= 2000:
        raise ValueError('unsupported synthetic rate')
    started = time.monotonic()
    next_output = started
    next_input = started
    input_period = 1.0 / pico.rate
    output_period = 1.0 / output_rate_hz
    output_lateness: deque[float] = deque(maxlen=40000)
    execution: deque[float] = deque(maxlen=40000)
    missed = input_ticks = output_ticks = 0
    while True:
        now = time.monotonic()
        if now - started >= seconds:
            break
        # Latest-only synthetic input. If the process was delayed, generate
        # one present-time count, with a sequence reflecting elapsed ticks.
        if now >= next_input:
            intervals = max(1, int((now - next_input) / input_period) + 1)
            pico.sequence += intervals - 1
            pico.tick(now, now - started)
            input_ticks += intervals
            next_input += intervals * input_period
        if now >= next_output:
            late_ms = max(0.0, (now - next_output) * 1000.0)
            output_lateness.append(late_ms)
            if late_ms >= output_period * 1000:
                missed += 1
            begin = time.perf_counter()
            samples = pico.snapshots(now)
            store.sample_tick(samples, now)
            writer.publish_store(store, samples, now)
            execution.append((time.perf_counter() - begin) * 1000.0)
            output_ticks += 1
            # Skip missed deadlines; do not burst obsolete poses to catch up.
            next_output += max(1, int((time.monotonic() - next_output) / output_period) + 1) * output_period
        await asyncio.sleep(max(0.0, min(next_input, next_output, started + seconds) - time.monotonic()))
    return {
        'mode': 'shadow_simulated', 'seconds': round(time.monotonic() - started, 3),
        'synthetic_input_intervals': input_ticks,
        'pose_writes': output_ticks, 'missed_pose_deadlines': missed,
        'target_input_hz': pico.rate, 'target_pose_hz': output_rate_hz,
        'observed_pose_hz': round(output_ticks / max(.001, time.monotonic() - started), 2),
        'wake_lateness': summarize_intervals(output_lateness),
        'pose_processing_time': summarize_intervals(execution),
        'final_valid': bool(store.tracking.get('valid')),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=30.0)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    parser.add_argument('--pose-file', type=Path, default=DEFAULT_SHADOW_PATH)
    parser.add_argument('--input-rate', type=int, default=500)
    parser.add_argument('--pose-rate', type=int, default=200)
    args = parser.parse_args(argv)
    path = validate_pose_path(args.pose_file)
    lock_path = path.with_name(path.name + '.lock')
    # Lock before creating/truncating the shared memory file.
    with lock_path.open('a+b') as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit('A shadow producer already owns this pose path') from exc
        scratch_parent = Path('/dev/shm') if Path('/dev/shm').is_dir() and os.access('/dev/shm', os.W_OK) else None
        with tempfile.TemporaryDirectory(prefix='kairix-shadow-', dir=scratch_parent) as scratch:
            store = load_shadow_store(args.data_dir, Path(scratch))
            writer = LatestPoseWriter(path)
            try:
                writer.open()
                result = asyncio.run(run_shadow(
                    store, ShadowPico(input_rate_hz=args.input_rate), writer,
                    seconds=args.seconds, output_rate_hz=args.pose_rate,
                ))
            finally:
                writer.close()
    import json
    print(json.dumps(result, indent=2))
    print(f'Shadow pose: {path} (NOT used by live GFX or HDMI)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
