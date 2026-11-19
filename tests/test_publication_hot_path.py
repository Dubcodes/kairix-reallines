import unittest
from unittest.mock import patch

from app.stability import TrackingStabilityRecorder


class PublicationHotPathTests(unittest.TestCase):
    def setUp(self):
        self.recorder = TrackingStabilityRecorder()
        self.pose = {
            "render_camera": {
                "x": 0, "y": 0, "z": 2, "pan": 3, "tilt": -1,
                "roll": 0, "fov": 50, "valid": True,
                "world_valid": True, "render_timestamp": 1.0,
                "interpolation_status": "OK",
            },
            "tracking": {"valid": True},
            "server_mono": 1.0,
        }
        self.context = {
            "overlay_revision": 1,
            "reference_observation_id": "a",
            "reference_points": {"a": {"x": 1.0}},
            "world_calibration_id": "world-a",
            "world_valid": True,
        }

    def test_inactive_publication_does_not_deepcopy(self):
        # The routine is called continuously, even when nobody requested capture.
        with patch("app.stability.deepcopy", side_effect=AssertionError("hot-path deepcopy")):
            self.recorder.record_publication(1.0, self.pose, {}, self.context)
            self.recorder.record_publication(1.02, self.pose, {}, self.context)
        self.assertFalse(self.recorder.status()["active"])

    def test_nested_reference_change_still_triggers(self):
        self.recorder.record_publication(1.0, self.pose, {}, self.context)
        self.context["reference_points"]["a"]["x"] = 2.0
        self.recorder.record_publication(1.02, self.pose, {}, self.context)
        status = self.recorder.status()
        self.assertTrue(status["active"])
        self.assertIn("calibration_overlay_state_changed", status["trigger_reason"])

    def test_active_capture_retains_independent_context(self):
        self.recorder.start(10.0, mode="live")
        self.recorder.record_publication(1.0, self.pose, {}, self.context)
        self.context["reference_points"]["a"]["x"] = 3.0
        event = next(item for item in self.recorder._events if item["stage"] == "publication")
        self.assertEqual(event["diagnostic_context"]["reference_points"]["a"]["x"], 1.0)


if __name__ == "__main__":
    unittest.main()
