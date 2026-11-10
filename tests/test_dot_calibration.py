from __future__ import annotations

import math
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from app.dot_calibration import (
    DotCalibrationError,
    _camera_for_observation,
    _project,
    _world_points_for_baseline,
    default_dot_calibration,
    normalise_dot_calibration,
    solve_dot_calibration,
    zoom_fov_for_count,
)
from app.state import StateStore
from app.tracking import default_profile


class SilentDebug:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def profile_fixture(pan_ratio=3.2, tilt_ratio=2.3, fov=52.17):
    profile = default_profile()
    profile["camera"]["fov"] = fov
    for axis, ratio in (("pan", pan_ratio), ("tilt", tilt_ratio)):
        profile["axes"][axis]["source"] = "quadrature_gpio"
        profile["axes"][axis]["mapping"]["encoder_revs_per_camera_rev"] = ratio
    return profile


def observation(oid, pan_raw=0, tilt_raw=0, zoom_count=None, points=None, refs=None, fov=52.17):
    return {
        "id": oid,
        "captured_mono": 0.0,
        "references": refs or {"pan": "P1", "tilt": "T1", "zoom": "Z1" if zoom_count is not None else None},
        "axes": {
            "pan": {"raw_count": pan_raw, "reference_count": 0, "reference_angle": 0.0},
            "tilt": {"raw_count": tilt_raw, "reference_count": 0, "reference_angle": 0.0},
            "zoom": {"relative_count": zoom_count},
        },
        "camera": {"x": 0.0, "y": 0.0, "z": 1.7, "height": 1.7, "roll": 0.0, "fov": fov},
        "points": points or {},
    }


BASE_POINTS = {
    "M1": {"x": 0.4, "y": 0.4},
    "M2": {"x": 0.6, "y": 0.4},
    "M3": {"x": 0.6, "y": 0.6},
    "M4": {"x": 0.4, "y": 0.6},
}


def target_with(observations, **updates):
    target = {
        "id": "target-1",
        "name": "Measured sign",
        "type": "free",
        "approx_distance_m": 20.0,
        "width_m": None,
        "height_m": None,
        "edge_midpoints": False,
        "markers": [{"id": marker_id, "label": marker_id} for marker_id in BASE_POINTS],
        "observations": observations,
        "model_points": {},
    }
    target.update(updates)
    return target


class DotCalibrationMathCase(unittest.TestCase):
    def test_default_and_normalisation_preserve_partial_marker_observations(self):
        self.assertEqual(default_dot_calibration(), {"targets": [], "active_target_id": None, "last_solution": None})
        data = {
            "targets": [target_with([
                observation("O1", points=BASE_POINTS),
                observation("O2", pan_raw=100, points={"M2": {"x": 0.51, "y": 0.48}}),
            ])],
            "active_target_id": "target-1",
        }
        normal = normalise_dot_calibration(data)
        self.assertEqual(set(normal["targets"][0]["observations"][1]["points"]), {"M2"})
        self.assertEqual(normal["active_target_id"], "target-1")

    def test_measured_rectangle_sets_metric_scale(self):
        profile = profile_fixture(3.6, 2.5, 60.0)
        baseline = observation("O1", points=BASE_POINTS, fov=60.0)
        camera = _camera_for_observation(baseline, profile, 3.6, 2.5, {}, 0.0)
        target = target_with(
            [baseline], type="rectangle", approx_distance_m=99.0,
            width_m=4.0, height_m=2.25,
        )
        world = _world_points_for_baseline(target, baseline, camera)

        def distance(a, b):
            return math.sqrt(sum((world[a][axis] - world[b][axis]) ** 2 for axis in ("x", "y", "z")))

        self.assertAlmostEqual(distance("M1", "M2"), 4.0, places=2)
        self.assertAlmostEqual(distance("M1", "M4"), 2.25, places=2)

    def test_solver_recovers_pan_tilt_ratios_from_single_marker_followup_views(self):
        profile = profile_fixture(3.2, 2.3)
        zoom = {"wide": None, "tight": None, "curvature": 0.0}
        true_pan, true_tilt = 3.6, 2.5
        baseline = observation("O1", points=BASE_POINTS, refs={"pan": "P1", "tilt": "T1", "zoom": None})
        baseline_camera = _camera_for_observation(baseline, profile, true_pan, true_tilt, zoom, 0.0)
        world = _world_points_for_baseline({"type": "free", "approx_distance_m": 20.0}, baseline, baseline_camera)
        observations = [baseline]
        settings = [("M1", 120, 50), ("M2", -150, -50), ("M3", 210, -70), ("M4", -220, 90)]
        for index, (marker_id, pan_raw, tilt_raw) in enumerate(settings, 2):
            obs = observation(f"O{index}", pan_raw, tilt_raw, refs={"pan": "P1", "tilt": "T1", "zoom": None})
            camera = _camera_for_observation(obs, profile, true_pan, true_tilt, zoom, 0.0)
            obs["points"] = {marker_id: _project(world[marker_id], camera)}
            observations.append(obs)
        dot_data = {"targets": [target_with(observations)], "active_target_id": "target-1", "last_solution": None}
        solved = solve_dot_calibration(profile, dot_data, zoom)
        # Both the baseline placements and their follow-up placements are
        # equal-authority measurements in the symmetric solver.
        self.assertEqual(solved["matched_points"], 8)
        self.assertEqual(solved["observation_sets"], 5)
        self.assertAlmostEqual(solved["proposed"]["pan_ratio"], true_pan, places=3)
        self.assertAlmostEqual(solved["proposed"]["tilt_ratio"], true_tilt, places=3)
        self.assertLess(solved["rms_after_px"], solved["rms_before_px"] * 0.01)


    def test_repeated_observations_drive_fit_independently_of_distance_hint(self):
        profile = profile_fixture(3.0, 2.0)
        zoom = {"wide": None, "tight": None, "curvature": 0.0}
        true_pan, true_tilt = 3.7, 2.6
        baseline = observation("O1", points=BASE_POINTS, refs={"pan": "P1", "tilt": "T1", "zoom": None})
        camera = _camera_for_observation(baseline, profile, true_pan, true_tilt, zoom, 0.0)
        world = _world_points_for_baseline({"type": "free", "approx_distance_m": 18.0}, baseline, camera)
        observations = [baseline]
        for index, (pan_raw, tilt_raw) in enumerate(((140, 55), (-180, -70), (260, 95)), 2):
            obs = observation(f"O{index}", pan_raw, tilt_raw, refs={"pan": "P1", "tilt": "T1", "zoom": None})
            camera = _camera_for_observation(obs, profile, true_pan, true_tilt, zoom, 0.0)
            obs["points"] = {marker_id: _project(point, camera) for marker_id, point in world.items()}
            observations.append(obs)
        near = solve_dot_calibration(profile, {"targets": [target_with(deepcopy(observations), approx_distance_m=2.0)], "active_target_id": "target-1"}, zoom)
        far = solve_dot_calibration(profile, {"targets": [target_with(deepcopy(observations), approx_distance_m=200.0)], "active_target_id": "target-1"}, zoom)
        self.assertAlmostEqual(near["proposed"]["pan_ratio"], true_pan, places=3)
        self.assertAlmostEqual(near["proposed"]["tilt_ratio"], true_tilt, places=3)
        self.assertAlmostEqual(near["proposed"]["pan_ratio"], far["proposed"]["pan_ratio"], places=6)
        self.assertAlmostEqual(near["proposed"]["tilt_ratio"], far["proposed"]["tilt_ratio"], places=6)

    def test_solving_same_observations_after_apply_is_stable(self):
        profile = profile_fixture(3.1, 2.2)
        zoom = {"wide": None, "tight": None, "curvature": 0.0}
        true_pan, true_tilt = 3.6, 2.5
        baseline = observation("O1", points=BASE_POINTS, refs={"pan": "P1", "tilt": "T1", "zoom": None})
        camera = _camera_for_observation(baseline, profile, true_pan, true_tilt, zoom, 0.0)
        world = _world_points_for_baseline({"type": "free", "approx_distance_m": 20.0}, baseline, camera)
        observations = [baseline]
        for index, (pan_raw, tilt_raw) in enumerate(((120, 60), (-170, -50), (240, 90)), 2):
            obs = observation(f"O{index}", pan_raw, tilt_raw, refs={"pan": "P1", "tilt": "T1", "zoom": None})
            camera = _camera_for_observation(obs, profile, true_pan, true_tilt, zoom, 0.0)
            obs["points"] = {marker_id: _project(point, camera) for marker_id, point in world.items()}
            observations.append(obs)
        data = {"targets": [target_with(observations)], "active_target_id": "target-1"}
        first = solve_dot_calibration(profile, data, zoom)
        profile["axes"]["pan"]["mapping"]["encoder_revs_per_camera_rev"] = first["proposed"]["pan_ratio"]
        profile["axes"]["tilt"]["mapping"]["encoder_revs_per_camera_rev"] = first["proposed"]["tilt_ratio"]
        second = solve_dot_calibration(profile, data, zoom)
        self.assertAlmostEqual(second["proposed"]["pan_ratio"], first["proposed"]["pan_ratio"], places=5)
        self.assertAlmostEqual(second["proposed"]["tilt_ratio"], first["proposed"]["tilt_ratio"], places=5)
        self.assertLess(second["rms_after_px"], 0.01)

    def test_solver_recovers_nonlinear_zoom_curve_without_moving_endpoints(self):
        profile = profile_fixture(3.6, 2.5)
        zoom = {
            "wide": {"relative_count": 0, "fov": 52.17, "reference_id": "Z1"},
            "tight": {"relative_count": 1000, "fov": 12.77, "reference_id": "Z1"},
            "curvature": 0.0,
        }
        true_curve = 0.8
        baseline = observation("O1", zoom_count=0, points=BASE_POINTS)
        baseline_camera = _camera_for_observation(baseline, profile, 3.6, 2.5, zoom, true_curve)
        world = _world_points_for_baseline({"type": "free", "approx_distance_m": 20.0}, baseline, baseline_camera)
        observations = [baseline]
        for index, zoom_count in enumerate((250, 500, 750), 2):
            obs = observation(f"O{index}", zoom_count=zoom_count)
            camera = _camera_for_observation(obs, profile, 3.6, 2.5, zoom, true_curve)
            obs["points"] = {marker_id: _project(point, camera) for marker_id, point in world.items()}
            observations.append(obs)
        solved = solve_dot_calibration(profile, {"targets": [target_with(observations)], "active_target_id": "target-1"}, zoom)
        self.assertEqual(solved["parameters"], ["zoom_curvature"])
        self.assertAlmostEqual(solved["proposed"]["zoom_curvature"], true_curve, places=3)
        self.assertAlmostEqual(zoom_fov_for_count(0, {**zoom, "curvature": true_curve}), 52.17, places=6)
        self.assertAlmostEqual(zoom_fov_for_count(1000, {**zoom, "curvature": true_curve}), 12.77, places=6)

    def test_insufficient_correspondence_data_is_rejected(self):
        profile = profile_fixture()
        dot_data = {"targets": [target_with([observation("O1", points=BASE_POINTS)])], "active_target_id": "target-1"}
        with self.assertRaisesRegex(DotCalibrationError, "more camera movement"):
            solve_dot_calibration(profile, dot_data, {"wide": None, "tight": None, "curvature": 0.0})


class DotCalibrationStateCase(unittest.IsolatedAsyncioTestCase):
    def make_store(self, directory):
        return StateStore(Path(directory), SilentDebug())

    def axis_sample(self, reference_id=None, **updates):
        sample = {
            "raw_count": 0, "configured": True, "driver_alive": True,
            "referenced": reference_id is not None,
            "reference_count": 0 if reference_id else None,
            "reference_angle": 0.0, "reference_id": reference_id, "events": 0,
        }
        sample.update(updates)
        return sample

    async def configure_angle_axes(self, store):
        profile_id = store.profile_index["active_id"]
        await store.update_axis_config(profile_id, "pan", {"source": "quadrature_gpio", "source_config": {"line_a": 17, "line_b": 27}})
        await store.update_axis_config(profile_id, "tilt", {"source": "quadrature_gpio", "source_config": {"line_a": 22, "line_b": 23}})

    async def test_overlay_is_session_only_but_saved_target_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            target = target_with([])
            await store.update_dot_calibration({"targets": [target], "active_target_id": target["id"]})
            await store.update_calibration_overlay({"enabled": True, "target_id": target["id"], "editing": True, "draft_points": {"M1": {"x": 0.4, "y": 0.4}}, "marker_scale": 3.0, "show_labels": False, "show_vectors": False})
            overlay = store.snapshot()["calibration_overlay"]
            self.assertTrue(overlay["enabled"])
            self.assertEqual(overlay["marker_scale"], 3.0)
            self.assertFalse(overlay["show_labels"])
            self.assertFalse(overlay["show_vectors"])
            reloaded = self.make_store(directory)
            self.assertFalse(reloaded.snapshot()["calibration_overlay"]["enabled"])
            self.assertEqual(reloaded.world_calibration["dot_calibration"]["targets"][0]["name"], "Measured sign")

    async def test_old_encoder_reference_observations_are_rejected_but_target_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            target = target_with([observation("O1", points=BASE_POINTS, refs={"pan": "OLD-P", "tilt": "OLD-T", "zoom": None})])
            await store.update_dot_calibration({"targets": [target], "active_target_id": target["id"]})
            with self.assertRaisesRegex(DotCalibrationError, "older encoder reference"):
                await store.solve_dot_calibration()
            self.assertEqual(len(store.world_calibration["dot_calibration"]["targets"]), 1)
            self.assertEqual(store.world_calibration["dot_calibration"]["targets"][0]["markers"][0]["id"], "M1")

    async def test_apply_refinement_preserves_live_encoder_reference_and_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_angle_axes(store)
            store.sample_tick({
                "pan": self.axis_sample("PAN-REF", raw_count=500, reference_count=100),
                "tilt": self.axis_sample("TILT-REF", raw_count=250, reference_count=50),
            })
            before = {axis: deepcopy(store.tracking["axes"][axis]) for axis in ("pan", "tilt")}
            target = target_with([])
            target["model_points"] = {"M1": {"x": 0.0, "y": 10.0, "z": 1.0}}
            refined_model = {"M1": {"x": 1.0, "y": 12.0, "z": 1.5}}
            data = {
                "targets": [target], "active_target_id": target["id"],
                "last_solution": {
                    "solved": True, "parameters": ["pan_ratio", "tilt_ratio"],
                    "proposed": {"pan_ratio": 3.6, "tilt_ratio": 2.5, "zoom_curvature": 0.0},
                    "target_models": {target["id"]: refined_model},
                },
            }
            await store.update_dot_calibration(data)
            self.assertEqual(store.world_calibration["dot_calibration"]["targets"][0]["model_points"]["M1"]["y"], 10.0)
            result = await store.apply_dot_calibration_solution()
            self.assertAlmostEqual(result["pan_ratio"], 3.6)
            self.assertAlmostEqual(result["tilt_ratio"], 2.5)
            for axis in ("pan", "tilt"):
                for key in ("raw_count", "reference_count", "reference_id", "driver_alive"):
                    self.assertEqual(store.tracking["axes"][axis][key], before[axis][key])
                self.assertTrue(store.tracking["axes"][axis]["referenced"])
            self.assertFalse(store.world_calibration["valid"])
            self.assertIn("DOT CALIBRATION APPLIED", store.world_calibration["status"])
            applied_model = store.world_calibration["dot_calibration"]["targets"][0]["model_points"]["M1"]
            self.assertEqual(applied_model, refined_model["M1"])


if __name__ == "__main__":
    unittest.main()
