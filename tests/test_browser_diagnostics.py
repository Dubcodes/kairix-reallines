from __future__ import annotations

import unittest

from app.browser_diagnostics import BrowserDiagnostics


class BrowserDiagnosticsCase(unittest.TestCase):
    def test_reports_are_rate_limited_bounded_and_expire(self):
        now = [10.0]
        diagnostics = BrowserDiagnostics(capacity=2, minimum_interval=1.0, ttl=5.0, clock=lambda: now[0])
        metrics = {"incoming_pose_hz": 50, "animation_frame_hz": 60, "held_poses": 1,
                   "incoming_pose_hz_5s": 49.8, "animation_frame_hz_5s": 50,
                   "visibility_state": "visible", "transport_mode": "websocket",
                   "presentation_timing_available": False}
        self.assertTrue(diagnostics.report("obs-a", "gfx", "OBS", metrics))
        self.assertFalse(diagnostics.report("obs-a", "gfx", "OBS", metrics))
        now[0] += 1.1
        self.assertTrue(diagnostics.report("laptop", "preview", "Laptop", metrics))
        self.assertTrue(diagnostics.report("control", "control", "Operator", metrics))
        reports = diagnostics.snapshot()
        self.assertEqual([(item["client_id"], item["page"]) for item in reports],
                         [("laptop", "preview"), ("control", "control")])
        now[0] += 5.1
        self.assertEqual(diagnostics.snapshot(), [])

    def test_rolling_text_and_capability_metrics_are_bounded(self):
        diagnostics = BrowserDiagnostics()
        diagnostics.report("instance-a", "gfx", "OBS", {
            "animation_frame_hz_5s": 50, "frame_interval_p99_ms_5s": 21.2,
            "visibility_state": "visible", "transport_mode": "websocket",
            "presentation_timing_available": False,
        })
        report = diagnostics.snapshot()[0]["metrics"]
        self.assertEqual(report["visibility_state"], "visible")
        self.assertEqual(report["transport_mode"], "websocket")
        self.assertFalse(report["presentation_timing_available"])

    def test_rejects_unbounded_or_nonfinite_input(self):
        diagnostics = BrowserDiagnostics()
        with self.assertRaises(ValueError):
            diagnostics.report("bad id", "gfx", None, {})
        with self.assertRaises(ValueError):
            diagnostics.report("client", "unknown", None, {})
        with self.assertRaises(ValueError):
            diagnostics.report("client", "gfx", None, {"render_mean_ms": float("inf")})


if __name__ == "__main__":
    unittest.main()
