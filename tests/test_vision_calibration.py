import base64
import tempfile
import unittest
from pathlib import Path

from app.vision_calibration import OpenCvTemplateDetector, VisionCalibrationError, VisionCalibrationService

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = np = None


class FakeDetector:
    def teach(self, image, markers):
        return {"image_width": 1920, "image_height": 1080, "templates": {key: {"x": value["x"], "y": value["y"], "png": "x"} for key, value in markers.items()}}

    def detect(self, image, target):
        return {"points": {key: {"x": value["x"], "y": value["y"], "confidence": .96, "method": "automatic"} for key, value in target["templates"].items()},
                "minimum_confidence": .96, "ambiguous": False, "accepted": True}

    def stability(self, first, second):
        return {"stable": True, "mean_pixel_change": .2, "reason": None}


class FakeObs:
    def __init__(self):
        self.calls = 0

    async def screenshot(self, host, port, password, source):
        self.calls += 1
        return b"frame" + bytes([self.calls])


def context(pan=100):
    return {"tracking_valid": True, "references": {"pan": "P", "tilt": "T", "zoom": "Z"},
            "axes": {"pan": {"raw_count": pan}, "tilt": {"raw_count": 200}, "zoom": {"relative_count": 300}},
            "camera": {"pan": 1.0, "tilt": 2.0}, "captured_mono": 10.0}


class VisionCalibrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.service = VisionCalibrationService(Path(self.temporary.name), detector=FakeDetector(), obs_client=FakeObs())
        self.markers = {"M1": {"x": .2, "y": .2}, "M2": {"x": .8, "y": .2}, "M3": {"x": .5, "y": .8}}
        await self.service.teach(b"image", self.markers, "target-1")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_manual_result_is_review_only_and_capture_is_immutable(self):
        self.service.start(); captured = context()
        result = await self.service.analyse(b"still", captured, source="manual")
        captured["axes"]["pan"]["raw_count"] = 999
        self.assertFalse(result["accepted_for_review"])
        self.assertEqual(result["capture_context"]["axes"]["pan"]["raw_count"], 100)
        self.assertEqual(result["points"]["M1"]["method"], "automatic")

    async def test_obs_pair_requires_stable_encoder_counts(self):
        self.service.start(); self.service.configure_obs({"host": "127.0.0.1", "port": 4455, "source": "Camera", "password": "secret"})
        samples = iter((context(100), context(110)))
        result = await self.service.capture_obs_pair(lambda: next(samples))
        self.assertFalse(result["accepted_for_review"])
        self.assertFalse(result["frame_stability"]["encoder_stable"])
        status = self.service.status()
        self.assertNotIn("password", status["obs"])
        self.assertNotIn("templates", status["target"])

    async def test_obs_pair_accepts_identity_preserving_stable_detection(self):
        self.service.start(); self.service.configure_obs({"host": "obs", "source": "Camera", "password": "secret"})
        result = await self.service.capture_obs_pair(lambda: context())
        self.assertTrue(result["accepted_for_review"])
        self.assertEqual(set(result["points"]), set(self.markers))

    async def test_inactive_and_busy_paths_are_bounded(self):
        with self.assertRaises(VisionCalibrationError):
            await self.service.analyse(b"still", context())
        self.assertEqual(self.service.status()["queue_capacity"], 1)


@unittest.skipUnless(cv2 is not None, "OpenCV is an optional runtime dependency")
class OpenCvDetectorTests(unittest.TestCase):
    @staticmethod
    def encode(image):
        ok, value = cv2.imencode(".png", image)
        if not ok:
            raise AssertionError("fixture encoding failed")
        return value.tobytes()

    def test_subpixel_identity_detection_and_ambiguity_rejection(self):
        detector = OpenCvTemplateDetector()
        rng = np.random.default_rng(14)
        image = np.zeros((240, 320), dtype=np.uint8)
        centers = {"M1": (.30, .40), "M2": (.70, .42), "M3": (.52, .72)}
        for x_fraction, y_fraction in centers.values():
            x, y = round(x_fraction * 320), round(y_fraction * 240)
            image[y-8:y+9, x-8:x+9] = rng.integers(0, 256, (17, 17), dtype=np.uint8)
        taught = detector.teach(self.encode(image), {key: {"x": x, "y": y} for key, (x, y) in centers.items()})
        shifted = cv2.warpAffine(image, np.float32([[1, 0, 3.25], [0, 1, 1.75]]), (320, 240))
        result = detector.detect(self.encode(shifted), taught)
        self.assertTrue(result["accepted"])
        self.assertAlmostEqual(result["points"]["M1"]["x"], centers["M1"][0] + 3.25 / 320, delta=.006)
        self.assertAlmostEqual(result["points"]["M1"]["y"], centers["M1"][1] + 1.75 / 240, delta=.006)

        template = cv2.imdecode(np.frombuffer(base64.b64decode(taught["templates"]["M1"]["png"]), np.uint8), cv2.IMREAD_GRAYSCALE)
        ambiguous = np.zeros_like(image); th, tw = template.shape
        for x, y in ((80, 96), (112, 96)):
            ambiguous[y-th//2:y-th//2+th, x-tw//2:x-tw//2+tw] = template
        single_target = {**taught, "templates": {"M1": taught["templates"]["M1"]}}
        uncertain = detector.detect(self.encode(ambiguous), single_target)
        self.assertTrue(uncertain["ambiguous"])
        self.assertFalse(uncertain["accepted"])


if __name__ == "__main__":
    unittest.main()
