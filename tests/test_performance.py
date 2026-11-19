import threading
import unittest

from app.performance import RuntimeProfiler


class RuntimeProfilerCase(unittest.TestCase):
    def test_rolling_timings_and_gauges_are_bounded(self):
        profiler = RuntimeProfiler(capacity=32)
        for value in range(40):
            profiler.observe("loop_jitter_ms", value)
            with profiler.measure("stage"):
                sum(range(10))
        snapshot = profiler.snapshot()
        self.assertEqual(snapshot["window_capacity"], 32)
        self.assertEqual(snapshot["gauges"]["loop_jitter_ms"]["samples"], 32)
        self.assertEqual(snapshot["gauges"]["loop_jitter_ms"]["max"], 39)
        self.assertEqual(snapshot["gauges"]["loop_jitter_ms"]["p99"], 39)
        self.assertEqual(snapshot["timings_ms"]["stage"]["wall"]["samples"], 32)
        self.assertGreaterEqual(snapshot["timings_ms"]["stage"]["process_cpu"]["mean"], 0)

    def test_loop_rates_targets_and_missed_deadlines_are_explicit(self):
        profiler = RuntimeProfiler(capacity=32)
        for tick in range(21):
            profiler.observe_loop("acquisition", tick * .005, 200, 7 if tick == 10 else 0)
        loop = profiler.snapshot()["loops"]["acquisition"]
        self.assertEqual(loop["target_hz"], 200)
        self.assertEqual(loop["actual_hz"], 200)
        self.assertEqual(loop["missed_deadlines_since_start"], 1)

    def test_stall_gc_overlap_is_timestamped_and_bounded(self):
        profiler = RuntimeProfiler(capacity=32)
        profiler._gc_intervals.append((10.0, 10.05, 50.0))
        profiler.record_stall("acquisition", 10.06, 70)
        profiler.record_stall("publication", 12.0, 30)
        profiler.record_stall("acquisition", 13.0, 2)
        events = profiler.snapshot()["recent_scheduler_stalls"]
        self.assertEqual(len(events), 2)
        self.assertTrue(events[0]["gc_overlapping"])
        self.assertEqual(events[0]["gc_overlap_durations_ms"], [50.0])
        self.assertFalse(events[1]["gc_overlapping"])

    def test_non_finite_gauges_are_rejected(self):
        profiler = RuntimeProfiler()
        profiler.observe("bad", float("nan"))
        self.assertNotIn("bad", profiler.snapshot()["gauges"])

    def test_gc_callback_never_reenters_general_profiler_lock(self):
        profiler = RuntimeProfiler(capacity=32)
        completed = threading.Event()

        def invoke_while_owned():
            with profiler._lock:
                profiler._gc_callback("start", {})
                profiler._gc_callback("stop", {"uncollectable": 2})
            completed.set()

        thread = threading.Thread(target=invoke_while_owned, daemon=True)
        thread.start()
        self.assertTrue(completed.wait(0.5), "GC callback self-deadlocked on profiler lock")
        thread.join(0.5)
        snapshot = profiler.snapshot()
        self.assertEqual(snapshot["garbage_collection"]["collections_since_monitor_start"], 1)
        self.assertEqual(snapshot["garbage_collection"]["uncollectable_since_monitor_start"], 2)
        self.assertEqual(snapshot["gauges"]["gc_pause_ms"]["samples"], 1)


if __name__ == "__main__":
    unittest.main()
