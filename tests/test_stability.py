import csv
import asyncio
import io
import json
import unittest
from types import SimpleNamespace

from app.stability import TrackingStabilityRecorder
from app.runtime import sample_tracking_once


class FakeClock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


def packet(sequence, *, session=7, pan=0, tilt=0, zoom=0, pico_us=None):
    return SimpleNamespace(
        sequence=sequence, session_id=session, pan=pan, tilt=tilt, zoom=zoom,
        pico_us=pico_us if pico_us is not None else sequence * 2_000,
    )


class TrackingStabilityRecorderCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.recorder = TrackingStabilityRecorder(clock=self.clock)

    def test_automatic_pre_post_trigger_on_pose_jump(self):
        samples = {axis: {"raw_count": 0, "input_raw_count": 0, "filtered_count": 0} for axis in ("pan", "tilt", "zoom")}
        axes = {axis: {"value": 0.0, "reference_id": f"{axis}-1"} for axis in ("pan", "tilt", "zoom")}
        tracking = {"axes": axes, "valid": True}
        camera = {"pan": 0.0, "tilt": 0.0}
        self.assertFalse(self.recorder.observe_acquisition(100.0, samples, tracking, camera))
        axes["pan"]["value"] = 1.0
        samples["pan"].update({"raw_count": 5, "input_raw_count": 5, "filtered_count": 5})
        self.assertTrue(self.recorder.observe_acquisition(100.01, samples, tracking, camera))
        status = self.recorder.status()
        self.assertTrue(status["automatic"])
        self.assertEqual(status["trigger_reason"], "large_pan_pose_change")
        self.assertGreaterEqual(status["event_count"], 2)

    def test_pose_jump_with_constant_input_is_classified_downstream(self):
        samples = {axis: {"raw_count": 0, "filtered_count": 0} for axis in ("pan", "tilt", "zoom")}
        axes = {axis: {"value": 0.0, "reference_id": f"{axis}-1"} for axis in ("pan", "tilt", "zoom")}
        tracking = {"axes": axes, "valid": True}
        self.recorder.observe_acquisition(100.0, samples, tracking, {"pan": 0.0, "tilt": 0.0})
        axes["tilt"]["value"] = 2.0
        self.recorder.observe_acquisition(100.01, samples, tracking, {"pan": 0.0, "tilt": 2.0})
        self.assertEqual(self.recorder.status()["trigger_reason"],
                         "downstream_large_tilt_pose_change_constant_counts")

    def test_large_scheduling_gap_automatically_records_correlation_event(self):
        self.recorder.observe_scheduling("acquisition", 75.0, 100.0)
        status = self.recorder.status()
        self.assertTrue(status["active"])
        self.assertIn("acquisition_scheduling_gap", status["trigger_reason"])
        self.assertEqual(status["summary"]["events_by_stage"]["scheduling_gap"], 1)

    def test_duration_and_mode_are_strict_and_inactive_path_records_nothing(self):
        self.recorder.record_raw_packet(packet(1), self.clock())
        self.assertEqual(self.recorder.status()["event_count"], 0)
        for duration in (9.9, 60.1, float("nan")):
            with self.assertRaises(ValueError):
                self.recorder.start(duration)
        with self.assertRaises(ValueError):
            self.recorder.start(10, mode="smoothing")

    def test_raw_counts_deltas_and_session_boundaries_are_preserved(self):
        self.recorder.start(10)
        self.recorder.record_raw_packet(packet(1, pan=100, tilt=-20), 100.001)
        self.recorder.record_raw_packet(packet(2, pan=101, tilt=-22), 100.003)
        self.recorder.record_raw_packet(packet(1, session=8, pan=900, tilt=400), 100.005)
        rows = [json.loads(row) for row in self.recorder.export_jsonl().splitlines()]
        self.assertEqual(rows[0]["deltas"], {"pan": 0, "tilt": 0, "zoom": 0})
        self.assertEqual(rows[1]["deltas"], {"pan": 1, "tilt": -2, "zoom": 0})
        self.assertEqual(rows[2]["deltas"], {"pan": 0, "tilt": 0, "zoom": 0})
        summary = self.recorder.status()["summary"]
        self.assertEqual(summary["raw_movement_events"]["pan"], 1)
        self.assertEqual(summary["raw_movement_events"]["tilt"], 1)

    def test_acquisition_publication_and_exports_correlate_pipeline_stages(self):
        self.recorder.start(10)
        samples = {
            "pan": {"raw_count": 11, "pico_session": 7, "packet_seq": 4, "packet_age_ms": 1.2},
            "tilt": {"raw_count": 20, "pico_session": 7, "packet_seq": 4, "packet_age_ms": 1.2},
        }
        tracking = {"valid": True, "integrity": "TRUSTED", "axes": {
            "pan": {"raw_count": 11, "value": 1.25, "reference_id": "p", "health": "VALID", "valid": True},
            "tilt": {"raw_count": 20, "value": -2.5, "reference_id": "t", "health": "VALID", "valid": True},
            "zoom": {"raw_count": 0, "value": None, "health": "DISABLED", "valid": False},
        }}
        camera = {"x": 1, "y": 2, "z": 3, "pan": 1.25, "tilt": -2.5, "roll": 0, "fov": 50, "valid": True}
        self.recorder.record_acquisition(100.01, samples, tracking, camera, camera)
        pose = {"server_mono": 100.02, "render_camera": camera, "tracking": {"valid": True}}
        self.recorder.record_publication(100.02, pose, {"clients": 1, "queued_messages": 0, "coalesced_poses": 2})
        status = self.recorder.status()
        self.assertEqual(status["summary"]["events_by_stage"]["acquisition"], 1)
        self.assertEqual(status["summary"]["events_by_stage"]["publication"], 1)
        rows = list(csv.reader(io.StringIO(self.recorder.export_csv())))
        self.assertEqual(rows[0][0:2], ["stage", "mono"])
        self.assertEqual(len(rows), 3)

    def test_fixed_pose_is_session_only_and_expires_automatically(self):
        self.recorder.start(10, mode="fixed_pose", fixed_pose={"pan": 12, "tilt": -4, "fov": 30})
        source = {"pan": 99, "tilt": 88, "valid": False}
        fixed = self.recorder.override_render_pose(source, 100.1)
        self.assertEqual((fixed["pan"], fixed["tilt"], fixed["fov"]), (12, -4, 30))
        self.assertEqual(fixed["source"], "diagnostic_fixed_pose")
        self.clock.value = 110.1
        self.assertFalse(self.recorder.status()["active"])
        self.assertIs(self.recorder.override_render_pose(source, 110.1), source)

    def test_capacity_is_bounded_and_reports_overwrites(self):
        recorder = TrackingStabilityRecorder(capacity=1_000, clock=self.clock)
        recorder.start(10)
        for sequence in range(1_005):
            recorder.record_raw_packet(packet(sequence), 100 + sequence / 10_000)
        status = recorder.status()
        self.assertEqual(status["event_count"], 1_000)
        self.assertEqual(status["dropped_by_capacity"], 5)


class StabilityInactiveRuntimeCase(unittest.IsolatedAsyncioTestCase):
    async def test_inactive_probe_does_not_request_an_extra_render_pose(self):
        recorder = TrackingStabilityRecorder()

        class Store:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.runtime_profiler = None
                self.stability_recorder = recorder
                self.camera = {"pan": 0.0, "tilt": 0.0}
                self.tracking = {"valid": True, "axes": {
                    axis: {"value": 0.0, "reference_id": f"{axis}-ref"}
                    for axis in ("pan", "tilt", "zoom")}}

            def sample_tick(self, samples, now):
                pass

            def _render_pose_state(self, now):
                raise AssertionError("inactive recorder must not calculate render pose")

        class Source:
            def snapshots(self):
                return {axis: {"raw_count": 0} for axis in ("pan", "tilt", "zoom")}

        await sample_tracking_once(Store(), Source(), 100.0)


if __name__ == "__main__":
    unittest.main()
