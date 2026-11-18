import tempfile
import unittest
from pathlib import Path

from app.native_pose import HEADER, HEADER_SIZE, SLOT, SLOT_COUNT, LatestPoseReader, LatestPoseWriter, NativePoseError


class NativePoseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "pose.bin"
        self.now = [2_000_000_000]
        self.writer = LatestPoseWriter(self.path, clock_ns=lambda: self.now[0], producer_id=7)
        self.writer.open()

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def publish(self, pan=1.0):
        return self.writer.publish(
            {"x": 1, "y": 2, "z": 3, "pan": pan, "tilt": 4, "roll": 5,
             "fov": 60, "valid": True, "world_valid": True},
            {"valid": True, "integrity": "TRUSTED"}, acquisition_ns=self.now[0] - 1_000_000,
            pico_sequence=99, pico_timestamp_us=1234,
            model_identity=("cal-a",), reference_identity=("pan-a", "tilt-a"),
        )

    def test_coherent_latest_snapshot_and_generations(self):
        self.publish()
        reader = LatestPoseReader(self.path, clock_ns=lambda: self.now[0])
        reader.open()
        pose = reader.read()
        self.assertEqual((pose["sequence"], pose["pan"], pose["pico_sequence"]), (1, 1.0, 99))
        self.assertTrue(pose["tracking_valid"])
        self.assertFalse(pose["stale"])
        first_model = pose["model_generation"]
        self.now[0] += 5_000_000
        self.publish(2.0)
        pose = reader.read()
        self.assertEqual(pose["model_generation"], first_model)
        self.assertEqual(pose["sequence"], 2)
        reader.close()

    def test_torn_snapshot_is_rejected(self):
        self.publish()
        generation = self.writer.generation
        offset = HEADER_SIZE + (generation % SLOT_COUNT) * SLOT.size
        self.writer._map[offset:offset + 8] = (999).to_bytes(8, "little")
        reader = LatestPoseReader(self.path)
        reader.open()
        with self.assertRaisesRegex(NativePoseError, "torn"):
            reader.read()
        reader.close()

    def test_protocol_version_mismatch_is_rejected(self):
        self.writer._map[8:12] = (999).to_bytes(4, "little")
        reader = LatestPoseReader(self.path)
        with self.assertRaisesRegex(NativePoseError, "protocol"):
            reader.open()

    def test_slow_consumer_gets_latest_without_queue_growth(self):
        for index in range(SLOT_COUNT * 3):
            self.now[0] += 1_000_000
            self.publish(float(index))
        reader = LatestPoseReader(self.path, clock_ns=lambda: self.now[0])
        reader.open()
        self.assertEqual(reader.read()["sequence"], SLOT_COUNT * 3)
        history = reader.latest_history(SLOT_COUNT * 2)
        self.assertEqual(len(history), SLOT_COUNT)
        self.assertEqual(history[-1]["pan"], float(SLOT_COUNT * 3 - 1))
        reader.close()

    def test_restart_and_stale_pose_are_explicit(self):
        self.publish()
        reader = LatestPoseReader(self.path, stale_seconds=.1, clock_ns=lambda: self.now[0])
        reader.open()
        self.assertFalse(reader.read()["producer_restart"])
        self.writer.close()
        replacement = LatestPoseWriter(self.path, clock_ns=lambda: self.now[0], producer_id=8)
        try:
            replacement.open()
            replacement.publish({"valid": False}, {"valid": False}, acquisition_ns=self.now[0])
            self.assertTrue(reader.read()["producer_restart"])
            self.now[0] += 200_000_000
            self.assertTrue(reader.read()["stale"])
            reader.close()
        finally:
            replacement.close()


if __name__ == "__main__":
    unittest.main()
