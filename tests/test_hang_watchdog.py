import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.hang_watchdog import EventLoopWatchdog


class EventLoopWatchdogCase(unittest.TestCase):
    def test_stall_dump_is_bounded_by_cooldown_and_labels_live_process(self):
        now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            watchdog = EventLoopWatchdog(Path(directory), stall_seconds=2, cooldown_seconds=10,
                                          clock=lambda: now[0])
            watchdog.beat()
            now[0] = 103.0
            with patch("app.hang_watchdog.faulthandler.dump_traceback") as dump:
                self.assertTrue(watchdog.check_once())
                self.assertFalse(watchdog.check_once(104.0))
                self.assertTrue(watchdog.check_once(114.0))
                self.assertEqual(dump.call_count, 2)
            text = watchdog.path.read_text(encoding="utf-8")
            self.assertIn("event-loop heartbeat stalled; process still alive", text)
            watchdog.stop()

    def test_heartbeat_prevents_false_stall(self):
        with tempfile.TemporaryDirectory() as directory:
            watchdog = EventLoopWatchdog(Path(directory), stall_seconds=2, clock=lambda: 10.0)
            watchdog.beat(9.0)
            self.assertFalse(watchdog.check_once(10.0))
            self.assertFalse(watchdog.path.exists())


if __name__ == "__main__":
    unittest.main()
