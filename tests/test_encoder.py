import sys
import tempfile
import time
import unittest
from pathlib import Path

from app.quadrature import QuadratureDecoder
from app.state import StateStore
from app.tracking import TrackingConfigError, default_profile, tracking_fingerprint


class SilentDebug:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class RecordingClient:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


FORWARD = ((0, 0), (0, 1), (1, 1), (1, 0), (0, 0))
REVERSE = tuple(reversed(FORWARD))


class DecoderCase(unittest.TestCase):
    def drive(self, multiplier, states):
        decoder = QuadratureDecoder(multiplier, *states[0])
        for state in states[1:]:
            decoder.update(*state)
        return decoder

    def test_true_x1_x2_x4_counts_in_both_directions(self):
        for multiplier, expected in ((1, 1), (2, 2), (4, 4)):
            self.assertEqual(self.drive(multiplier, FORWARD).count, expected)
            self.assertEqual(self.drive(multiplier, REVERSE).count, -expected)

    def test_illegal_transition_is_diagnostic_not_a_jump(self):
        decoder = QuadratureDecoder(4)
        self.assertEqual(decoder.update(1, 1), 0)
        self.assertEqual(decoder.count, 0)
        self.assertEqual(decoder.diagnostics.illegal_transitions, 1)

    def test_reversal_and_duplicate_noise_do_not_create_counts(self):
        decoder = QuadratureDecoder(4)
        for state in FORWARD[1:]:
            decoder.update(*state)
        for state in REVERSE[1:]:
            decoder.update(*state)
            decoder.update(*state)
        self.assertEqual(decoder.count, 0)
        self.assertEqual(decoder.diagnostics.duplicate_events, 4)

    def test_unbounded_high_rate_sequence(self):
        decoder = QuadratureDecoder(4)
        for _ in range(10_000):
            for state in FORWARD[1:]:
                decoder.update(*state)
        self.assertEqual(decoder.count, 40_000)
        self.assertEqual(decoder.diagnostics.illegal_transitions, 0)


class FingerprintCase(unittest.TestCase):
    def test_fingerprint_is_deterministic_and_covers_encoder_identity(self):
        profile = default_profile()
        profile["axes"]["pan"].update({"source": "quadrature_gpio"})
        config = profile["axes"]["pan"]
        config["source_config"].update({"chip": "/dev/gpiochip0", "line_a": 5, "line_b": 6})
        original = tracking_fingerprint(profile)
        self.assertEqual(original, tracking_fingerprint(profile))
        mutations = (
            ("mapping", "direction", -1),
            ("mapping", "offset", 2.0),
            ("mapping", "ppr", 1024),
            ("mapping", "quadrature_multiplier", 2),
            ("mapping", "encoder_revs_per_camera_rev", 2.0),
            ("source_config", "chip", "/dev/gpiochip1"),
            ("source_config", "line_a", 7),
            ("source_config", "line_b", 8),
        )
        for section, key, value in mutations:
            changed = default_profile()
            changed["axes"]["pan"] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in config.items()}
            changed["axes"]["pan"][section][key] = value
            self.assertNotEqual(original, tracking_fingerprint(changed), key)

    def test_gpio_module_is_import_safe_without_loading_driver(self):
        import app.gpio_source  # noqa: F401
        self.assertNotIn("gpiod", sys.modules)


class EncoderStateCase(unittest.IsolatedAsyncioTestCase):
    def make_store(self, directory):
        return StateStore(Path(directory), SilentDebug())

    async def configure_pan(self, store, offset=0.0):
        profile_id = store.profile_index["active_id"]
        return await store.update_axis_config(profile_id, "pan", {
            "source": "quadrature_gpio",
            "mapping": {"offset": offset, "ppr": 600, "quadrature_multiplier": 4, "encoder_revs_per_camera_rev": 1.0},
            "source_config": {"chip": "/dev/gpiochip0", "line_a": 5, "line_b": 6, "bias": "as_is"},
        })

    def sample(self, **updates):
        value = {"raw_count": 0, "configured": True, "driver_alive": True, "referenced": False, "reference_count": None, "reference_angle": 0.0, "events": 0}
        value.update(updates)
        return {"pan": value}

    async def test_reference_required_then_absolute_mapping_and_stationary_health(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_pan(store, offset=3.0)
            store.sample_tick(self.sample())
            self.assertEqual(store.tracking["axes"]["pan"]["health"], "REFERENCE_REQUIRED")
            self.assertFalse(store.camera["valid"])
            generation = store.pose_history.generation
            store.sample_tick(self.sample(raw_count=2400, referenced=True, reference_count=0, reference_angle=12.0))
            pan = store.tracking["axes"]["pan"]
            self.assertAlmostEqual(pan["relative_angle"], 360.0)
            self.assertAlmostEqual(pan["value"], 375.0)
            self.assertTrue(store.camera["valid"])
            self.assertGreater(store.pose_history.generation, generation)
            store.sample_tick(self.sample(raw_count=2400, referenced=True, reference_count=0, reference_angle=12.0), time.monotonic() + 30)
            self.assertEqual(store.tracking["axes"]["pan"]["health"], "VALID")

    async def test_driver_failure_invalidates_authoritative_camera_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_pan(store)
            store.sample_tick(self.sample(referenced=True, reference_count=0))
            self.assertTrue(store.camera["valid"])
            store.sample_tick(self.sample(configured=False, driver_alive=False, health="ERROR", status="device removed"))
            self.assertFalse(store.camera["valid"])
            self.assertFalse(store.pose_history.samples[-1].valid)
            self.assertEqual(store.snapshot()["tracking"]["axes"]["pan"]["health"], "ERROR")

    async def test_periodic_stale_is_authoritative_before_sampling(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            runtime = store.tracking["axes"]["pan"]
            runtime.update({"source": "external", "expects_periodic_samples": True, "valid": True, "health": "VALID", "updated_mono": time.monotonic() - 10})
            store.profile["axes"]["pan"]["source"] = "external"
            store.sample_tick()
            self.assertEqual(store.tracking["axes"]["pan"]["health"], "STALE")
            self.assertFalse(store.camera["valid"])
            self.assertFalse(store.pose_history.samples[-1].valid)

    async def test_gpio_conflicts_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_pan(store)
            with self.assertRaises(TrackingConfigError):
                await store.update_axis_config(store.profile_index["active_id"], "tilt", {
                    "source": "quadrature_gpio",
                    "source_config": {"chip": "/dev/gpiochip0", "line_a": 6, "line_b": 7},
                })

    async def test_reference_is_session_only_after_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_pan(store)
            store.sample_tick(self.sample(referenced=True, reference_count=0, reference_angle=20))
            self.assertTrue(store.tracking["axes"]["pan"]["referenced"])
            reloaded = self.make_store(directory)
            self.assertFalse(reloaded.tracking["axes"]["pan"]["referenced"])
            self.assertFalse(reloaded.tracking["axes"]["pan"]["valid"])

    async def test_fingerprint_change_invalidates_world_and_resets_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await store.generate_synthetic_world_calibration({"camera": {"x": 0, "y": 0, "z": 1.8}})
            self.assertTrue(store.camera["world_valid"])
            generation = store.pose_history.generation
            await store.update_axis_config(store.profile_index["active_id"], "pan", {"mapping": {"offset": 1.0}})
            snapshot = store.snapshot()
            self.assertFalse(snapshot["world_calibrations"]["fingerprint_match"])
            self.assertFalse(snapshot["camera"]["world_valid"])
            self.assertGreater(store.pose_history.generation, generation)

    async def test_pose_transport_excludes_scene_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            pose = store.pose_snapshot()
            self.assertEqual(set(pose), {"camera", "live_camera", "render_camera", "tracking", "sync", "server_mono"})
            self.assertNotIn("scene", pose)
            self.assertIn("scene", store.snapshot())
            client = RecordingClient()
            store.clients.add(client)
            await store.set_camera({"pan": 5})
            self.assertEqual(client.messages[-1]["type"], "pose")


if __name__ == "__main__":
    unittest.main()
