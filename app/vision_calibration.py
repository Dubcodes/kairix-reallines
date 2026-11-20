from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import time
import uuid
from collections import deque
from copy import deepcopy
from pathlib import Path
from typing import Any


MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_COMPLETED = 20


class VisionCalibrationError(ValueError):
    pass


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class ObsScreenshotClient:
    """Minimal OBS WebSocket v5 screenshot client; it never changes OBS state."""

    async def screenshot(self, host: str, port: int, password: str, source: str) -> bytes:
        import websockets

        uri = f"ws://{host}:{port}"
        try:
            async with websockets.connect(uri, open_timeout=5, close_timeout=2, max_size=MAX_IMAGE_BYTES * 2) as socket:
                hello = json.loads(await asyncio.wait_for(socket.recv(), 5))
                if hello.get("op") != 0:
                    raise VisionCalibrationError("OBS did not send a WebSocket v5 hello")
                auth = (hello.get("d") or {}).get("authentication")
                identify: dict[str, Any] = {"rpcVersion": 1}
                if auth:
                    secret = base64.b64encode(hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
                    identify["authentication"] = base64.b64encode(
                        hashlib.sha256((secret + auth["challenge"]).encode()).digest()
                    ).decode()
                await socket.send(json.dumps({"op": 1, "d": identify}))
                identified = json.loads(await asyncio.wait_for(socket.recv(), 5))
                if identified.get("op") != 2:
                    raise VisionCalibrationError("OBS authentication failed")
                request_id = str(uuid.uuid4())
                await socket.send(json.dumps({"op": 6, "d": {
                    "requestType": "GetSourceScreenshot", "requestId": request_id,
                    "requestData": {"sourceName": source, "imageFormat": "png", "imageCompressionQuality": 100},
                }}))
                while True:
                    response = json.loads(await asyncio.wait_for(socket.recv(), 8))
                    data = response.get("d") or {}
                    if response.get("op") != 7 or data.get("requestId") != request_id:
                        continue
                    status = data.get("requestStatus") or {}
                    if not status.get("result"):
                        raise VisionCalibrationError(status.get("comment") or "OBS screenshot failed")
                    encoded = (data.get("responseData") or {}).get("imageData", "")
                    if "," not in encoded:
                        raise VisionCalibrationError("OBS returned an invalid screenshot")
                    image = base64.b64decode(encoded.split(",", 1)[1], validate=True)
                    if not image or len(image) > MAX_IMAGE_BYTES:
                        raise VisionCalibrationError("OBS screenshot is empty or too large")
                    return image
        except VisionCalibrationError:
            raise
        except Exception as exc:
            raise VisionCalibrationError(f"OBS screenshot unavailable: {exc}") from exc


class OpenCvTemplateDetector:
    """Identity-preserving local template matching around taught positions."""

    @staticmethod
    def _cv():
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise VisionCalibrationError(
                "Automatic detection needs opencv-python-headless; manual Dot Calibration remains available"
            ) from exc
        return cv2, np

    def decode(self, image: bytes):
        cv2, np = self._cv()
        frame = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if frame is None or min(frame.shape[:2]) < 64:
            raise VisionCalibrationError("The calibration image could not be decoded or is too small")
        return frame

    def teach(self, image: bytes, markers: dict[str, dict[str, float]]) -> dict[str, Any]:
        cv2, _ = self._cv()
        frame = self.decode(image); height, width = frame.shape[:2]
        radius = max(8, min(28, round(min(width, height) * 0.014)))
        templates: dict[str, Any] = {}
        for marker_id, point in markers.items():
            x, y = round(float(point["x"]) * width), round(float(point["y"]) * height)
            if x - radius < 0 or y - radius < 0 or x + radius >= width or y + radius >= height:
                raise VisionCalibrationError(f"{marker_id} is too close to the image edge to teach")
            patch = frame[y-radius:y+radius+1, x-radius:x+radius+1]
            ok, encoded = cv2.imencode(".png", patch)
            if not ok:
                raise VisionCalibrationError(f"Could not encode template {marker_id}")
            templates[marker_id] = {
                "x": float(point["x"]), "y": float(point["y"]),
                "png": base64.b64encode(encoded.tobytes()).decode(),
            }
        return {"image_width": width, "image_height": height, "search_fraction": 0.18, "templates": templates}

    def detect(self, image: bytes, target: dict[str, Any]) -> dict[str, Any]:
        cv2, np = self._cv(); frame = self.decode(image); height, width = frame.shape[:2]
        found: dict[str, Any] = {}; search_fraction = float(target.get("search_fraction", 0.18))
        for marker_id, taught in (target.get("templates") or {}).items():
            template = cv2.imdecode(np.frombuffer(base64.b64decode(taught["png"]), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            th, tw = template.shape[:2]; cx, cy = float(taught["x"]) * width, float(taught["y"]) * height
            sx, sy = round(width * search_fraction), round(height * search_fraction)
            left, top = max(0, round(cx) - sx), max(0, round(cy) - sy)
            right, bottom = min(width, round(cx) + sx), min(height, round(cy) + sy)
            region = frame[top:bottom, left:right]
            if region.shape[0] < th or region.shape[1] < tw:
                continue
            scores = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
            _, confidence, _, location = cv2.minMaxLoc(scores)
            lx, ly = location

            def peak_offset(before: float, center: float, after: float) -> float:
                denominator = before - 2.0 * center + after
                if abs(denominator) < 1e-9:
                    return 0.0
                return max(-0.75, min(0.75, 0.5 * (before - after) / denominator))

            sub_x = peak_offset(float(scores[ly, lx - 1]), float(scores[ly, lx]), float(scores[ly, lx + 1])) if 0 < lx < scores.shape[1] - 1 else 0.0
            sub_y = peak_offset(float(scores[ly - 1, lx]), float(scores[ly, lx]), float(scores[ly + 1, lx])) if 0 < ly < scores.shape[0] - 1 else 0.0
            alternatives = scores.copy()
            alternatives[max(0, ly-th):min(scores.shape[0], ly+th+1),
                         max(0, lx-tw):min(scores.shape[1], lx+tw+1)] = -1.0
            second = float(alternatives.max()) if alternatives.size else -1.0
            marker_ambiguous = second >= float(confidence) - 0.03
            px, py = left + lx + sub_x + tw / 2.0, top + ly + sub_y + th / 2.0
            found[marker_id] = {"x": px / width, "y": py / height, "confidence": float(confidence),
                                "second_confidence": second, "ambiguous": marker_ambiguous,
                                "method": "automatic"}
        positions = [(round(value["x"] * width), round(value["y"] * height)) for value in found.values()]
        ambiguous = len(set(positions)) != len(positions) or any(item["ambiguous"] for item in found.values())
        minimum = min((item["confidence"] for item in found.values()), default=0.0)
        accepted = bool(found and not ambiguous and minimum >= 0.70)
        return {"points": found, "minimum_confidence": minimum, "ambiguous": ambiguous, "accepted": accepted,
                "image_width": width, "image_height": height}

    def stability(self, first: bytes, second: bytes) -> dict[str, Any]:
        cv2, _ = self._cv(); a, b = self.decode(first), self.decode(second)
        if a.shape != b.shape:
            return {"stable": False, "mean_pixel_change": None, "reason": "frame dimensions changed"}
        mean = float(cv2.absdiff(a, b).mean())
        return {"stable": mean <= 2.5, "mean_pixel_change": mean, "reason": None if mean <= 2.5 else "video features still moving"}


class VisionCalibrationService:
    def __init__(self, data_dir: Path, *, detector=None, obs_client=None):
        self.data_dir = data_dir
        self.target_path = data_dir / "vision_targets.json"
        self.secret_path = data_dir / "vision_obs_secret.json"
        self.detector = detector or OpenCvTemplateDetector()
        self.obs_client = obs_client or ObsScreenshotClient()
        self.active = False
        self.processing = False
        self.completed: deque[dict[str, Any]] = deque(maxlen=MAX_COMPLETED)
        self._gate = asyncio.Semaphore(1)
        self.target = self._read_json(self.target_path, {})

    @staticmethod
    def _read_json(path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return deepcopy(default)

    def status(self) -> dict[str, Any]:
        secret = self._read_json(self.secret_path, {})
        target_summary = None if not self.target else {
            "target_id": self.target.get("target_id"), "taught_at": self.target.get("taught_at"),
            "image_width": self.target.get("image_width"), "image_height": self.target.get("image_height"),
            "marker_ids": sorted((self.target.get("templates") or {}).keys()),
        }
        return {"active": self.active, "processing": self.processing, "queue_capacity": 1,
                "completed": list(self.completed), "target": target_summary,
                "opencv_available": self._opencv_available(),
                "obs": {"configured": bool(secret.get("host") and secret.get("source")),
                        "host": secret.get("host"), "port": secret.get("port", 4455), "source": secret.get("source")}}

    @staticmethod
    def _opencv_available() -> bool:
        try:
            import cv2  # noqa: F401
            return True
        except ImportError:
            return False

    def start(self) -> dict[str, Any]:
        self.active = True
        return self.status()

    def stop(self) -> dict[str, Any]:
        self.active = False
        return self.status()

    def configure_obs(self, values: dict[str, Any]) -> dict[str, Any]:
        host = str(values.get("host") or "").strip(); source = str(values.get("source") or "").strip()
        port = int(values.get("port", 4455)); password = str(values.get("password") or "")
        if not host or not source or not 1 <= port <= 65535:
            raise VisionCalibrationError("OBS host, port and camera source are required")
        _atomic_json(self.secret_path, {"host": host, "port": port, "source": source, "password": password})
        try:
            os.chmod(self.secret_path, 0o600)
        except OSError:
            pass
        return self.status()["obs"]

    async def teach(self, image: bytes, markers: dict[str, dict[str, float]], target_id: str) -> dict[str, Any]:
        self._validate_image(image)
        if len(markers) < 3:
            raise VisionCalibrationError("Teach at least three labelled target markers")
        taught = await asyncio.to_thread(self.detector.teach, bytes(image), deepcopy(markers))
        taught.update({"target_id": target_id, "taught_at": time.time()})
        self.target = taught; _atomic_json(self.target_path, taught)
        return {"target_id": target_id, "taught_at": taught["taught_at"],
                "image_width": taught.get("image_width"), "image_height": taught.get("image_height"),
                "marker_ids": sorted((taught.get("templates") or {}).keys())}

    async def analyse(self, image: bytes, capture_context: dict[str, Any], *, stability=None, source="manual") -> dict[str, Any]:
        if not self.active:
            raise VisionCalibrationError("Start Vision Calibration before processing images")
        if not self.target:
            raise VisionCalibrationError("Teach the measured target before automatic detection")
        self._validate_image(image)
        if self._gate.locked():
            raise VisionCalibrationError("Vision worker is busy; the bounded queue accepts one frame at a time")
        immutable_image, immutable_context = bytes(image), deepcopy(capture_context)
        async with self._gate:
            self.processing = True
            try:
                detection = await asyncio.to_thread(self.detector.detect, immutable_image, deepcopy(self.target))
            finally:
                self.processing = False
        tracking_ok = bool(immutable_context.get("tracking_valid"))
        stable = bool(stability and stability.get("stable"))
        detection["accepted_for_review"] = bool(detection.get("accepted") and tracking_ok and stable)
        detection.update({"id": str(uuid.uuid4()), "source": source, "capture_context": immutable_context,
                          "frame_stability": stability or {"stable": False, "reason": "single imported still; verify manually"},
                          "target_id": self.target.get("target_id")})
        self.completed.appendleft(deepcopy(detection))
        return detection

    async def capture_obs_motion(self, context_provider, *, interval_seconds: float = 3.0) -> dict[str, Any]:
        """Measure actual camera-only optical flow against two encoder samples.

        For the operator: point at stationary distant landmarks, start the test,
        then turn only Pan OR Tilt during the next three seconds. OBS screenshots
        must contain *camera video only* rather than the composite output.
        All results are review-only and never change a live calibration.
        """
        from .vision_motion import VisionMotionAnalyzer, VisionMotionError
        if not self.active:
            raise VisionCalibrationError('Start Vision Calibration before measuring motion')
        if self._gate.locked():
            raise VisionCalibrationError('Vision worker is busy')
        config = self._read_json(self.secret_path, {})
        if not config.get('source') or not config.get('host'):
            raise VisionCalibrationError('Configure the camera-only OBS screenshot source first')
        if not .01 <= float(interval_seconds) <= 10:
            raise VisionCalibrationError('Invalid movement capture interval')
        async with self._gate:
            self.processing = True
            try:
                before_image = await self.obs_client.screenshot(
                    config['host'], int(config.get('port', 4455)),
                    config.get('password', ''), config['source'])
                before_context = deepcopy(context_provider())
                await asyncio.sleep(float(interval_seconds))
                after_image = await self.obs_client.screenshot(
                    config['host'], int(config.get('port', 4455)),
                    config.get('password', ''), config['source'])
                after_context = deepcopy(context_provider())
                if not self.active:
                    raise VisionCalibrationError('Motion test was stopped during capture')
                try:
                    result = await asyncio.to_thread(
                        VisionMotionAnalyzer().analyse, before_image, after_image,
                        before_context, after_context)
                except VisionMotionError as exc:
                    raise VisionCalibrationError(str(exc)) from exc
                result.update({'capture_interval_seconds': float(interval_seconds),
                               'captured_mono_start': before_context.get('captured_mono'),
                               'captured_mono_end': after_context.get('captured_mono'),
                               'source': 'obs_camera_only', 'id': str(uuid.uuid4())})
                # Only compact interpretation is retained; screenshot bytes and
                # OBS password are not included in results or persisted to disk.
                self.completed.appendleft({'id': result['id'], 'source': 'obs_motion',
                                           'review_only': True,
                                           'dominant_axis': result['dominant_axis'],
                                           'count_delta': result['count_delta'],
                                           'motion_px': result['motion_px']})
                return result
            finally:
                self.processing = False

    async def capture_obs_pair(self, context_provider) -> dict[str, Any]:
        if not self.active:
            raise VisionCalibrationError("Start Vision Calibration before capturing")
        config = self._read_json(self.secret_path, {})
        if not config:
            raise VisionCalibrationError("Configure OBS WebSocket and its camera-only source first")
        before = deepcopy(context_provider())
        first = await self.obs_client.screenshot(config["host"], int(config.get("port", 4455)), config.get("password", ""), config["source"])
        await asyncio.sleep(0.25)
        second = await self.obs_client.screenshot(config["host"], int(config.get("port", 4455)), config.get("password", ""), config["source"])
        after = deepcopy(context_provider())
        stability = await asyncio.to_thread(self.detector.stability, first, second)
        before_axes, after_axes = before.get("axes") or {}, after.get("axes") or {}
        encoder_stable = True
        for axis, tolerance in (("pan", 3), ("tilt", 3), ("zoom", 8)):
            field = "relative_count" if axis == "zoom" else "raw_count"
            old, new = (before_axes.get(axis) or {}).get(field), (after_axes.get(axis) or {}).get(field)
            if old is not None and new is not None and abs(int(new) - int(old)) > tolerance:
                encoder_stable = False
        stability = {**stability, "encoder_stable": encoder_stable,
                     "stable": bool(stability.get("stable") and encoder_stable)}
        if not encoder_stable:
            stability["reason"] = "encoder counts changed during image capture"
        return await self.analyse(second, after, stability=stability, source="obs")

    @staticmethod
    def _validate_image(image: bytes) -> None:
        if not image or len(image) > MAX_IMAGE_BYTES:
            raise VisionCalibrationError("Calibration image is empty or exceeds 12 MiB")

