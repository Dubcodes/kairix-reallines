import tempfile
import unittest
from pathlib import Path

from app.history import PoseHistory, PoseSample, interpolate_angle
from app.state import StateStore


class SilentDebug:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def sample(timestamp, pan, valid=True, profile="profile", calibration="calibration"):
    return PoseSample(
        timestamp=timestamp,
        tracking_pan=pan,
        tracking_tilt=pan / 2,
        pan=pan,
        tilt=pan / 2,
        x=timestamp,
        y=2 * timestamp,
        z=1.8,
        roll=0,
        fov=60,
        valid=valid,
        world_valid=valid,
        profile_id=profile,
        calibration_id=calibration,
    )


class PoseHistoryCase(unittest.TestCase):
    def test_basic_delay_and_exact_sample(self):
        history = PoseHistory(5)
        for timestamp, pan in ((0.0, 0), (0.1, 10), (0.2, 20)):
            history.append(sample(timestamp, pan))
        result = history.query(0.15)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["mode"], "INTERPOLATED")
        self.assertAlmostEqual(result["pose"]["pan"], 15)
        self.assertAlmostEqual(result["pose"]["x"], 0.15)
        exact = history.query(0.1)
        self.assertEqual(exact["mode"], "EXACT")
        self.assertEqual(exact["pose"]["pan"], 10)

    def test_pan_wrap_uses_shortest_arc(self):
        self.assertAlmostEqual(abs(interpolate_angle(179, -179, 0.5)), 180)
        self.assertAlmostEqual(interpolate_angle(359, 1, 0.5), 0)
        history = PoseHistory(5)
        history.append(sample(0, 179))
        history.append(sample(1, -179))
        self.assertAlmostEqual(abs(history.query(0.5)["pose"]["pan"]), 180)

    def test_irregular_intervals_use_timestamps(self):
        history = PoseHistory(5)
        for milliseconds in (0, 7, 21, 34, 58):
            history.append(sample(milliseconds / 1000, milliseconds))
        self.assertAlmostEqual(history.query(0.0275)["pose"]["pan"], 27.5)

    def test_underrun_invalid_region_and_static_latest(self):
        history = PoseHistory(5)
        history.append(sample(1.0, 0))
        history.append(sample(1.1, 10, valid=False))
        history.append(sample(1.2, 20))
        self.assertEqual(history.query(0.9)["status"], "HISTORY_UNDERRUN")
        self.assertEqual(history.query(1.05)["status"], "INVALID")
        self.assertEqual(history.query(1.15)["status"], "INVALID")
        latest = history.query(8.0)
        self.assertEqual(latest["status"], "OK")
        self.assertEqual(latest["mode"], "LATEST_STATIC")
        self.assertEqual(latest["pose"]["pan"], 20)

    def test_reference_mismatch_is_never_interpolated(self):
        history = PoseHistory(5)
        history.append(sample(0, 0, profile="a"))
        history.append(sample(1, 10, profile="b"))
        self.assertEqual(history.query(0.5)["status"], "INVALID")

    def test_buffer_is_duration_bounded(self):
        history = PoseHistory(2.1)
        for index in range(101):
            history.append(sample(index / 10, index))
        self.assertGreaterEqual(history.samples[0].timestamp, 7.8)
        self.assertEqual(history.samples[-1].timestamp, 10)


class HistoryStateCase(unittest.IsolatedAsyncioTestCase):
    def make_store(self, directory):
        return StateStore(Path(directory), SilentDebug())

    async def test_delay_zero_and_persistent_configuration_without_persistent_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await store.set_camera({"pan": 10})
            await store.set_camera({"pan": 20})
            self.assertEqual(store.snapshot()["render_camera"]["pan"], 20)
            self.assertEqual(store.pose_history.generation, 0)
            self.assertGreater(len(store.pose_history.samples), 1)
            await store.update_sync_config({"graphics_delay_ms": 120})
            reloaded = self.make_store(directory)
            self.assertEqual(reloaded.sync_config["graphics_delay_ms"], 120)
            self.assertEqual(len(reloaded.pose_history.samples), 1)

    async def test_profile_switch_creates_history_discontinuity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await store.set_camera({"pan": 10})
            generation = store.pose_history.generation
            await store.create_profile("Different Rig")
            self.assertGreater(store.pose_history.generation, generation)
            self.assertEqual(len(store.pose_history.samples), 1)
            self.assertEqual(store.pose_history.samples[0].profile_id, store.profile_index["active_id"])

    async def test_world_calibration_switch_creates_history_discontinuity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            generation = store.pose_history.generation
            created = await store.create_world_calibration("Second Calibration")
            self.assertGreater(store.pose_history.generation, generation)
            self.assertEqual(len(store.pose_history.samples), 1)
            self.assertEqual(store.pose_history.samples[0].calibration_id, created["id"])

    async def test_calibration_mark_uses_live_tracking_not_render_pose(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await store.set_camera({"pan": 20, "tilt": 4})
            now = store.pose_history.samples[-1].timestamp
            profile = store.profile_index["active_id"]
            calibration = store.world_calibration_index["active_id"]
            store.pose_history.reset("test fixture")
            store.pose_history.append(sample(now - 0.2, 0, profile=profile, calibration=calibration))
            store.pose_history.append(sample(now, 20, profile=profile, calibration=calibration))
            await store.update_sync_config({"graphics_delay_ms": 100})
            snapshot = store.snapshot()
            self.assertLess(snapshot["render_camera"]["pan"], 15)
            mark = await store.mark_world_target("top_left")
            self.assertEqual(mark["mapped_pan"], 20)
            self.assertEqual(mark["mapped_tilt"], 4)


if __name__ == "__main__":
    unittest.main()
