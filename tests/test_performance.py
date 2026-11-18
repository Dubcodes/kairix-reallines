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

    def test_non_finite_gauges_are_rejected(self):
        profiler = RuntimeProfiler()
        profiler.observe("bad", float("nan"))
        self.assertNotIn("bad", profiler.snapshot()["gauges"])


if __name__ == "__main__":
    unittest.main()
