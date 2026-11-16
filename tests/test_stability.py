import csv
import io
import json
import unittest
from types import SimpleNamespace

from app.stability import TrackingStabilityRecorder


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


if __name__ == "__main__":
    unittest.main()
