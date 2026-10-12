import json
import tempfile
import time
import unittest
from pathlib import Path

from app.state import StateStore
from app.tracking import TrackingConfigError, counts_per_camera_revolution, default_axis_config, degrees_per_count, map_raw_value, merge_axis_update, normalise_axis_config


class SilentDebug:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class MappingCase(unittest.TestCase):
    def test_standard_pan_and_tilt_gpio_defaults_are_distinct(self):
        pan = default_axis_config("pan")
        tilt = default_axis_config("tilt")

        self.assertEqual(pan["source"], "simulator")
        self.assertEqual(tilt["source"], "simulator")
        self.assertEqual(
            {key: pan["source_config"][key] for key in ("stale_timeout_seconds", "chip", "line_a", "line_b", "bias")},
            {"stale_timeout_seconds": 2.0, "chip": "/dev/gpiochip0", "line_a": 17, "line_b": 27, "bias": "as_is"},
        )
        self.assertEqual(
            {key: tilt["source_config"][key] for key in ("stale_timeout_seconds", "chip", "line_a", "line_b", "bias")},
            {"stale_timeout_seconds": 2.0, "chip": "/dev/gpiochip0", "line_a": 22, "line_b": 23, "bias": "as_is"},
        )
        self.assertEqual({pan["source_config"]["line_a"], pan["source_config"]["line_b"]} & {tilt["source_config"]["line_a"], tilt["source_config"]["line_b"]}, set())
        for config in (pan, tilt):
            self.assertEqual(config["mapping"]["ppr"], 600)
            self.assertEqual(config["mapping"]["quadrature_multiplier"], 4)
            self.assertEqual(config["mapping"]["encoder_revs_per_camera_rev"], 1.0)
            self.assertEqual(config["mapping"]["direction"], 1)
            self.assertEqual(config["mapping"]["offset"], 0.0)

    def test_normalisation_preserves_saved_custom_gpio_mapping(self):
        custom = {
            "source": "quadrature_gpio",
            "source_config": {"chip": "/dev/gpiochip4", "line_a": 9, "line_b": 10, "bias": "pull_up"},
        }
        normalised = normalise_axis_config("pan", custom)
        self.assertEqual(normalised["source_config"]["chip"], "/dev/gpiochip4")
        self.assertEqual(normalised["source_config"]["line_a"], 9)
        self.assertEqual(normalised["source_config"]["line_b"], 10)
        self.assertEqual(normalised["source_config"]["bias"], "pull_up")

    def test_normalisation_upgrades_only_legacy_empty_gpio_placeholders(self):
        legacy = {"source": "simulator", "source_config": {"chip": "", "line_a": None, "line_b": None, "bias": "as_is"}}
        self.assertEqual(normalise_axis_config("pan", legacy)["source_config"]["line_a"], 17)
        self.assertEqual(normalise_axis_config("tilt", legacy)["source_config"]["line_a"], 22)

    def test_encoder_mapping_known_values_direction_and_offset(self):
        config = default_axis_config("pan", "quadrature_gpio")
        config["mapping"].update({"ppr": 600, "quadrature_multiplier": 4, "encoder_revs_per_camera_rev": 5, "direction": 1, "offset": 0})
        self.assertEqual(counts_per_camera_revolution(config), 12000)
        self.assertAlmostEqual(degrees_per_count(config), 0.03)
        self.assertAlmostEqual(map_raw_value("pan", 100, config), 3.0)
        config["mapping"].update({"direction": -1, "offset": 10})
        self.assertAlmostEqual(map_raw_value("pan", 100, config), 7.0)

    def test_linux_rotary_defaults_and_fixed_x4_validation(self):
        pan = default_axis_config("pan", "linux_rotary")
        tilt = default_axis_config("tilt", "linux_rotary")
        self.assertEqual(
            {key: pan["source_config"][key] for key in ("discovery", "rel_axis", "pin_a", "pin_b", "steps_per_period")},
            {"discovery": "auto", "rel_axis": "REL_X", "pin_a": 17, "pin_b": 27, "steps_per_period": 4},
        )
        self.assertEqual(
            {key: tilt["source_config"][key] for key in ("discovery", "rel_axis", "pin_a", "pin_b", "steps_per_period")},
            {"discovery": "auto", "rel_axis": "REL_Y", "pin_a": 22, "pin_b": 23, "steps_per_period": 4},
        )
        selected = merge_axis_update("pan", default_axis_config("pan", "quadrature_gpio"), {
            "source": "linux_rotary", "mapping": {"quadrature_multiplier": 1},
        })
        self.assertEqual(selected["mapping"]["quadrature_multiplier"], 4)
        pan["mapping"]["quadrature_multiplier"] = 2
        with self.assertRaisesRegex(TrackingConfigError, "requires quadrature multiplier"):
            normalise_axis_config("pan", pan)

    def test_linux_rotary_source_switch_atomically_normalises_only_multiplier(self):
        for multiplier in (1, 2, 4):
            current = default_axis_config("pan", "quadrature_gpio")
            current["mapping"].update({
                "ppr": 1234,
                "quadrature_multiplier": multiplier,
                "encoder_revs_per_camera_rev": 9.5,
                "direction": -1,
                "offset": 12.25,
            })
            expected_mapping = {**current["mapping"], "quadrature_multiplier": 4}
            selected = merge_axis_update("pan", current, {"source": "linux_rotary"})
            self.assertEqual(selected["source"], "linux_rotary")
            self.assertEqual(selected["mapping"], expected_mapping)

        gpio = merge_axis_update("pan", selected, {"source": "quadrature_gpio"})
        self.assertEqual(gpio["mapping"]["quadrature_multiplier"], 4)

    def test_invalid_linux_rotary_multiplier_is_rejected_after_source_switch(self):
        current = default_axis_config("pan", "quadrature_gpio")
        rotary = merge_axis_update("pan", current, {"source": "linux_rotary"})
        with self.assertRaisesRegex(TrackingConfigError, "requires quadrature multiplier"):
            merge_axis_update("pan", rotary, {"mapping": {"quadrature_multiplier": 1}})

    def test_invalid_encoder_config_is_rejected(self):
        current = default_axis_config("pan", "quadrature_gpio")
        for values in ({"ppr": 0}, {"quadrature_multiplier": 3}, {"encoder_revs_per_camera_rev": 0}, {"direction": 0}):
            with self.assertRaises(TrackingConfigError):
                merge_axis_update("pan", current, {"mapping": values})
        with self.assertRaises(TrackingConfigError):
            merge_axis_update("pan", current, {"source_config": {"stale_timeout_seconds": 0}})


class TrackingStoreCase(unittest.IsolatedAsyncioTestCase):
    def make_store(self, path):
        return StateStore(Path(path), SilentDebug())

    async def test_clean_install_default_profile_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            self.assertEqual(store.profile["name"], "Development Simulator")
            self.assertEqual(store.profile["axes"]["pan"]["source"], "simulator")
            self.assertEqual(store.profile["axes"]["focus"]["source"], "disabled")
            self.assertTrue(store.tracking["valid"])
            profile_id = store.profile_index["active_id"]
            reloaded = self.make_store(directory)
            self.assertEqual(reloaded.profile_index["active_id"], profile_id)
            self.assertEqual(reloaded.profile["name"], "Development Simulator")

    async def test_saved_custom_gpio_mapping_survives_store_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            profile_id = store.profile_index["active_id"]
            await store.update_axis_config(profile_id, "pan", {
                "source": "quadrature_gpio",
                "source_config": {"chip": "/dev/gpiochip4", "line_a": 9, "line_b": 10, "bias": "pull_up"},
            })

            reloaded = self.make_store(directory)
            source = reloaded.profile["axes"]["pan"]["source_config"]
            self.assertEqual((source["chip"], source["line_a"], source["line_b"], source["bias"]), ("/dev/gpiochip4", 9, 10, "pull_up"))

    async def test_rig_geometry_persists_and_old_profiles_migrate_to_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            profile_id = store.profile_index["active_id"]
            await store.update_rig_geometry(profile_id, {"right_m": 0.01, "forward_m": 0.18, "up_m": 0.08})
            reloaded = self.make_store(directory)
            self.assertEqual(reloaded.profile["camera"]["rig_offset"], {"right_m": 0.01, "forward_m": 0.18, "up_m": 0.08})

            path = Path(directory) / "profiles" / f"{profile_id}.json"
            old_profile = json.loads(path.read_text())
            old_profile["camera"].pop("rig_offset")
            old_profile["camera"].pop("rig_model")
            path.write_text(json.dumps(old_profile))
            migrated = self.make_store(directory)
            self.assertEqual(migrated.profile["camera"]["rig_offset"], {"right_m": 0.0, "forward_m": 0.0, "up_m": 0.0})

    async def test_profile_crud_switching_and_cannot_delete_last(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            original_id = store.profile_index["active_id"]
            created = await store.create_profile("Miller Head + Encoders")
            self.assertEqual(store.profile_index["active_id"], created["id"])
            renamed = await store.rename_profile(created["id"], "Miller Head")
            self.assertEqual(renamed["name"], "Miller Head")
            duplicate = await store.duplicate_profile(created["id"], "Trackside Camera 2")
            self.assertEqual(store.profile_index["active_id"], duplicate["id"])
            await store.select_profile(original_id)
            self.assertEqual(store.profile["name"], "Development Simulator")
            self.assertTrue(await store.delete_profile(created["id"]))
            self.assertTrue(await store.delete_profile(duplicate["id"]))
            self.assertFalse(await store.delete_profile(original_id))
            reloaded = self.make_store(directory)
            self.assertEqual(len(reloaded.profile_index["profiles"]), 1)

    async def test_legacy_engineering_migration_is_idempotent_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = {"profile_name": "Legacy Rig", "debug_enabled": False, "input_sources": {"pan": "imu", "tilt": "simulator", "zoom": "external", "focus": "disabled"}}
            calibration = {"pan_direction": -1, "tilt_direction": 1, "marks": {}}
            (root / "engineering.json").write_text(json.dumps(legacy))
            (root / "calibration.json").write_text(json.dumps(calibration))
            store = self.make_store(directory)
            self.assertEqual(store.profile["name"], "Legacy Rig")
            self.assertEqual(store.profile["axes"]["pan"]["source"], "imu")
            self.assertEqual(store.profile["axes"]["pan"]["mapping"]["direction"], -1)
            self.assertTrue((root / "engineering.json").exists())
            profile_id = store.profile_index["active_id"]
            second = self.make_store(directory)
            self.assertEqual(second.profile_index["active_id"], profile_id)

    async def test_simulator_uses_mapping_pipeline_and_preserves_camera_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            profile_id = store.profile_index["active_id"]
            await store.update_axis_config(profile_id, "pan", {"mapping": {"direction": -1, "offset": 10}})
            await store.set_camera({"pan": 20, "tilt": -6, "fov": 55, "height": 2.1})
            self.assertEqual(store.tracking["axes"]["pan"]["raw"], 20)
            self.assertEqual(store.tracking["axes"]["pan"]["value"], -10)
            self.assertEqual(store.camera["pan"], -10)
            self.assertEqual(store.camera["tilt"], -6)
            self.assertEqual(store.camera["fov"], 55)
            self.assertEqual(store.camera["height"], 2.1)
            reloaded = self.make_store(directory)
            self.assertEqual(reloaded.camera["fov"], 55)
            self.assertEqual(reloaded.camera["height"], 2.1)

    async def test_required_axis_validity_and_optional_disabled_axes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            profile_id = store.profile_index["active_id"]
            self.assertTrue(store.tracking["valid"])
            self.assertEqual(store.tracking["axes"]["focus"]["health"], "DISABLED")
            await store.update_axis_config(profile_id, "zoom", {"source": "disabled"})
            self.assertTrue(store.tracking["valid"])
            await store.update_axis_config(profile_id, "pan", {"source": "imu"})
            self.assertFalse(store.tracking["valid"])
            self.assertFalse(store.camera["valid"])
            self.assertEqual(store.tracking["axes"]["pan"]["health"], "NOT_CONFIGURED")
            await store.update_axis_config(profile_id, "pan", {"source": "simulator"})
            await store.update_axis_config(profile_id, "tilt", {"source": "disabled"})
            self.assertFalse(store.tracking["valid"])

    async def test_periodic_source_can_age_stale_but_simulator_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.tracking["axes"]["pan"].update({
                "source": "external",
                "valid": True,
                "health": "VALID",
                "expects_periodic_samples": True,
                "updated_mono": time.monotonic() - 3,
            })
            snapshot = store.snapshot()
            self.assertEqual(snapshot["tracking"]["axes"]["pan"]["health"], "STALE")
            self.assertFalse(snapshot["tracking"]["valid"])
            self.assertFalse(snapshot["camera"]["valid"])
            store.tracking["axes"]["pan"].update({
                "source": "simulator",
                "valid": True,
                "health": "VALID",
                "expects_periodic_samples": False,
                "updated_mono": time.monotonic() - 30,
            })
            self.assertEqual(store.snapshot()["tracking"]["axes"]["pan"]["health"], "VALID")

    async def test_direction_learning_updates_active_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await store.set_camera({"pan": 100})
            await store.calibration_mark("pan_left")
            await store.set_camera({"pan": 500})
            await store.calibration_mark("pan_right")
            self.assertEqual(store.profile["axes"]["pan"]["mapping"]["direction"], 1)
            self.assertTrue(store.profile["axes"]["pan"]["mapping"]["direction_learned"])
            await store.set_camera({"tilt": 500})
            await store.calibration_mark("tilt_down")
            await store.set_camera({"tilt": 100})
            await store.calibration_mark("tilt_up")
            self.assertEqual(store.profile["axes"]["tilt"]["mapping"]["direction"], -1)
            reloaded = self.make_store(directory)
            self.assertEqual(reloaded.profile["axes"]["tilt"]["mapping"]["direction"], -1)

    async def test_partial_direction_marks_are_profile_scoped_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            original_id = store.profile_index["active_id"]
            await store.set_camera({"pan": 123})
            await store.calibration_mark("pan_left")
            created = await store.create_profile("Second Rig")
            self.assertNotIn("pan_left", store.calibration["marks"])
            await store.select_profile(original_id)
            self.assertEqual(store.calibration["marks"]["pan_left"], 123)
            reloaded = self.make_store(directory)
            self.assertEqual(reloaded.calibration["marks"]["pan_left"], 123)
            self.assertNotEqual(created["id"], original_id)

    async def test_direction_learning_supports_both_orientations(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await store.set_camera({"pan": 500, "tilt": 100})
            await store.calibration_mark("pan_left")
            await store.calibration_mark("tilt_down")
            await store.set_camera({"pan": 100, "tilt": 500})
            await store.calibration_mark("pan_right")
            await store.calibration_mark("tilt_up")
            self.assertEqual(store.profile["axes"]["pan"]["mapping"]["direction"], -1)
            self.assertEqual(store.profile["axes"]["tilt"]["mapping"]["direction"], 1)

    async def test_invalid_update_does_not_corrupt_profile_json(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            profile_id = store.profile_index["active_id"]
            await store.update_axis_config(profile_id, "pan", {"source": "quadrature_gpio"})
            path = store._profile_path(profile_id)
            before = path.read_text()
            with self.assertRaises(TrackingConfigError):
                await store.update_axis_config(profile_id, "pan", {"mapping": {"ppr": 0}})
            self.assertEqual(path.read_text(), before)


if __name__ == "__main__":
    unittest.main()
