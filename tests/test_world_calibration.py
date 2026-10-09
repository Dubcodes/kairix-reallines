import json
import math
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from app.state import StateStore
from app.world_calibration import CalibrationError, TARGET_POINTS, default_world_calibration, solve_world_calibration, synthetic_observations, target_points, tracking_angles, world_angles


class SilentDebug:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class GeometryCase(unittest.TestCase):
    def test_world_vectors_angles_and_offsets(self):
        camera = {"x": 0, "y": 0, "z": 1}
        self.assertEqual(world_angles(camera, {"x": 0, "y": 10, "z": 1}), (0.0, 0.0))
        pan, tilt = world_angles(camera, {"x": 10, "y": 10, "z": 11})
        self.assertAlmostEqual(pan, 45.0)
        self.assertAlmostEqual(tilt, math.degrees(math.atan2(10, math.sqrt(200))))
        observed = tracking_angles(camera, {"x": 10, "y": 10, "z": 11}, 5, -3)
        self.assertAlmostEqual(observed[0], pan - 5)
        self.assertAlmostEqual(observed[1], tilt + 3)

    def solve_case(self, camera, target_y=20.0, offsets=(5.0, -3.0)):
        calibration = default_world_calibration("Synthetic", "profile")
        calibration["target"]["center"]["y"] = target_y
        calibration["camera_hint"] = {"x": camera["x"] + 0.4, "y": camera["y"] - 0.5, "z": camera["z"] + 0.2}
        calibration["orientation_hint"] = {"pan_offset": offsets[0] - 1, "tilt_offset": offsets[1] + 1}
        calibration["observations"] = synthetic_observations(calibration, camera, *offsets)
        result = solve_world_calibration(calibration)
        for axis in ("x", "y", "z"):
            self.assertAlmostEqual(result["camera"][axis], camera[axis], places=5)
        self.assertAlmostEqual(result["pan_offset"], offsets[0], places=5)
        self.assertAlmostEqual(result["tilt_offset"], offsets[1], places=5)
        self.assertLess(result["rms_angular_error"], 1e-6)

    def test_solver_multiple_ground_truth_cases(self):
        for camera, distance in (({"x": 0, "y": 0, "z": 1.8}, 20), ({"x": 3, "y": 0, "z": 1.8}, 20), ({"x": -2, "y": 1, "z": 3.2}, 20), ({"x": 1, "y": -2, "z": 1.1}, 45)):
            with self.subTest(camera=camera, distance=distance):
                self.solve_case(camera, distance)

    def test_small_noise_converges_with_measurable_residual(self):
        calibration = default_world_calibration("Noisy", "profile")
        camera = {"x": 3.0, "y": 0.0, "z": 1.8}
        calibration["camera_hint"] = {"x": 2.7, "y": -0.3, "z": 1.7}
        noise = {name: pair for name, pair in zip(TARGET_POINTS, ((0.03, -0.02), (-0.02, 0.01), (0.01, 0.03), (-0.03, -0.01)))}
        calibration["observations"] = synthetic_observations(calibration, camera, 4.0, -2.0, noise)
        result = solve_world_calibration(calibration)
        self.assertLess(abs(result["camera"]["x"] - camera["x"]), 0.2)
        self.assertLess(abs(result["camera"]["z"] - camera["z"]), 0.2)
        self.assertGreater(result["rms_angular_error"], 0.001)
        self.assertLess(result["rms_angular_error"], 0.1)

    def test_bad_and_degenerate_inputs_are_rejected(self):
        calibration = default_world_calibration("Bad", "profile")
        camera = {"x": 3.0, "y": 0.0, "z": 1.8}
        with self.assertRaises(CalibrationError):
            solve_world_calibration(calibration)
        calibration["target"]["width"] = 0
        calibration["observations"] = synthetic_observations(default_world_calibration("Source", "profile"), camera, 0, 0)
        with self.assertRaises(CalibrationError):
            solve_world_calibration(calibration)
        calibration = default_world_calibration("Duplicate", "profile")
        one = synthetic_observations(calibration, camera, 0, 0)["top_left"]
        calibration["observations"] = {name: deepcopy(one) for name in TARGET_POINTS}
        with self.assertRaises(CalibrationError):
            solve_world_calibration(calibration)
        calibration = default_world_calibration("NaN", "profile")
        calibration["observations"] = synthetic_observations(calibration, camera, 0, 0)
        calibration["observations"]["top_left"]["mapped_pan"] = float("nan")
        with self.assertRaises(CalibrationError):
            solve_world_calibration(calibration)

    def test_grossly_incorrect_mark_fails_quality_threshold(self):
        calibration = default_world_calibration("Gross error", "profile")
        camera = {"x": 3.0, "y": 0.0, "z": 1.8}
        calibration["camera_hint"] = deepcopy(camera)
        calibration["observations"] = synthetic_observations(calibration, camera, 5, -3)
        calibration["observations"]["top_left"]["mapped_pan"] += 12
        with self.assertRaises(CalibrationError):
            solve_world_calibration(calibration)


class WorldCalibrationStoreCase(unittest.IsolatedAsyncioTestCase):
    def make_store(self, directory):
        return StateStore(Path(directory), SilentDebug())

    async def test_synthetic_solution_updates_camera_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            result = await store.generate_synthetic_world_calibration({"camera": {"x": 3, "y": 0, "z": 1.8}, "pan_offset": 5, "tilt_offset": -3})
            self.assertTrue(store.world_calibration["valid"])
            self.assertTrue(store.camera["world_valid"])
            self.assertAlmostEqual(store.camera["x"], 3, places=5)
            self.assertAlmostEqual(store.camera["pan"], store.tracking["axes"]["pan"]["value"] + 5, places=5)
            calibration_id = store.world_calibration_index["active_id"]
            reloaded = self.make_store(directory)
            self.assertEqual(reloaded.world_calibration_index["active_id"], calibration_id)
            self.assertTrue(reloaded.world_calibration["valid"])
            self.assertAlmostEqual(reloaded.world_calibration["solution"]["camera"]["x"], result["camera"]["x"])

    async def test_profile_mismatch_is_visible_and_invalidates_world_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await store.generate_synthetic_world_calibration({"camera": {"x": 0, "y": 0, "z": 1.8}})
            await store.create_profile("Other Hardware")
            snapshot = store.snapshot()
            self.assertFalse(snapshot["world_calibrations"]["profile_match"])
            self.assertFalse(snapshot["camera"]["world_valid"])
            self.assertEqual(snapshot["camera"]["x"], 0)

    async def test_failed_solve_preserves_previous_good_solution(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await store.generate_synthetic_world_calibration({"camera": {"x": 3, "y": 0, "z": 1.8}, "pan_offset": 5, "tilt_offset": -3})
            good = deepcopy(store.world_calibration["solution"])
            store.world_calibration["observations"]["top_left"]["mapped_pan"] += 12
            with self.assertRaises(CalibrationError):
                await store.solve_active_world_calibration()
            self.assertEqual(store.world_calibration["solution"], good)
            self.assertFalse(store.world_calibration["valid"])

    async def test_mark_records_tracking_and_world_storage_does_not_replace_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = {"pan_direction": 1, "tilt_direction": -1, "marks": {"pan_left": 10}}
            (root / "calibration.json").write_text(json.dumps(legacy))
            store = self.make_store(directory)
            await store.set_camera({"pan": 12.5, "tilt": -4.5})
            mark = await store.mark_world_target("top_left")
            self.assertEqual(mark["raw_pan"], 12.5)
            self.assertEqual(mark["mapped_tilt"], 4.5)
            self.assertEqual(mark["setup_profile_id"], store.profile_index["active_id"])
            self.assertTrue((root / "calibration.json").exists())
            self.assertTrue((root / "world_calibrations" / "index.json").exists())

    async def test_multiple_named_calibrations_select_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            original = store.world_calibration_index["active_id"]
            created = await store.create_world_calibration("Trackside Two")
            self.assertEqual(store.world_calibration["name"], "Trackside Two")
            await store.select_world_calibration(original)
            self.assertNotEqual(store.world_calibration_index["active_id"], created["id"])


if __name__ == "__main__":
    unittest.main()
