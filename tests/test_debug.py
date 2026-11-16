import json
import tempfile
import unittest
from pathlib import Path

from app.debug import DebugRecorder


class DebugRecorderCase(unittest.TestCase):
    def test_routine_events_are_ram_first_and_flush_on_request(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = DebugRecorder(Path(directory), flush_interval=60)
            try:
                recorder.app("routine", value=1)
                path = recorder.session_dir / "application.jsonl"
                self.assertFalse(path.exists())
                self.assertEqual(recorder.status()["pending_events"], 1)
                self.assertTrue(recorder.status()["sudden_power_loss_may_lose_pending"])
                recorder.flush()
                self.assertEqual(json.loads(path.read_text())["event"], "routine")
                self.assertEqual(recorder.status()["pending_events"], 0)
            finally:
                recorder.close()

    def test_errors_are_preserved_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = DebugRecorder(Path(directory), flush_interval=60)
            try:
                recorder.error("important", detail="failure")
                row = json.loads((recorder.session_dir / "errors.jsonl").read_text())
                self.assertEqual(row["event"], "important")
                self.assertEqual(row["payload"]["detail"], "failure")
            finally:
                recorder.close()

    def test_queue_is_bounded_and_reports_dropped_routine_events(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = DebugRecorder(Path(directory), queue_capacity=64, flush_interval=60)
            try:
                for value in range(70):
                    recorder.tracking("sample", value=value)
                status = recorder.status()
                self.assertEqual(status["pending_events"], 64)
                self.assertEqual(status["dropped_events"], 6)
                recorder.flush()
                rows = (recorder.session_dir / "tracking.jsonl").read_text().splitlines()
                self.assertEqual(len(rows), 64)
                self.assertEqual(json.loads(rows[0])["payload"]["value"], 6)
            finally:
                recorder.close()


if __name__ == "__main__":
    unittest.main()
