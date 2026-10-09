import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

from app.quadrature import QuadratureDecoder
from app.gpio_source import QuadratureGpioWorker
from app.runtime import publish_pose_once, sample_tracking_once
from app.state import StateStore
from app.tracking import TrackingConfigError, default_axis_config, default_profile, tracking_fingerprint
from app.world_calibration import CalibrationError


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


def worker_config(axis="pan", line_a=5, line_b=6):
    config = default_axis_config(axis, "quadrature_gpio")
    config["source_config"].update({"chip": "/dev/gpiochip0", "line_a": line_a, "line_b": line_b})
    return config


class GpioWorkerIntegrityCase(unittest.TestCase):
    def referenced_worker(self):
        worker = QuadratureGpioWorker("pan", worker_config())
        worker.begin_acquisition(0, 0)
        reference_id = worker.set_reference(0)
        return worker, reference_id

    def test_reference_ids_are_unique_and_clear_is_complete(self):
        worker, first = self.referenced_worker()
        worker.clear_reference()
        snapshot = worker.snapshot()
        self.assertFalse(snapshot["referenced"])
        self.assertIsNone(snapshot["reference_id"])
        second = worker.set_reference(10)
        self.assertNotEqual(first, second)
        self.assertEqual(worker.snapshot()["reference_id"], second)

    def test_duplicate_level_does_not_invalidate_reference(self):
        worker, reference_id = self.referenced_worker()
        worker.process_levels(0, 0, 1_000_000_000)
        snapshot = worker.snapshot(1_000_000_000)
        self.assertTrue(snapshot["referenced"])
        self.assertEqual(snapshot["reference_id"], reference_id)
        self.assertFalse(snapshot["integrity_lost"])

    def test_driver_restart_loses_reference_but_retains_diagnostics(self):
        worker, _ = self.referenced_worker()
        worker.decoder.count = 42
        worker._set_error("request lost")
        self.assertEqual(worker.snapshot()["health"], "ERROR")
        worker.begin_acquisition(0, 0)
        snapshot = worker.snapshot()
        self.assertEqual(snapshot["raw_count"], 42)
        self.assertFalse(snapshot["referenced"])
        self.assertIsNone(snapshot["reference_id"])
        self.assertEqual(snapshot["health"], "REFERENCE_REQUIRED")
        self.assertEqual(snapshot["last_error"], "request lost")

    def test_illegal_transition_latches_integrity_and_loses_reference(self):
        worker, _ = self.referenced_worker()
        worker.process_levels(1, 1, 1_000_000_000)
        snapshot = worker.snapshot(1_000_000_000)
        self.assertEqual(snapshot["illegal_transitions"], 1)
        self.assertTrue(snapshot["integrity_lost"])
        self.assertEqual(snapshot["integrity_loss_events"], 1)
        self.assertFalse(snapshot["referenced"])
        self.assertIsNone(snapshot["reference_id"])

    def test_sequence_gap_is_separate_and_loses_reference_once(self):
        worker, _ = self.referenced_worker()
        worker.process_edge(6, True, 1_000_000_000, global_seqno=1, line_seqno=1)
        worker.process_edge(5, True, 1_100_000_000, global_seqno=3, line_seqno=1)
        snapshot = worker.snapshot(1_100_000_000)
        self.assertEqual(snapshot["global_sequence_gaps"], 1)
        self.assertEqual(snapshot["line_sequence_gaps"], 0)
        self.assertEqual(snapshot["integrity_loss_events"], 1)
        self.assertFalse(snapshot["referenced"])

    def test_line_sequence_gap_is_counted_independently(self):
        worker, _ = self.referenced_worker()
        worker.process_edge(6, True, 1_000_000_000, global_seqno=1, line_seqno=1)
        worker.process_edge(6, False, 1_100_000_000, global_seqno=2, line_seqno=3)
        snapshot = worker.snapshot(1_100_000_000)
        self.assertEqual(snapshot["global_sequence_gaps"], 0)
        self.assertEqual(snapshot["line_sequence_gaps"], 1)
        self.assertEqual(snapshot["integrity_loss_events"], 1)
        self.assertFalse(snapshot["referenced"])

    def test_kernel_timestamps_levels_and_signed_motion_rates(self):
        worker, _ = self.referenced_worker()
        events = ((6, True), (5, True), (6, False), (5, False))
        for index, (line, rising) in enumerate(events, 1):
            worker.process_edge(line, rising, index * 100_000_000, global_seqno=index, line_seqno=1 if index < 3 else 2)
        snapshot = worker.snapshot(400_000_000)
        self.assertEqual((snapshot["a_state"], snapshot["b_state"]), (0, 0))
        self.assertEqual(snapshot["last_edge_timestamp_ns"], 400_000_000)
        self.assertEqual(snapshot["last_edge_age_ms"], 0)
        self.assertGreater(snapshot["event_rate_hz"], 0)
        self.assertGreater(snapshot["counts_per_second"], 0)
        self.assertGreater(snapshot["degrees_per_second"], 0)
        stationary = worker.snapshot(1_500_000_000)
        self.assertEqual(stationary["counts_per_second"], 0)
        self.assertEqual(stationary["degrees_per_second"], 0)
        self.assertEqual(stationary["health"], "VALID")


class EncoderStateCase(unittest.IsolatedAsyncioTestCase):
    def make_store(self, directory):
        return StateStore(Path(directory), SilentDebug())

    async def configure_axis(self, store, axis, offset=0.0, line_a=5, line_b=6):
        profile_id = store.profile_index["active_id"]
        return await store.update_axis_config(profile_id, axis, {
            "source": "quadrature_gpio",
            "mapping": {"offset": offset, "ppr": 600, "quadrature_multiplier": 4, "encoder_revs_per_camera_rev": 1.0},
            "source_config": {"chip": "/dev/gpiochip0", "line_a": line_a, "line_b": line_b, "bias": "as_is"},
        })

    async def configure_pan(self, store, offset=0.0):
        return await self.configure_axis(store, "pan", offset)

    def axis_sample(self, reference_id=None, **updates):
        value = {"raw_count": 0, "configured": True, "driver_alive": True, "referenced": reference_id is not None, "reference_count": 0 if reference_id else None, "reference_angle": 0.0, "reference_id": reference_id, "events": 0}
        value.update(updates)
        return value

    def sample(self, reference_id=None, **updates):
        return {"pan": self.axis_sample(reference_id, **updates)}

    async def test_reference_required_then_absolute_mapping_and_stationary_health(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_pan(store, offset=3.0)
            store.sample_tick(self.sample())
            self.assertEqual(store.tracking["axes"]["pan"]["health"], "REFERENCE_REQUIRED")
            self.assertFalse(store.camera["valid"])
            generation = store.pose_history.generation
            store.sample_tick(self.sample("R1", raw_count=2400, reference_angle=12.0))
            pan = store.tracking["axes"]["pan"]
            self.assertAlmostEqual(pan["relative_angle"], 360.0)
            self.assertAlmostEqual(pan["value"], 375.0)
            self.assertTrue(store.camera["valid"])
            self.assertGreater(store.pose_history.generation, generation)
            store.sample_tick(self.sample("R1", raw_count=2400, reference_angle=12.0), time.monotonic() + 30)
            self.assertEqual(store.tracking["axes"]["pan"]["health"], "VALID")

    async def test_driver_failure_invalidates_authoritative_camera_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_pan(store)
            store.sample_tick(self.sample("R1"))
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
            store.sample_tick(self.sample("R1", reference_angle=20))
            self.assertTrue(store.tracking["axes"]["pan"]["referenced"])
            reloaded = self.make_store(directory)
            self.assertFalse(reloaded.tracking["axes"]["pan"]["referenced"])
            self.assertFalse(reloaded.tracking["axes"]["pan"]["valid"])

    async def test_one_revolution_relative_and_direction_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_pan(store)
            store.sample_tick(self.sample("R1", raw_count=1000, reference_count=1000))
            self.assertEqual(store.tracking["axes"]["pan"]["relative_angle"], 0)
            self.assertEqual(store.tracking["axes"]["pan"]["value"], 0)
            store.sample_tick(self.sample("R1", raw_count=3400, reference_count=1000))
            self.assertAlmostEqual(store.tracking["axes"]["pan"]["relative_angle"], 360)
            self.assertAlmostEqual(store.tracking["axes"]["pan"]["value"], 360)
            store.sample_tick(self.sample("R1", raw_count=1000, reference_count=1000))
            self.assertAlmostEqual(store.tracking["axes"]["pan"]["value"], 0)
            await store.update_axis_config(store.profile_index["active_id"], "pan", {"mapping": {"direction": -1}})
            store.sample_tick(self.sample("R2", raw_count=3400, reference_count=1000))
            self.assertEqual(store.tracking["axes"]["pan"]["raw_count"], 3400)
            self.assertAlmostEqual(store.tracking["axes"]["pan"]["relative_angle"], 360)
            self.assertAlmostEqual(store.tracking["axes"]["pan"]["value"], -360)

    async def test_world_calibration_reference_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_pan(store)
            store.sample_tick(self.sample("R1"))
            await store.generate_synthetic_world_calibration({"camera": {"x": 0, "y": 0, "z": 1.8}})
            self.assertTrue(store.camera["world_valid"])
            self.assertEqual(store.world_calibration["tracking_reference_identity"], {"pan": "R1"})
            store.sample_tick(self.sample("R2"))
            snapshot = store.snapshot()
            self.assertFalse(snapshot["camera"]["world_valid"])
            self.assertFalse(snapshot["world_calibrations"]["reference_match"])
            self.assertEqual(snapshot["world_calibrations"]["active"]["effective_status"], "REFERENCE MISMATCH — RECALIBRATE")

    async def test_encoder_reference_loss_invalidates_world_and_pose_history(self):
        for failure in ("driver_restart", "illegal_transition", "sequence_gap"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                store = self.make_store(directory)
                await self.configure_pan(store)
                worker = QuadratureGpioWorker("pan", worker_config())
                worker.begin_acquisition(0, 0)
                worker.set_reference(0)
                store.sample_tick({"pan": worker.snapshot()})
                await store.generate_synthetic_world_calibration({"camera": {"x": 0, "y": 0, "z": 1.8}})
                self.assertTrue(store.camera["world_valid"])

                if failure == "driver_restart":
                    worker._set_error("request lost")
                    worker.begin_acquisition(0, 0)
                elif failure == "illegal_transition":
                    worker.process_levels(1, 1, 1_000_000_000)
                else:
                    worker.process_edge(6, True, 1_000_000_000, global_seqno=1, line_seqno=1)
                    worker.process_edge(5, True, 1_100_000_000, global_seqno=3, line_seqno=1)
                store.sample_tick({"pan": worker.snapshot(1_100_000_000)})

                snapshot = store.snapshot()
                self.assertEqual(snapshot["tracking"]["axes"]["pan"]["health"], "REFERENCE_REQUIRED")
                self.assertFalse(snapshot["camera"]["valid"])
                self.assertFalse(snapshot["camera"]["world_valid"])
                self.assertFalse(store.pose_history.samples[-1].valid)

    async def test_mixed_and_dual_quadrature_reference_identity(self):
        cases = (("pan",), ("tilt",), ("pan", "tilt"))
        for quadrature_axes in cases:
            with self.subTest(quadrature_axes=quadrature_axes), tempfile.TemporaryDirectory() as directory:
                store = self.make_store(directory)
                if "pan" in quadrature_axes:
                    await self.configure_axis(store, "pan", line_a=5, line_b=6)
                if "tilt" in quadrature_axes:
                    await self.configure_axis(store, "tilt", line_a=7, line_b=8)
                samples = {axis: self.axis_sample(f"{axis}-R1") for axis in quadrature_axes}
                store.sample_tick(samples)
                await store.generate_synthetic_world_calibration({"camera": {"x": 0, "y": 0, "z": 1.8}})
                expected = {axis: f"{axis}-R1" for axis in quadrature_axes}
                self.assertEqual(store.world_calibration["tracking_reference_identity"], expected)
                self.assertTrue(store.camera["world_valid"])
                simulator_axis = next((axis for axis in ("pan", "tilt") if axis not in quadrature_axes), None)
                if simulator_axis:
                    await store.set_camera({simulator_axis: 5})
                    self.assertTrue(store.snapshot()["camera"]["world_valid"])
                changed = dict(samples)
                first = quadrature_axes[0]
                changed[first] = self.axis_sample(f"{first}-R2")
                store.sample_tick(changed)
                self.assertFalse(store.snapshot()["camera"]["world_valid"])

    async def test_marks_from_different_references_cannot_solve(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            await self.configure_pan(store)
            store.sample_tick(self.sample("R1"))
            await store.mark_world_target("top_left")
            store.sample_tick(self.sample("R2", raw_count=100))
            for index, target in enumerate(("top_right", "bottom_right", "bottom_left"), 1):
                store.sample_tick(self.sample("R2", raw_count=100 + index * 100))
                await store.set_camera({"tilt": index})
                await store.mark_world_target(target)
            with self.assertRaisesRegex(CalibrationError, "different encoder reference"):
                await store.solve_active_world_calibration()

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


class RuntimeSeparationCase(unittest.IsolatedAsyncioTestCase):
    async def test_slow_publication_cannot_block_sampling_step(self):
        release = asyncio.Event()

        class FakeStore:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.samples = 0
                self.publication_started = asyncio.Event()

            def sample_tick(self, _snapshots, _timestamp):
                self.samples += 1

            async def broadcast_pose(self):
                self.publication_started.set()
                await release.wait()

        class FakeSources:
            def snapshots(self):
                return {}

        store = FakeStore()
        publisher = asyncio.create_task(publish_pose_once(store))
        await store.publication_started.wait()
        await asyncio.wait_for(sample_tracking_once(store, FakeSources()), timeout=0.1)
        self.assertEqual(store.samples, 1)
        publisher.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await publisher


if __name__ == "__main__":
    unittest.main()
