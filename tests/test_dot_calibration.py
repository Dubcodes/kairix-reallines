from __future__ import annotations

import math
import tempfile
import unittest
from unittest.mock import patch
from copy import deepcopy
from pathlib import Path

from app.dot_calibration import (
    DotCalibrationError,
    _camera_for_observation,
    _project,
    _world_points_for_baseline,
    default_dot_calibration,
    normalise_dot_calibration,
    metric_rectangle_model_points,
    model_points_from_saved_observation,
    solve_dot_calibration,
    solve_dot_world_reference,
    zoom_fov_for_count,
)
from app.state import StateStore
from app.tracking import default_profile
from app.kinematics import camera_position


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
    def test_saved_anchor_is_pixel_stable_after_mapping_change(self):
        profile = profile_fixture(4.73, 2.43, 56.0)
        profile["camera"]["height"] = 1.9
        profile["camera"]["rig_offset"] = {"right_m": -0.015, "forward_m": -0.01, "up_m": 0.1}
        obs = observation("O-last", pan_raw=477, tilt_raw=80, points=BASE_POINTS, fov=77.6)
        target = target_with([obs], type="rectangle", width_m=1.3415, height_m=1.1685, approx_distance_m=5.715)
        model = model_points_from_saved_observation(target, obs, profile, {"active": False})
        pan = _camera_for_observation(obs, profile, 4.73, 2.43, {}, 0.0, horizontal_fov=56.0)
        optical = camera_position(
            {"x": 0.0, "y": 0.0, "z": 1.9}, pan["pan"], pan["tilt"], profile["camera"]["rig_offset"]
        )
        camera = {**optical, "pan": pan["pan"], "tilt": pan["tilt"], "roll": 0.0, "fov": 56.0}
        for marker_id, observed in BASE_POINTS.items():
            projected = _project(model[marker_id], camera)
            self.assertAlmostEqual(projected["x"], observed["x"], places=8)
            self.assertAlmostEqual(projected["y"], observed["y"], places=8)

    def test_internal_rectangle_marker_order_is_unchanged(self):
        points = metric_rectangle_model_points(target_with(
            [], type="rectangle", width_m=1.3415, height_m=1.1685, bottom_height_m=.3575
        ))
        self.assertLess(points["M1"]["x"], points["M2"]["x"])
        self.assertGreater(points["M3"]["x"], points["M4"]["x"])
        self.assertEqual(points["M3"]["z"], points["M4"]["z"])

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


    def test_bearing_only_pan_refinement_holds_horizontal_fov(self):
        # Regression for the real runaway where repeated pure-Pan observations
        # drove FOV toward 5 degrees while multiplying the Pan gear ratio. Dot
        # bearing consistency is not allowed to solve those two coupled terms.
        profile = profile_fixture(3.28, 2.21, 77.6)
        zoom = {"wide": {"relative_count": 0, "fov": 77.6, "reference_id": "Z1"}, "tight": None, "curvature": 0.0}
        true_pan, true_fov = 4.72, 56.0
        baseline = observation(
            "O1", pan_raw=60, tilt_raw=0, zoom_count=-20, points=BASE_POINTS,
            refs={"pan": "P1", "tilt": "T1", "zoom": "Z1"}, fov=77.6,
        )
        baseline_camera = _camera_for_observation(
            baseline, profile, true_pan, 2.21, zoom, 0.0, horizontal_fov=true_fov,
        )
        world = _world_points_for_baseline({"type": "free", "approx_distance_m": 12.0}, baseline, baseline_camera)
        observations = [baseline]
        for index, pan_raw in enumerate((310, -580, -5), 2):
            obs = observation(
                f"O{index}", pan_raw=pan_raw, tilt_raw=1, zoom_count=-20,
                refs={"pan": "P1", "tilt": "T1", "zoom": "Z1"}, fov=77.6,
            )
            camera = _camera_for_observation(
                obs, profile, true_pan, 2.21, zoom, 0.0, horizontal_fov=true_fov,
            )
            obs["points"] = {marker_id: _project(point, camera) for marker_id, point in world.items()}
            observations.append(obs)
        solved = solve_dot_calibration(
            profile, {"targets": [target_with(observations)], "active_target_id": "target-1"}, zoom,
        )
        self.assertIn("pan_ratio", solved["parameters"])
        self.assertNotIn("horizontal_fov", solved["parameters"])
        self.assertNotIn("tilt_ratio", solved["parameters"])
        self.assertAlmostEqual(solved["proposed"]["horizontal_fov"], 77.6, places=6)
        self.assertTrue(any("not solved from bearing-only" in message for message in solved["warnings"]))

    def test_large_ratio_jump_is_review_only(self):
        profile = profile_fixture(3.2, 2.3, 60.0)
        zoom = {"wide": None, "tight": None, "curvature": 0.0}
        true_pan = 5.5  # intentionally >25% away from the active mapping
        baseline = observation("O1", points=BASE_POINTS, refs={"pan": "P1", "tilt": "T1", "zoom": None}, fov=60.0)
        baseline_camera = _camera_for_observation(baseline, profile, true_pan, 2.3, zoom, 0.0, horizontal_fov=60.0)
        world = _world_points_for_baseline({"type": "free", "approx_distance_m": 15.0}, baseline, baseline_camera)
        observations = [baseline]
        for index, pan_raw in enumerate((350, -420, 650), 2):
            obs = observation(f"O{index}", pan_raw=pan_raw, points={}, refs={"pan": "P1", "tilt": "T1", "zoom": None}, fov=60.0)
            camera = _camera_for_observation(obs, profile, true_pan, 2.3, zoom, 0.0, horizontal_fov=60.0)
            obs["points"] = {marker_id: _project(point, camera) for marker_id, point in world.items()}
            observations.append(obs)
        solved = solve_dot_calibration(profile, {"targets": [target_with(observations)], "active_target_id": "target-1"}, zoom)
        self.assertFalse(solved["apply_allowed"])
        self.assertTrue(any("changes by" in blocker for blocker in solved["apply_blockers"]))

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

    def test_near_duplicate_observations_do_not_gain_extra_weight(self):
        profile = profile_fixture(3.3, 2.2, 60.0)
        zoom = {"wide": None, "tight": None, "curvature": 0.0}
        true_pan = 3.8
        baseline = observation("O1", pan_raw=0, tilt_raw=0, points=BASE_POINTS, refs={"pan": "P1", "tilt": "T1", "zoom": None}, fov=60.0)
        baseline_camera = _camera_for_observation(baseline, profile, true_pan, 2.2, zoom, 0.0, horizontal_fov=60.0)
        world = _world_points_for_baseline({"type": "free", "approx_distance_m": 15.0}, baseline, baseline_camera)
        second = observation("O2", pan_raw=350, tilt_raw=0, refs={"pan": "P1", "tilt": "T1", "zoom": None}, fov=60.0)
        second_camera = _camera_for_observation(second, profile, true_pan, 2.2, zoom, 0.0, horizontal_fov=60.0)
        second["points"] = {marker_id: _project(point, second_camera) for marker_id, point in world.items()}
        duplicate = deepcopy(second)
        duplicate["id"] = "O3"
        data = {"targets": [target_with([baseline, second, duplicate])], "active_target_id": "target-1"}
        solved = solve_dot_calibration(profile, data, zoom)
        baseline_solved = solve_dot_calibration(
            profile,
            {"targets": [target_with([deepcopy(baseline), deepcopy(second)])], "active_target_id": "target-1"},
            zoom,
        )
        self.assertEqual(solved["analysis"]["duplicate_observations"], 1)
        self.assertAlmostEqual(solved["proposed"]["pan_ratio"], baseline_solved["proposed"]["pan_ratio"], places=6)
        self.assertAlmostEqual(solved["proposed"]["horizontal_fov"], baseline_solved["proposed"]["horizontal_fov"], places=6)

    def test_measured_rectangle_world_reference_recovers_metric_camera_pose(self):
        profile = profile_fixture(4.6, 2.3, 56.0)
        profile["camera"]["height"] = 1.9
        profile["camera"]["rig_offset"] = {"right_m": -0.015, "forward_m": -0.01, "up_m": 0.1}
        zoom = {
            "wide": {"relative_count": 0, "fov": 56.0, "reference_id": "Z1"},
            "tight": {"relative_count": 725, "fov": 10.0, "reference_id": "Z1"},
            "curvature": -0.7,
        }
        target = target_with(
            [], type="rectangle", width_m=2.4, height_m=1.2,
            bottom_height_m=0.8, approx_distance_m=9.0,
        )
        target["markers"] = [{"id": f"M{index}", "label": f"M{index}"} for index in range(1, 5)]
        world_points = {
            "M1": {"x": -1.2, "y": 0.0, "z": 2.0},
            "M2": {"x": 1.2, "y": 0.0, "z": 2.0},
            "M3": {"x": 1.2, "y": 0.0, "z": 0.8},
            "M4": {"x": -1.2, "y": 0.0, "z": 0.8},
        }
        true_pivot = {"x": 1.2, "y": -8.5, "z": 1.7}
        true_pan_offset, true_tilt_offset = 7.0, -2.0
        for index, (pan_raw, tilt_raw) in enumerate(((0, 0), (350, 0), (-300, 0), (100, 180), (-200, -150)), 1):
            obs = observation(
                f"O{index}", pan_raw=pan_raw, tilt_raw=tilt_raw, zoom_count=0,
                refs={"pan": "P1", "tilt": "T1", "zoom": "Z1"}, fov=56.0,
            )
            world_pan = _camera_for_observation(obs, profile, 4.6, 2.3, zoom, -0.7)["pan"] + true_pan_offset
            world_tilt = _camera_for_observation(obs, profile, 4.6, 2.3, zoom, -0.7)["tilt"] + true_tilt_offset
            optical = camera_position(true_pivot, world_pan, world_tilt, profile["camera"]["rig_offset"])
            camera = {**optical, "pan": world_pan, "tilt": world_tilt, "roll": 0.0, "fov": 56.0}
            obs["points"] = {marker_id: _project(point, camera) for marker_id, point in world_points.items()}
            target["observations"].append(obs)
        solved = solve_dot_world_reference(profile, {"targets": [target], "active_target_id": target["id"]}, zoom)
        solution = solved["solution"]
        self.assertAlmostEqual(solution["pivot"]["x"], true_pivot["x"], places=3)
        self.assertAlmostEqual(solution["pivot"]["y"], true_pivot["y"], places=3)
        self.assertAlmostEqual(solution["pivot"]["z"], true_pivot["z"], places=3)
        self.assertAlmostEqual(solution["pan_offset"], true_pan_offset, places=3)
        self.assertAlmostEqual(solution["tilt_offset"], true_tilt_offset, places=3)
        self.assertAlmostEqual(solution["fixed_roll"], 0.0, places=3)
        self.assertLess(solution["rms_pixel_error"], 0.01)
        self.assertTrue(solution["precision_ready"])
        self.assertAlmostEqual(solved["target"]["center"]["z"], 1.4, places=6)

    def test_world_reference_ignores_historical_observation_fov(self):
        profile = profile_fixture(4.6, 2.3, 56.0)
        profile["camera"]["height"] = 1.9
        zoom = {"wide": None, "tight": None, "curvature": 0.0}
        target = target_with([], type="rectangle", width_m=2.0, height_m=1.0, bottom_height_m=0.4, approx_distance_m=8.0)
        world_points = {
            "M1": {"x": -1.0, "y": 0.0, "z": 1.4},
            "M2": {"x": 1.0, "y": 0.0, "z": 1.4},
            "M3": {"x": 1.0, "y": 0.0, "z": 0.4},
            "M4": {"x": -1.0, "y": 0.0, "z": 0.4},
        }
        true_pivot = {"x": 0.4, "y": -7.0, "z": 1.8}
        true_pan_offset, true_tilt_offset, true_roll = 4.0, -1.5, 1.25
        for index, (pan_raw, tilt_raw, stale_fov) in enumerate(((0, 0, 56.0), (300, 0, 14.54), (-260, 80, 5.0), (120, -100, 5.0)), 1):
            obs = observation(f"O{index}", pan_raw=pan_raw, tilt_raw=tilt_raw, refs={"pan": "P1", "tilt": "T1", "zoom": None}, fov=stale_fov)
            tracking = _camera_for_observation(obs, profile, 4.6, 2.3, zoom, 0.0, horizontal_fov=56.0)
            world_pan = tracking["pan"] + true_pan_offset
            world_tilt = tracking["tilt"] + true_tilt_offset
            optical = camera_position(true_pivot, world_pan, world_tilt, profile["camera"]["rig_offset"])
            camera = {**optical, "pan": world_pan, "tilt": world_tilt, "roll": true_roll, "fov": 56.0}
            obs["points"] = {marker_id: _project(point, camera) for marker_id, point in world_points.items()}
            target["observations"].append(obs)
        solved = solve_dot_world_reference(profile, {"targets": [target], "active_target_id": target["id"]}, zoom)
        solution = solved["solution"]
        self.assertLess(solution["rms_pixel_error"], 0.05)
        self.assertAlmostEqual(solution["fixed_roll"], true_roll, places=2)
        self.assertAlmostEqual(solution["pivot"]["y"], true_pivot["y"], places=2)

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
            await store.update_calibration_overlay({
                "enabled": True, "target_id": target["id"], "editing": True,
                "draft_points": {"M1": {"x": 0.4, "y": 0.4}},
                "reference_points": {"M1": {"x": 0.41, "y": 0.42}},
                "reference_observation_id": "O1",
                "marker_scale": 3.0, "show_labels": False, "show_vectors": False,
                "ground_grid_calibration": True, "ground_grid_gfx": True,
                "grid_spacing_m": 0.5, "grid_extent_m": 40.0, "grid_major_every": 4,
            })
            overlay = store.snapshot()["calibration_overlay"]
            self.assertTrue(overlay["enabled"])
            self.assertEqual(overlay["marker_scale"], 3.0)
            self.assertFalse(overlay["show_labels"])
            self.assertFalse(overlay["show_vectors"])
            self.assertEqual(overlay["reference_points"]["M1"], {"x": 0.41, "y": 0.42})
            self.assertEqual(overlay["reference_observation_id"], "O1")
            self.assertTrue(overlay["ground_grid_calibration"])
            self.assertTrue(overlay["ground_grid_gfx"])
            self.assertEqual(overlay["grid_spacing_m"], 0.5)
            self.assertEqual(overlay["grid_extent_m"], 40.0)
            self.assertEqual(overlay["grid_major_every"], 4)
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

    async def test_measured_rectangle_world_solution_becomes_active_world_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_angle_axes(store)
            store.sample_tick({
                "pan": self.axis_sample("PAN-REF", raw_count=100, reference_count=0),
                "tilt": self.axis_sample("TILT-REF", raw_count=50, reference_count=0),
            })
            target = target_with(
                [
                    observation("O1", points=BASE_POINTS, refs={"pan": "PAN-REF", "tilt": "TILT-REF", "zoom": None}),
                    observation("O2", pan_raw=100, points=BASE_POINTS, refs={"pan": "PAN-REF", "tilt": "TILT-REF", "zoom": None}),
                ],
                type="rectangle", width_m=2.0, height_m=1.0, bottom_height_m=0.5,
            )
            await store.update_dot_calibration({"targets": [target], "active_target_id": target["id"]})
            fake = {
                "solution": {
                    "solved": True,
                    "pivot": {"x": 1.0, "y": -8.0, "z": 1.8},
                    "camera": {"x": 1.0, "y": -8.0, "z": 1.8},
                    "pan_offset": 5.0,
                    "tilt_offset": -2.0,
                    "rig_offset": deepcopy(store.profile["camera"]["rig_offset"]),
                    "fixed_roll": 0.0,
                    "horizontal_fov": 60.0,
                    "observation_count": 8,
                    "view_count": 2,
                    "rms_pixel_error": 2.0,
                    "max_pixel_error": 3.0,
                    "converged": True,
                    "precision_ready": True,
                    "target_source": target["id"],
                    "target_name": target["name"],
                    "solved_at": "2026-11-11T00:00:00+00:00",
                },
                "target": {
                    "center": {"x": 0.0, "y": 0.0, "z": 1.0},
                    "width": 2.0,
                    "height": 1.0,
                    "yaw": 0.0,
                    "plane": "vertical_rectangle",
                },
            }
            with patch("app.state.solve_dot_world_reference", return_value=fake):
                result = await store.solve_dot_world_reference(target["id"])
            self.assertTrue(store.world_calibration["valid"])
            self.assertIn("DOT TARGET WORLD CALIBRATION VALID", store.world_calibration["status"])
            self.assertEqual(store.world_calibration["solution"]["pivot"]["y"], -8.0)
            self.assertEqual(result["target"]["center"]["z"], 1.0)
            self.assertEqual(store.world_calibration["tracking_reference_identity"]["pan"], "PAN-REF")
            self.assertEqual(store.world_calibration["tracking_reference_identity"]["tilt"], "TILT-REF")
            snapshot = store.snapshot()
            self.assertTrue(snapshot["camera"]["world_valid"])
            world_model = store.world_calibration["dot_calibration"]["targets"][0]["model_points"]
            self.assertEqual(world_model, metric_rectangle_model_points(target))

    async def test_poor_world_solution_is_not_marked_valid_for_output(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_angle_axes(store)
            store.sample_tick({
                "pan": self.axis_sample("PAN-REF", raw_count=100),
                "tilt": self.axis_sample("TILT-REF", raw_count=50),
            })
            target = target_with(
                [
                    observation("O1", points=BASE_POINTS, refs={"pan": "PAN-REF", "tilt": "TILT-REF", "zoom": None}),
                    observation("O2", pan_raw=100, points=BASE_POINTS, refs={"pan": "PAN-REF", "tilt": "TILT-REF", "zoom": None}),
                ],
                type="rectangle", width_m=2.0, height_m=1.0, bottom_height_m=0.5,
            )
            await store.update_dot_calibration({"targets": [target], "active_target_id": target["id"]})
            fake = {
                "solution": {
                    "solved": True, "converged": True, "precision_ready": False,
                    "pivot": {"x": 1.0, "y": -8.0, "z": 1.8},
                    "camera": {"x": 1.0, "y": -8.0, "z": 1.8},
                    "pan_offset": 5.0, "tilt_offset": -2.0,
                    "rig_offset": deepcopy(store.profile["camera"]["rig_offset"]),
                    "fixed_roll": 0.0, "horizontal_fov": 60.0,
                    "observation_count": 8, "view_count": 2,
                    "rms_pixel_error": 7.1, "max_pixel_error": 15.0,
                    "target_source": target["id"], "target_name": target["name"],
                    "solved_at": "2026-11-11T00:00:00+00:00",
                },
                "target": {"center": {"x": 0.0, "y": 0.0, "z": 1.0}, "width": 2.0, "height": 1.0, "yaw": 0.0, "plane": "vertical_rectangle"},
            }
            before_world = deepcopy(store.world_calibration)
            with patch("app.state.solve_dot_world_reference", return_value=fake):
                outcome = await store.solve_dot_world_reference(target["id"])
            self.assertFalse(outcome["valid"])
            self.assertIn("NOT APPLIED", outcome["status"])
            self.assertEqual(store.world_calibration, before_world)

    async def test_apply_refinement_preserves_live_encoder_reference_and_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_angle_axes(store)
            profile_id = store.profile_index["active_id"]
            await store.update_axis_config(profile_id, "zoom", {"source": "quadrature_gpio", "source_config": {"line_a": 5, "line_b": 6}})
            store.sample_tick({
                "pan": self.axis_sample("PAN-REF", raw_count=500, reference_count=100),
                "tilt": self.axis_sample("TILT-REF", raw_count=250, reference_count=50),
                "zoom": self.axis_sample("ZOOM-REF", raw_count=-20, reference_count=0),
            })
            before = {axis: deepcopy(store.tracking["axes"][axis]) for axis in ("pan", "tilt")}
            target = target_with([])
            target["model_points"] = {"M1": {"x": 0.0, "y": 10.0, "z": 1.0}}
            refined_model = {"M1": {"x": 1.0, "y": 12.0, "z": 1.5}}
            data = {
                "targets": [target], "active_target_id": target["id"],
                "last_solution": {
                    "solved": True, "converged": True, "apply_allowed": True, "apply_blockers": [],
                    "parameters": ["pan_ratio", "tilt_ratio"],
                    "current": {
                        "pan_ratio": store.profile["axes"]["pan"]["mapping"]["encoder_revs_per_camera_rev"],
                        "tilt_ratio": store.profile["axes"]["tilt"]["mapping"]["encoder_revs_per_camera_rev"],
                        "horizontal_fov": store.profile["camera"]["fov"],
                        "zoom_curvature": 0.0,
                    },
                    "proposed": {"pan_ratio": 3.6, "tilt_ratio": 2.5, "horizontal_fov": store.profile["camera"]["fov"], "zoom_curvature": 0.0},
                    "solve_context": {
                        "references": {"pan": "PAN-REF", "tilt": "TILT-REF", "zoom": "ZOOM-REF"},
                        "pan_ratio": store.profile["axes"]["pan"]["mapping"]["encoder_revs_per_camera_rev"],
                        "tilt_ratio": store.profile["axes"]["tilt"]["mapping"]["encoder_revs_per_camera_rev"],
                        "horizontal_fov": store.profile["camera"]["fov"],
                        "zoom_curvature": 0.0,
                    },
                    "target_models": {target["id"]: refined_model},
                },
            }
            store.zoom_fov_mapping.update({
                "wide": {"relative_count": 0, "fov": 77.6, "reference_id": "ZOOM-REF"},
                "tight": None, "curvature": 0.0,
            })
            await store.update_dot_calibration(data)
            self.assertEqual(store.world_calibration["dot_calibration"]["targets"][0]["model_points"]["M1"]["y"], 10.0)
            result = await store.apply_dot_calibration_solution()
            self.assertAlmostEqual(result["pan_ratio"], 3.6)
            self.assertAlmostEqual(result["tilt_ratio"], 2.5)
            self.assertAlmostEqual(result["horizontal_fov"], store.profile["camera"]["fov"])
            self.assertAlmostEqual(store.zoom_fov_mapping["wide"]["fov"], 77.6)
            self.assertEqual(store.zoom_fov_mapping["wide"]["relative_count"], 0)
            self.assertEqual(store.zoom_fov_mapping["wide"]["reference_id"], "ZOOM-REF")
            for axis in ("pan", "tilt"):
                for key in ("raw_count", "reference_count", "reference_id", "driver_alive"):
                    self.assertEqual(store.tracking["axes"][axis][key], before[axis][key])
                self.assertTrue(store.tracking["axes"][axis]["referenced"])
            self.assertFalse(store.world_calibration["valid"])
            self.assertIn("DOT CALIBRATION APPLIED", store.world_calibration["status"])
            applied_model = store.world_calibration["dot_calibration"]["targets"][0]["model_points"]["M1"]
            self.assertEqual(applied_model, refined_model["M1"])

    async def test_apply_preserves_gold_positions_in_latest_stationary_view(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_angle_axes(store)
            store.sample_tick({
                "pan": self.axis_sample("PAN-REF", raw_count=500, reference_count=0),
                "tilt": self.axis_sample("TILT-REF", raw_count=0, reference_count=0),
            })
            first = observation("O1", pan_raw=100, points=BASE_POINTS,
                                refs={"pan": "PAN-REF", "tilt": "TILT-REF", "zoom": None})
            latest = observation("O2", pan_raw=500, points=BASE_POINTS,
                                 refs={"pan": "PAN-REF", "tilt": "TILT-REF", "zoom": None})
            target = target_with([first, latest], type="rectangle", width_m=1.3415,
                                 height_m=1.1685, approx_distance_m=5.715)
            base = store.profile["axes"]["pan"]["mapping"]["encoder_revs_per_camera_rev"]
            await store.update_dot_calibration({
                "targets": [target], "active_target_id": target["id"],
                "last_solution": {
                    "solved": True, "apply_allowed": True, "parameters": ["pan_ratio"],
                    "current": {"pan_ratio": base},
                    "proposed": {"pan_ratio": base * 1.02},
                    "solve_context": {"references": {"pan": "PAN-REF",
                                     "tilt": "TILT-REF", "zoom": None}},
                    "target_models": {target["id"]: {"M1": {"x": 1.0, "y": 50.0, "z": 4.0}}},
                },
            })
            await store.apply_dot_calibration_solution()
            model = store.world_calibration["dot_calibration"]["targets"][0]["model_points"]
            for marker_id, gold in latest["points"].items():
                projected = _project(model[marker_id], store.camera)
                self.assertAlmostEqual(projected["x"], gold["x"], places=7)
                self.assertAlmostEqual(projected["y"], gold["y"], places=7)

    async def test_review_only_refinement_cannot_be_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_angle_axes(store)
            store.sample_tick({
                "pan": self.axis_sample("PAN-REF", raw_count=100),
                "tilt": self.axis_sample("TILT-REF", raw_count=50),
            })
            target = target_with([])
            data = {
                "targets": [target], "active_target_id": target["id"],
                "last_solution": {
                    "solved": True, "apply_allowed": False,
                    "apply_blockers": ["The proposal only improves RMS by 0.11 px (1.10%)"],
                    "parameters": ["pan_ratio"],
                    "current": {"pan_ratio": store.profile["axes"]["pan"]["mapping"]["encoder_revs_per_camera_rev"]},
                    "proposed": {"pan_ratio": 53.0},
                },
            }
            await store.update_dot_calibration(data)
            before = store.profile["axes"]["pan"]["mapping"]["encoder_revs_per_camera_rev"]
            with self.assertRaisesRegex(DotCalibrationError, "review-only"):
                await store.apply_dot_calibration_solution()
            self.assertEqual(store.profile["axes"]["pan"]["mapping"]["encoder_revs_per_camera_rev"], before)


if __name__ == "__main__":
    unittest.main()
