import sys
import struct
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from app.linux_rotary_source import (
    EVIOCSCLOCKID,
    EV_REL,
    EV_SYN,
    INPUT_EVENT_STRUCT,
    REL_CODES,
    SYN_DROPPED,
    SYN_REPORT,
    LinuxRotaryUnavailable,
    LinuxRotaryWorker,
    _configure_event_clock,
    discover_rotary_device,
)
from app.source_manager import TrackingSourceManager
from app.state import StateStore
from app.tracking import counts_per_camera_revolution, default_axis_config, degrees_per_count, tracking_fingerprint


class SilentDebug:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def rotary_config(axis="pan", ratio=1.0):
    config = default_axis_config(axis, "linux_rotary")
    config["mapping"]["encoder_revs_per_camera_rev"] = ratio
    return config


def live_worker(axis="pan"):
    worker = LinuxRotaryWorker(axis, rotary_config(axis))
    worker.begin_acquisition(f"/dev/input/event-{axis}", "rotary-encoder")
    return worker


def add_input_device(root: Path, event: str, name: str, rel_bits: int):
    base = root / event / "device"
    (base / "capabilities").mkdir(parents=True)
    (base / "name").write_text(name, encoding="utf-8")
    (base / "capabilities" / "rel").write_text(f"{rel_bits:x}\n", encoding="ascii")


class LinuxRotaryWorkerCase(unittest.TestCase):
    def test_evdev_clock_is_configured_for_monotonic_timestamps(self):
        calls = []
        fake_fcntl = SimpleNamespace(ioctl=lambda fd, request, value: calls.append((fd, request, value)))
        with patch.dict(sys.modules, {"fcntl": fake_fcntl}):
            _configure_event_clock(42, 7)
        self.assertEqual(calls, [(42, EVIOCSCLOCKID, struct.pack("i", 7))])

    def test_maximum_resolution_mapping_is_exact(self):
        shaft = rotary_config("pan")
        geared = rotary_config("pan", 10.0)
        self.assertEqual(counts_per_camera_revolution(shaft), 2400)
        self.assertEqual(counts_per_camera_revolution(geared), 24_000)
        self.assertAlmostEqual(degrees_per_count(geared), 0.015)
        self.assertEqual(shaft["mapping"]["quadrature_multiplier"], 4)

    def test_quarter_steps_negative_and_accumulated_values_are_applied_directly(self):
        worker = live_worker()
        events = [(1_000_000_000 + index * 100_000, EV_REL, REL_CODES["REL_X"], 1) for index in range(2400)]
        worker.process_input_events(events)
        self.assertEqual(worker.snapshot(events[-1][0])["raw_count"], 2400)
        worker.process_input_events([(2_000_000_000, EV_REL, REL_CODES["REL_X"], -25)])
        worker.process_input_events([(2_100_000_000, EV_REL, REL_CODES["REL_X"], 125)])
        self.assertEqual(worker.snapshot(2_100_000_000)["raw_count"], 2500)

    def test_reference_uuid_clear_restart_and_disconnect_are_fail_safe(self):
        worker = live_worker()
        first = worker.set_reference(0)
        worker.clear_reference()
        second = worker.set_reference(10)
        self.assertNotEqual(first, second)
        worker.begin_acquisition("/dev/input/event9", "rotary-encoder")
        self.assertFalse(worker.snapshot()["referenced"])
        worker.set_reference(0)
        worker._set_error("device disconnected")
        snapshot = worker.snapshot()
        self.assertFalse(snapshot["referenced"])
        self.assertEqual(snapshot["health"], "ERROR")

    def test_syn_dropped_loses_reference_and_ignores_through_syn_report(self):
        worker = live_worker()
        worker.set_reference(0)
        worker.process_input_events([
            (1, EV_REL, REL_CODES["REL_X"], 5),
            (2, EV_SYN, SYN_DROPPED, 0),
            (3, EV_REL, REL_CODES["REL_X"], 100),
            (4, EV_SYN, SYN_REPORT, 0),
            (5, EV_REL, REL_CODES["REL_X"], 2),
        ])
        snapshot = worker.snapshot(5)
        self.assertEqual(snapshot["raw_count"], 7)
        self.assertFalse(snapshot["referenced"])
        self.assertTrue(snapshot["integrity_lost"])
        self.assertEqual(snapshot["integrity_loss_events"], 1)
        self.assertEqual(snapshot["evdev_dropped_events"], 1)

    def test_snapshot_polling_never_consumes_or_loses_position(self):
        worker = live_worker()
        worker.process_input_events([(1, EV_REL, REL_CODES["REL_X"], 321)])
        for _ in range(100):
            self.assertEqual(worker.snapshot(1)["raw_count"], 321)

    def test_native_input_event_records_are_parsed_in_batches_with_partial_tail(self):
        worker = live_worker()
        first = INPUT_EVENT_STRUCT.pack(1, 500, EV_REL, REL_CODES["REL_X"], 7)
        second = INPUT_EVENT_STRUCT.pack(1, 600, EV_REL, REL_CODES["REL_X"], -2)
        split = len(first) + 3
        worker.process_input_bytes((first + second)[:split])
        self.assertEqual(worker.snapshot(1_000_500_000)["raw_count"], 7)
        worker.process_input_bytes((first + second)[split:])
        self.assertEqual(worker.snapshot(1_000_600_000)["raw_count"], 5)

    def test_pan_tilt_axis_filtering_and_counts_are_independent(self):
        pan = live_worker("pan")
        tilt = live_worker("tilt")
        pan.process_input_events([(1, EV_REL, REL_CODES["REL_X"], 100), (2, EV_REL, REL_CODES["REL_Y"], 900)])
        tilt.process_input_events([(1, EV_REL, REL_CODES["REL_Y"], -50), (2, EV_REL, REL_CODES["REL_X"], 900)])
        self.assertEqual(pan.snapshot(2)["raw_count"], 100)
        self.assertEqual(tilt.snapshot(2)["raw_count"], -50)

    def test_peak_rate_requires_a_meaningful_window(self):
        worker = live_worker()
        worker.process_input_events([(1, EV_REL, 0, 1), (2, EV_REL, 0, 1), (3, EV_REL, 0, 1)])
        self.assertEqual(worker.snapshot(3)["peak_event_rate_hz"], 0)
        worker.process_input_events([(4_000_000 + index * 1_000_000, EV_REL, 0, 1) for index in range(101)])
        self.assertGreater(worker.snapshot(104_000_000)["peak_event_rate_hz"], 900)

    def test_windows_import_is_safe(self):
        self.assertIn("app.linux_rotary_source", sys.modules)


class LinuxRotaryDiscoveryCase(unittest.TestCase):
    def test_discovery_uses_rotary_name_and_expected_rel_axis_not_mouse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            add_input_device(root, "event0", "USB Mouse", (1 << 0) | (1 << 1))
            add_input_device(root, "event4", "rotary@11", 1 << REL_CODES["REL_X"])
            device, identity = discover_rotary_device("REL_X", sysfs_root=root, dev_root=Path("/dev/input"))
            self.assertEqual(device, str(Path("/dev/input") / "event4"))
            self.assertEqual(identity, "rotary@11")

    def test_ambiguous_and_missing_discovery_fail_instead_of_guessing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            add_input_device(root, "event1", "rotary-encoder", 1)
            add_input_device(root, "event2", "rotary_encoder", 1)
            with self.assertRaisesRegex(LinuxRotaryUnavailable, "Ambiguous"):
                discover_rotary_device("REL_X", sysfs_root=root)
            with self.assertRaisesRegex(LinuxRotaryUnavailable, "No rotary-encoder"):
                discover_rotary_device("REL_Y", sysfs_root=root)


class LinuxRotaryIntegrationCase(unittest.IsolatedAsyncioTestCase):
    async def test_two_referenced_workers_drive_validity_and_loss_is_axis_local(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory), SilentDebug())
            profile_id = store.profile_index["active_id"]
            await store.update_axis_config(profile_id, "pan", {"source": "linux_rotary", "mapping": {"encoder_revs_per_camera_rev": 10}})
            await store.update_axis_config(profile_id, "tilt", {"source": "linux_rotary", "mapping": {"encoder_revs_per_camera_rev": 10}})
            pan, tilt = live_worker("pan"), live_worker("tilt")
            pan.set_reference(0)
            store.sample_tick({"pan": pan.snapshot(), "tilt": tilt.snapshot()})
            self.assertFalse(store.tracking["valid"])
            tilt.set_reference(0)
            store.sample_tick({"pan": pan.snapshot(), "tilt": tilt.snapshot()})
            self.assertTrue(store.tracking["valid"])
            await store.generate_synthetic_world_calibration({"camera": {"x": 0, "y": 0, "z": 1.8}})
            self.assertEqual(
                store.world_calibration["tracking_reference_identity"],
                {"pan": pan.snapshot()["reference_id"], "tilt": tilt.snapshot()["reference_id"]},
            )
            self.assertTrue(store.camera["world_valid"])
            pan.process_input_events([(1, EV_SYN, SYN_DROPPED, 0)])
            store.sample_tick({"pan": pan.snapshot(), "tilt": tilt.snapshot()})
            self.assertFalse(store.tracking["valid"])
            self.assertFalse(store.camera["world_valid"])
            self.assertFalse(store.tracking["axes"]["pan"]["referenced"])
            self.assertTrue(store.tracking["axes"]["tilt"]["referenced"])

    async def test_fingerprint_covers_stable_rotary_identity_not_event_number(self):
        profile = {"axes": {"pan": rotary_config("pan", 10), "tilt": rotary_config("tilt", 10)}}
        original = tracking_fingerprint(profile)
        profile["axes"]["pan"]["source_config"]["device_override"] = "/dev/input/event99"
        self.assertEqual(original, tracking_fingerprint(profile))
        profile["axes"]["pan"]["source_config"]["pin_a"] = 18
        self.assertNotEqual(original, tracking_fingerprint(profile))

    async def test_source_manager_supports_dual_and_mixed_worker_types(self):
        created = []

        class FakeBase:
            def __init__(self, axis, config):
                self.axis, self.config, self.stopped = axis, config, False
                created.append(self)

            def start(self):
                pass

            def stop(self):
                self.stopped = True

            def snapshot(self):
                return {"axis": self.axis}

            def set_reference(self, _angle):
                return self.axis

            def clear_reference(self):
                pass

        class FakeGpio(FakeBase):
            pass
        class FakeRotary(FakeBase):
            pass

        profile = {"axes": {axis: default_axis_config(axis) for axis in ("pan", "tilt", "zoom", "focus")}}
        profile["axes"]["pan"] = rotary_config("pan")
        profile["axes"]["tilt"] = rotary_config("tilt")
        manager = TrackingSourceManager()
        with patch("app.source_manager.QuadratureGpioWorker", FakeGpio), patch("app.source_manager.LinuxRotaryWorker", FakeRotary):
            manager.configure("profile", profile)
            self.assertTrue(all(isinstance(worker, FakeRotary) for worker in manager._workers.values()))
            profile["axes"]["tilt"] = default_axis_config("tilt", "quadrature_gpio")
            manager.configure("profile", profile)
            self.assertIsInstance(manager._workers["pan"], FakeRotary)
            self.assertIsInstance(manager._workers["tilt"], FakeGpio)
            self.assertTrue(any(worker.axis == "tilt" and worker.stopped for worker in created))
            manager.stop()


if __name__ == "__main__":
    unittest.main()
