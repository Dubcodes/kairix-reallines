import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.gpio_source import QuadratureGpioWorker
from app.state import StateStore, temporary_zoom_fov


class SilentDebug:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def worker_config():
    return {
        "mapping": {
            "ppr": 600,
            "quadrature_multiplier": 4,
            "encoder_revs_per_camera_rev": 1.0,
            "direction": 1,
            "offset": 0.0,
        },
        "source_config": {
            "chip": "/dev/gpiochip0",
            "line_a": 5,
            "line_b": 6,
            "bias": "as_is",
            "integrity_policy": "strict",
        },
    }


class TemporaryZoomMathCase(unittest.TestCase):
    def test_endpoints_midpoint_and_clamping(self):
        self.assertAlmostEqual(temporary_zoom_fov(0, 0, 64.6, 1000, 10.0), 64.6)
        self.assertAlmostEqual(temporary_zoom_fov(1000, 0, 64.6, 1000, 10.0), 10.0)
        midpoint = temporary_zoom_fov(500, 0, 64.6, 1000, 10.0)
        self.assertGreater(midpoint, 10.0)
        self.assertLess(midpoint, 64.6)
        self.assertNotAlmostEqual(midpoint, (64.6 + 10.0) / 2)
        self.assertAlmostEqual(temporary_zoom_fov(-500, 0, 64.6, 1000, 10.0), 64.6)
        self.assertAlmostEqual(temporary_zoom_fov(1500, 0, 64.6, 1000, 10.0), 10.0)

    def test_reversed_count_direction_has_identical_fov_curve(self):
        forward = temporary_zoom_fov(250, 0, 64.6, 1000, 10.0)
        reverse = temporary_zoom_fov(750, 1000, 64.6, 0, 10.0)
        self.assertAlmostEqual(forward, reverse)

    def test_invalid_calibration_points_are_rejected(self):
        cases = (
            (0, 0, 64.6, 0, 10.0, "different counts"),
            (0, 0, 10.0, 1000, 10.0, "greater than Tight"),
            (0, 0, 9.0, 1000, 10.0, "greater than Tight"),
            (0, 0, 64.6, 1000, 0.0, "greater than 0"),
            (0, 0, 180.0, 1000, 10.0, "less than 180"),
        )
        for current, wide_count, wide_fov, tight_count, tight_fov, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                temporary_zoom_fov(current, wide_count, wide_fov, tight_count, tight_fov)


class TemporaryZoomStateCase(unittest.IsolatedAsyncioTestCase):
    def make_store(self, directory):
        return StateStore(Path(directory), SilentDebug())

    async def configure_zoom(self, store):
        await store.set_camera({"fov": 64.6})
        await store.update_axis_config(store.profile_index["active_id"], "zoom", {
            "source": "quadrature_gpio",
        })

    async def test_capture_drives_live_and_historic_fov_without_changing_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_zoom(store)
            zoom = QuadratureGpioWorker("zoom", worker_config())
            zoom.begin_acquisition(0, 0)
            reference_id = zoom.set_reference(0)

            store.sample_tick({"zoom": zoom.snapshot()})
            self.assertAlmostEqual(store.camera["fov"], 64.6)
            self.assertEqual(store.camera["fov_source"], "Manual / Profile")
            wide = await store.capture_zoom_fov_point("wide", 64.6)
            self.assertEqual(wide["wide"], {
                "relative_count": 0,
                "fov": 64.6,
                "reference_id": reference_id,
            })
            self.assertFalse(wide["active"])

            zoom.decoder.count = 1000
            store.sample_tick({"zoom": zoom.snapshot()})
            mapping = await store.capture_zoom_fov_point("tight", 10.0)
            self.assertTrue(mapping["active"])
            self.assertEqual(mapping["status"], "LIVE ZOOM ACTIVE")
            self.assertAlmostEqual(store.camera["fov"], 10.0)

            zoom.decoder.count = 500
            store.sample_tick({"zoom": zoom.snapshot()})
            self.assertGreater(store.camera["fov"], 10.0)
            self.assertLess(store.camera["fov"], 64.6)
            self.assertEqual(store.camera["fov_source"], "Physical Zoom — Temporary mapping")
            self.assertAlmostEqual(store.pose_history.samples[-1].fov, store.camera["fov"])
            self.assertEqual(store.profile["camera"]["fov"], 64.6)
            self.assertTrue(store.tracking["valid"], "optional Zoom must not alter Pan/Tilt validity")

            store.pose_history.reset("mapped FOV delay fixture")
            zoom.decoder.count = 0
            with patch("app.state.time.monotonic", return_value=100.0):
                store.sample_tick({"zoom": zoom.snapshot()}, 100.0)
            zoom.decoder.count = 1000
            with patch("app.state.time.monotonic", return_value=101.0):
                store.sample_tick({"zoom": zoom.snapshot()}, 101.0)
            store.sync_config["graphics_delay_ms"] = 500
            render_camera, sync = store._render_pose_state(101.0)
            self.assertEqual(sync["interpolation_mode"], "INTERPOLATED")
            self.assertGreater(render_camera["fov"], 10.0)
            self.assertLess(render_camera["fov"], 64.6)
            self.assertAlmostEqual(render_camera["pan"], store.camera["pan"])

            zoom.process_levels(1, 1, 1_000_000_000)
            zoom.decoder.count = 750
            store.sample_tick({"zoom": zoom.snapshot(1_000_000_000)})
            self.assertEqual(store.tracking["axes"]["zoom"]["integrity"], "SUSPECT")
            self.assertTrue(store.zoom_fov_mapping["active"])
            self.assertNotEqual(store.camera["fov"], 64.6)

    async def test_reference_change_loss_and_restart_clear_session_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_zoom(store)
            zoom = QuadratureGpioWorker("zoom", worker_config())
            zoom.begin_acquisition(0, 0)
            first_reference = zoom.set_reference(0)
            store.sample_tick({"zoom": zoom.snapshot()})
            await store.capture_zoom_fov_point("wide", 64.6)
            zoom.decoder.count = 1000
            store.sample_tick({"zoom": zoom.snapshot()})
            await store.capture_zoom_fov_point("tight", 10.0)

            second_reference = zoom.set_reference(0)
            self.assertNotEqual(first_reference, second_reference)
            store.sample_tick({"zoom": zoom.snapshot()})
            self.assertIsNone(store.zoom_fov_mapping["wide"])
            self.assertIsNone(store.zoom_fov_mapping["tight"])
            self.assertFalse(store.zoom_fov_mapping["active"])
            self.assertIn("REFERENCE CHANGED", store.zoom_fov_mapping["status"])
            self.assertAlmostEqual(store.camera["fov"], 64.6)

            await store.capture_zoom_fov_point("wide", 64.6)
            zoom.clear_reference()
            store.sample_tick({"zoom": zoom.snapshot()})
            self.assertIsNone(store.zoom_fov_mapping["wide"])
            self.assertAlmostEqual(store.camera["fov"], 64.6)

            reloaded = self.make_store(directory)
            self.assertIsNone(reloaded.zoom_fov_mapping["wide"])
            self.assertIsNone(reloaded.zoom_fov_mapping["tight"])
            self.assertFalse(reloaded.zoom_fov_mapping["active"])
            self.assertAlmostEqual(reloaded.camera["fov"], 64.6)

    async def test_capture_validation_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            with self.assertRaisesRegex(ValueError, "Reference"):
                await store.capture_zoom_fov_point("wide", 64.6)
            await self.configure_zoom(store)
            zoom = QuadratureGpioWorker("zoom", worker_config())
            zoom.begin_acquisition(0, 0)
            zoom.set_reference(0)
            store.sample_tick({"zoom": zoom.snapshot()})
            await store.capture_zoom_fov_point("wide", 64.6)
            with self.assertRaisesRegex(ValueError, "different counts"):
                await store.capture_zoom_fov_point("tight", 10.0)
            self.assertIsNone(store.zoom_fov_mapping["tight"])
            with self.assertRaisesRegex(ValueError, "greater than 0"):
                await store.capture_zoom_fov_point("tight", 0)


if __name__ == "__main__":
    unittest.main()
