import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from app.measured_targets import (
    default_measured_targets, duplicate_measured_target, new_measured_target,
    normalise_measured_targets, validate_measured_target,
)
from app.state import StateStore


class SilentDebug:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class MeasuredTargetMathCase(unittest.TestCase):
    def test_all_unique_pairs_generated_and_unknowns_remain_null(self):
        target = new_measured_target(marker_count=4)
        self.assertEqual(len(target["measurements"]), 6)
        self.assertTrue(all(item["distance_m"] is None for item in target["measurements"]))
        self.assertEqual(len({tuple(sorted((item["a"], item["b"]))) for item in target["measurements"]}), 6)

    def test_round_trip_heights_ground_slants_and_duplicate(self):
        target = new_measured_target(marker_count=3)
        target["markers"][0].update({"height_m": 0, "ground_reference": True})
        target["markers"][1]["height_m"] = 1.2
        target["measurements"][0].update({"distance_m": 2.5, "constraint_type": "ground_slant"})
        data = normalise_measured_targets({"version": 1, "active_target_id": target["id"], "targets": [target]})
        self.assertEqual(data["targets"][0]["markers"][0]["height_m"], 0.0)
        self.assertEqual(data["targets"][0]["measurements"][0]["constraint_type"], "ground_slant")
        duplicate = duplicate_measured_target(data["targets"][0])
        self.assertNotEqual(duplicate["id"], target["id"])
        self.assertEqual(duplicate["measurements"], data["targets"][0]["measurements"])

    def test_validation_reports_triangle_height_and_underconstraint(self):
        target = new_measured_target(marker_count=4)
        for marker, height in zip(target["markers"], (0, 3, None, None)):
            marker["height_m"] = height
        values = {("M1", "M2"): 2, ("M1", "M3"): 1, ("M2", "M3"): 1}
        for measurement in target["measurements"]:
            measurement["distance_m"] = values.get((measurement["a"], measurement["b"]))
        result = validate_measured_target(target)
        self.assertFalse(result["valid"])
        self.assertIn("height_distance_conflict", {issue["code"] for issue in result["issues"]})
        self.assertIn("triangle_conflict", {issue["code"] for issue in result["issues"]})
        self.assertIn("underconstrained", {issue["code"] for issue in result["issues"]})

    def test_invalid_values_and_ground_slant_without_ground_marker_rejected(self):
        target = new_measured_target(marker_count=2)
        target["measurements"][0]["distance_m"] = 0
        with self.assertRaisesRegex(ValueError, "positive"):
            normalise_measured_targets({"targets": [target]})
        target["measurements"][0].update({"distance_m": 1, "constraint_type": "ground_slant"})
        with self.assertRaisesRegex(ValueError, "ground marker"):
            normalise_measured_targets({"targets": [target]})


class MeasuredTargetPersistenceCase(unittest.IsolatedAsyncioTestCase):
    async def test_separate_atomic_persistence_never_changes_manual_dot_calibration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory), SilentDebug())
            world_path = store._world_calibration_path(store.world_calibration_index["active_id"])
            before = world_path.read_bytes()
            target = new_measured_target(marker_count=4)
            result = await store.update_measured_targets({"version": 1, "active_target_id": target["id"], "targets": [target]})
            self.assertEqual(result["targets"][0]["id"], target["id"])
            self.assertEqual(world_path.read_bytes(), before)
            saved = json.loads((Path(directory) / "measured_targets.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 1)
            self.assertNotIn("validation", saved)
            reloaded = StateStore(Path(directory), SilentDebug())
            self.assertEqual(reloaded.measured_targets, saved)
