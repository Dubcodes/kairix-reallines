import asyncio
import contextlib
import tempfile
import time
import unittest
from pathlib import Path

from app.imu import (
    ImuAssistanceService,
    ImuCalibrationError,
    ImuSample,
    calibration_proposal,
    estimate_stationary_bias,
    mounting_alignment,
    movement_observation,
)
from app.state import StateStore
from app.runtime import imu_status_publication_loop


class SilentDebug:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def samples(start, duration, gyro=(0.0, 0.0, 0.0), bias=(0.2, -0.1, 0.05), accel=(0.0, 0.0, 1.0), hz=100):
    return [
        ImuSample(
            start + index / hz,
            gyro[0] + bias[0], gyro[1] + bias[1], gyro[2] + bias[2],
            *accel,
        )
        for index in range(int(duration * hz) + 1)
    ]


class ImuMathCase(unittest.TestCase):
    def test_stationary_bias_estimation_and_motion_rejection(self):
        stationary = samples(1.0, 1.0)
        result = estimate_stationary_bias(stationary)
        self.assertAlmostEqual(result["gyro_bias_dps"]["x"], 0.2)
        moving = [
            ImuSample(sample.timestamp, sample.gyro_x + (index % 2) * 4, sample.gyro_y, sample.gyro_z, sample.accel_x, sample.accel_y, sample.accel_z)
            for index, sample in enumerate(stationary)
        ]
        with self.assertRaisesRegex(ImuCalibrationError, "moved"):
            estimate_stationary_bias(moving)

    def test_mounting_alignment_supports_arbitrary_sensor_orientation(self):
        bias = {"x": 0.2, "y": -0.1, "z": 0.05}
        alignment = mounting_alignment(
            samples(1.0, 1.0, gyro=(0, 0, 20)),
            samples(3.0, 1.0, gyro=(20, 0, 0)),
            bias,
        )
        self.assertAlmostEqual(alignment["pan_axis"]["z"], 1.0)
        self.assertAlmostEqual(alignment["tilt_axis"]["x"], 1.0)
        self.assertGreater(alignment["quality"], 0.99)
        with self.assertRaisesRegex(ImuCalibrationError, "distinct"):
            mounting_alignment(
                samples(5.0, 1.0, gyro=(0, 0, 20)),
                samples(7.0, 1.0, gyro=(0, 1, 20)),
                bias,
            )

    def test_observation_rejects_cross_axis_motion_and_proposal_requires_consistency(self):
        bias = {"x": 0.2, "y": -0.1, "z": 0.05}
        observation = movement_observation(
            samples(1.0, 1.0, gyro=(0, 0, 18)), bias, {"x": 0, "y": 0, "z": 1}, 0, 1200,
        )
        self.assertAlmostEqual(observation["angle_deg"], 18.0)
        proposal = calibration_proposal([
            {"delta_count": 1200, "angle_deg": 18, "cross_axis_fraction": 0.01},
            {"delta_count": -1800, "angle_deg": -27, "cross_axis_fraction": 0.02},
            {"delta_count": 2400, "angle_deg": 36, "cross_axis_fraction": 0.01},
        ], 600, 4)
        self.assertAlmostEqual(proposal["encoder_revs_per_camera_rev"], 10.0)
        self.assertEqual(proposal["direction"], 1)
        self.assertGreater(proposal["confidence"], 0.9)
        with self.assertRaisesRegex(ImuCalibrationError, "both directions"):
            calibration_proposal([
                {"delta_count": 1200, "angle_deg": 18},
                {"delta_count": 1800, "angle_deg": 27},
                {"delta_count": 2400, "angle_deg": 36},
            ], 600, 4)
        with self.assertRaisesRegex(ImuCalibrationError, "inconsistent"):
            calibration_proposal([
                {"delta_count": 1200, "angle_deg": 18},
                {"delta_count": -900, "angle_deg": -27},
                {"delta_count": 3000, "angle_deg": 36},
            ], 600, 4)
        with self.assertRaisesRegex(ImuCalibrationError, "mount shifted"):
            movement_observation(
                samples(3.0, 1.0, gyro=(12, 0, 18)), bias, {"x": 0, "y": 0, "z": 1}, 0, 1200,
            )


class ImuServiceCase(unittest.IsolatedAsyncioTestCase):
    def test_disabled_default_and_simulated_source_are_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ImuAssistanceService(Path(directory), SilentDebug())
            service.start()
            self.assertEqual(service.snapshot()["health"], "DISABLED")
            service.configure({"enabled": True, "source": "simulated"})
            self.assertEqual(service.snapshot()["health"], "READY")
            with self.assertRaisesRegex(ImuCalibrationError, "bus or address"):
                service.configure({"address": 0x80})
            base = time.monotonic() - 1.1
            for sample in samples(base, 1.0):
                service.inject_simulated(sample)
            service.calibrate_bias()
            self.assertIsNotNone(service.snapshot()["bias"])
            service.stop()

    def test_simulated_alignment_and_repeated_movements_prepare_but_do_not_apply_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ImuAssistanceService(Path(directory), SilentDebug())
            service.configure({"enabled": True, "source": "simulated"})
            base = time.monotonic() - 1.1
            for sample in samples(base, 1.0):
                service.inject_simulated(sample)
            service.calibrate_bias()

            def record(kind, axis, start_count, end_count, gyro):
                service.begin_measurement(kind, axis, start_count)
                start = service.snapshot()["last_sample_mono"] + 0.01
                for sample in samples(start, 1.0, gyro=gyro):
                    service.inject_simulated(sample)
                return service.finish_measurement(end_count, 600, 4)

            record("alignment", "pan", 0, 100, (0, 0, 20))
            aligned = record("alignment", "tilt", 0, 100, (20, 0, 0))
            self.assertIsNotNone(aligned["alignment"])
            record("movement", "pan", 0, 1200, (0, 0, 18))
            record("movement", "pan", 1200, -600, (0, 0, -27))
            result = record("movement", "pan", -600, 1800, (0, 0, 36))
            self.assertAlmostEqual(result["proposals"]["pan"]["encoder_revs_per_camera_rev"], 10.0)
            self.assertEqual(result["proposals"]["pan"]["direction"], 1)
            self.assertEqual(result["observations"]["pan"][0]["delta_count"], 1200)
            self.assertEqual(service.snapshot()["status"], "Calibration proposal ready — review before applying")

    def test_disconnect_during_measurement_cancels_without_a_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ImuAssistanceService(Path(directory), SilentDebug())
            service.configure({"enabled": True, "source": "simulated"})
            base = time.monotonic() - 1.1
            for sample in samples(base, 1.0):
                service.inject_simulated(sample)
            service.calibrate_bias()
            service.active = {"kind": "alignment", "axis": "pan", "start_count": 0, "started_mono": time.monotonic()}
            service.health = "NOT_CONNECTED"
            service.active = None
            with self.assertRaisesRegex(ImuCalibrationError, "disconnected"):
                service.finish_measurement(100, 600, 4)
            self.assertIsNone(service.snapshot()["proposals"]["pan"])

    async def test_imu_failure_never_blocks_manual_encoder_calibration_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = ImuAssistanceService(root, SilentDebug())
            service.health = "NOT_CONNECTED"
            service.last_error = "sensor absent"
            store = StateStore(root, SilentDebug())
            store.imu_service = service
            profile_id = store.profile_index["active_id"]
            before = store.profile["axes"]["pan"]["mapping"]["encoder_revs_per_camera_rev"]
            await store.update_axis_config(profile_id, "pan", {"mapping": {"encoder_revs_per_camera_rev": 2.5, "direction": -1}})
            self.assertNotEqual(before, store.profile["axes"]["pan"]["mapping"]["encoder_revs_per_camera_rev"])
            self.assertEqual(store.profile["axes"]["pan"]["mapping"]["direction"], -1)
            self.assertEqual(store.snapshot()["imu"]["health"], "NOT_CONNECTED")

    async def test_sensor_connectivity_change_is_published_without_pose_spam(self):
        class Store:
            broadcasts = 0

            async def broadcast_state(self):
                self.broadcasts += 1

        class Service:
            health = "CONNECTING"

            def snapshot(self):
                return {"enabled": True, "source": "mpu9250", "health": self.health, "last_error": None}

        store, service = Store(), Service()
        task = asyncio.create_task(imu_status_publication_loop(store, service, 0.005))
        await asyncio.sleep(0.012)
        self.assertEqual(store.broadcasts, 0)
        service.health = "READY"
        await asyncio.sleep(0.012)
        self.assertEqual(store.broadcasts, 1)
        await asyncio.sleep(0.012)
        self.assertEqual(store.broadcasts, 1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

