"""V1.6F1: recording/export semantics and lightweight diagnostics-only history."""
import csv
import io
import json
import unittest
from app.stability import TrackingStabilityRecorder

class FakeClock:
    def __init__(self): self.value = 1000.0
    def __call__(self): return self.value

class RollingCaptureTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.recorder = TrackingStabilityRecorder(clock=self.clock)

    def pose(self, pan=0.0):
        return {
            'pose_sequence': int(self.clock.value * 50), 'server_mono': self.clock.value,
            'camera': {'pan': pan, 'tilt': 0.0, 'valid': True},
            'render_camera': {'pan': pan, 'tilt': 0.0, 'valid': True},
            'tracking': {'valid': True, 'integrity': 'TRUSTED', 'axes': {
                'pan': {'raw_count': int(pan * 30), 'value': pan, 'valid': True, 'reference_id': 'ref'},
                'tilt': {'raw_count': 0, 'value': 0.0, 'valid': True, 'reference_id': 'ref'},
                'zoom': {'raw_count': 0, 'valid': True, 'reference_id': 'ref'},
            }}
        }

    def publish(self, pan=0.0):
        self.recorder.record_publication(self.clock.value, self.pose(pan), {'clients': 1}, {})

    def test_rolling_history_works_without_manual_start(self):
        for i in range(50):
            self.clock.value += 0.02
            self.publish(i * 0.5)
        data = [json.loads(line) for line in self.recorder.export_jsonl('recent').splitlines()]
        self.assertEqual(data[0]['stage'], 'export_metadata')
        self.assertEqual(len([row for row in data if row['stage'] == 'publication']), 50)
        self.assertEqual(data[-1]['axes']['pan']['raw_count'], int(24.5 * 30))
        self.assertIn('utc_s', data[-1])
        self.assertEqual(self.recorder.status()['recent']['event_count'], 50)
        self.assertEqual(self.recorder.export_jsonl('capture'), '')

    def test_expired_history_and_csv_metadata(self):
        self.publish(1.0)
        self.clock.value += 601
        self.publish(2.0)
        rows = list(csv.DictReader(io.StringIO(self.recorder.export_csv('recent'))))
        self.assertEqual(rows[0]['stage'], 'export_metadata')
        publications = [row for row in rows if row['stage'] == 'publication']
        self.assertEqual(len(publications), 1)
        self.assertEqual(json.loads(publications[0]['payload_json'])['render_pose']['pan'], 2.0)

    def test_manual_and_recent_are_independent(self):
        self.publish(1.0)
        self.recorder.start(10)
        self.clock.value += 0.02
        self.publish(2.0)
        self.recorder.stop()
        self.clock.value += 0.02
        self.publish(3.0)
        detailed = [json.loads(row) for row in self.recorder.export_jsonl('capture').splitlines()]
        recent = [json.loads(row) for row in self.recorder.export_jsonl('recent').splitlines()]
        self.assertEqual(len([row for row in detailed if row['stage'] == 'publication']), 1)
        self.assertEqual(len([row for row in recent if row['stage'] == 'publication']), 3)

    def test_gap_and_browser_metrics_are_in_recent(self):
        self.recorder.observe_scheduling('publication', 47, self.clock.value)
        self.recorder.record_browser_metrics({'page':'gfx','label':'OBS','metrics':{
            'frame_interval_p95_ms_5s': 22.0, 'incoming_pose_hz_5s': 49.1}}, self.clock.value)
        stages = [json.loads(row)['stage'] for row in self.recorder.export_jsonl('recent').splitlines()]
        self.assertIn('scheduling_gap', stages)
        self.assertIn('browser_metrics', stages)

    def test_invalid_scope_rejected(self):
        with self.assertRaises(ValueError): self.recorder.export_jsonl('malformed')

if __name__ == '__main__': unittest.main()
