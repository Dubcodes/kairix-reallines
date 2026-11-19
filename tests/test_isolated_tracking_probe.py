import asyncio
import tempfile
import unittest
from pathlib import Path

from app.isolated_tracking_probe import (
    ShadowPico, SilentDebug, load_shadow_store, run_shadow,
    summarize_intervals, validate_pose_path,
)
from app.native_pose import LatestPoseReader, LatestPoseWriter


class IsolatedTrackingProbeTests(unittest.TestCase):
    def test_refuses_live_shared_memory_path(self):
        with self.assertRaises(ValueError):
            validate_pose_path(Path('/dev/shm/kairix-reallines-pose-v1'))

    def test_probe_samples_are_cumulative_and_referenced(self):
        pico = ShadowPico()
        pico.tick(5.0, 1.1)
        samples = pico.snapshots(5.002)
        self.assertEqual(set(samples), {'pan', 'tilt', 'zoom'})
        self.assertTrue(all(v['referenced'] for v in samples.values()))
        self.assertEqual(samples['pan']['filtered_count'], samples['pan']['raw_count'])
        self.assertEqual(samples['pan']['packet_seq'], pico.sequence)
        self.assertAlmostEqual(samples['pan']['packet_age_ms'], 2.0)

    def test_stats_are_bounded(self):
        from collections import deque
        stats = summarize_intervals(deque([0.0, 1.0, 2.0, 3.0]))
        self.assertEqual(stats['samples'], 4)
        self.assertEqual(stats['max_ms'], 3.0)

    def test_separate_pose_writer_from_synthetic_tracking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = load_shadow_store(root / 'empty_setup', root / 'scratch')
            writer = LatestPoseWriter(root / 'shadow.bin')
            writer.open()
            try:
                summary = asyncio.run(run_shadow(
                    store, ShadowPico(), writer, seconds=.15,
                ))
                reader = LatestPoseReader(root / 'shadow.bin')
                reader.open()
                try:
                    pose = reader.read()
                    self.assertTrue(pose['tracking_valid'])
                    self.assertTrue(pose['integrity_trusted'])
                    self.assertGreater(summary['pose_writes'], 5)
                    self.assertEqual(summary['mode'], 'shadow_simulated')
                    self.assertEqual(pose['sequence'], summary['pose_writes'])
                    self.assertIsInstance(pose['pan'], float)
                finally:
                    reader.close()
            finally:
                writer.close()


if __name__ == '__main__':
    unittest.main()
