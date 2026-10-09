from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any


LINEAR_FIELDS = ("tracking_pan", "tracking_tilt", "tilt", "x", "y", "z", "roll", "fov")


@dataclass(slots=True)
class PoseSample:
    timestamp: float
    tracking_pan: float
    tracking_tilt: float
    pan: float
    tilt: float
    x: float
    y: float
    z: float
    roll: float
    fov: float
    valid: bool
    world_valid: bool
    profile_id: str
    calibration_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def wrap_angle(value: float) -> float:
    wrapped = (value + 180.0) % 360.0 - 180.0
    return 180.0 if wrapped == -180.0 and value > 0 else wrapped


def interpolate_angle(start: float, end: float, fraction: float) -> float:
    delta = (end - start + 180.0) % 360.0 - 180.0
    return wrap_angle(start + delta * fraction)


class PoseHistory:
    """Memory-bounded monotonic pose history with timestamp interpolation."""

    def __init__(self, duration_seconds: float = 5.0) -> None:
        if not math.isfinite(duration_seconds) or duration_seconds < 2.1:
            raise ValueError("History duration must be at least 2.1 seconds")
        self.duration_seconds = float(duration_seconds)
        self.samples: deque[PoseSample] = deque()
        self.generation = 0
        self.last_reset_reason = "startup"

    def reset(self, reason: str) -> None:
        self.samples.clear()
        self.generation += 1
        self.last_reset_reason = reason

    def append(self, sample: PoseSample) -> None:
        if self.samples and sample.timestamp < self.samples[-1].timestamp:
            raise ValueError("Pose history timestamps must be monotonic")
        if self.samples and sample.timestamp == self.samples[-1].timestamp:
            self.samples[-1] = sample
        else:
            self.samples.append(sample)
        cutoff = sample.timestamp - self.duration_seconds
        while len(self.samples) > 1 and self.samples[1].timestamp < cutoff:
            self.samples.popleft()

    def query(self, requested_timestamp: float) -> dict[str, Any]:
        if not self.samples:
            return {"status": "BUFFERING", "pose": None, "before": None, "after": None, "fraction": None}
        oldest, newest = self.samples[0], self.samples[-1]
        if requested_timestamp < oldest.timestamp:
            return {"status": "HISTORY_UNDERRUN", "pose": None, "before": oldest.timestamp, "after": oldest.timestamp, "fraction": None}
        if requested_timestamp >= newest.timestamp:
            pose = newest.to_dict()
            return {"status": "OK" if newest.valid else "INVALID", "pose": pose, "before": newest.timestamp, "after": newest.timestamp, "fraction": 1.0, "mode": "LATEST_STATIC"}
        before = oldest
        for after in list(self.samples)[1:]:
            if requested_timestamp == after.timestamp:
                pose = after.to_dict()
                return {"status": "OK" if after.valid else "INVALID", "pose": pose, "before": after.timestamp, "after": after.timestamp, "fraction": 0.0, "mode": "EXACT"}
            if requested_timestamp < after.timestamp:
                if not before.valid or not after.valid:
                    return {"status": "INVALID", "pose": None, "before": before.timestamp, "after": after.timestamp, "fraction": None}
                if before.profile_id != after.profile_id or before.calibration_id != after.calibration_id:
                    return {"status": "INVALID", "pose": None, "before": before.timestamp, "after": after.timestamp, "fraction": None}
                span = after.timestamp - before.timestamp
                fraction = 0.0 if span <= 0 else (requested_timestamp - before.timestamp) / span
                values = before.to_dict()
                for field in LINEAR_FIELDS:
                    values[field] = getattr(before, field) + (getattr(after, field) - getattr(before, field)) * fraction
                values["pan"] = interpolate_angle(before.pan, after.pan, fraction)
                values["timestamp"] = requested_timestamp
                values["valid"] = True
                values["world_valid"] = before.world_valid and after.world_valid
                return {"status": "OK", "pose": values, "before": before.timestamp, "after": after.timestamp, "fraction": fraction, "mode": "INTERPOLATED"}
            before = after
        raise RuntimeError("Pose history interpolation failed")

    def stats(self, now: float) -> dict[str, Any]:
        oldest = self.samples[0].timestamp if self.samples else None
        newest = self.samples[-1].timestamp if self.samples else None
        span = newest - oldest if oldest is not None and newest is not None else 0.0
        return {
            "duration_seconds": self.duration_seconds,
            "sample_count": len(self.samples),
            "oldest_age_ms": round((now - oldest) * 1000.0, 3) if oldest is not None else None,
            "newest_age_ms": round((now - newest) * 1000.0, 3) if newest is not None else None,
            "sample_rate_hz": round((len(self.samples) - 1) / span, 2) if len(self.samples) > 1 and span > 0 else None,
            "generation": self.generation,
            "last_reset_reason": self.last_reset_reason,
        }
