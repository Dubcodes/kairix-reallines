from __future__ import annotations

import csv
import io
import json
import math
import threading
import time
from collections import deque
from copy import deepcopy
from typing import Any


AXES = ("pan", "tilt", "zoom")
MODES = {"live", "fixed_pose"}


class TrackingStabilityRecorder:
    """Bounded, opt-in correlation recorder for tracking stability diagnosis.

    Normal operation performs only a cheap inactive check. Active recordings are
    retained in RAM and are serialized only when the operator requests an export.
    """

    def __init__(self, *, capacity: int = 60_000, clock=time.monotonic) -> None:
        self.capacity = max(1_000, int(capacity))
        self.clock = clock
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=self.capacity)
        self._active = False
        self._started_mono: float | None = None
        self._ends_mono: float | None = None
        self._stopped_mono: float | None = None
        self._duration_seconds = 10.0
        self._mode = "live"
        self._stop_reason = "not_started"
        self._fixed_pose: dict[str, Any] | None = None
        self._previous_raw: dict[str, int] = {}
        self._raw_session: int | None = None
        self._publication_sequence = 0
        self._dropped_by_capacity = 0

    def start(self, duration_seconds: float, *, mode: str = "live",
              fixed_pose: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            duration = float(duration_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("Recording duration must be numeric") from exc
        if not math.isfinite(duration) or not 10.0 <= duration <= 60.0:
            raise ValueError("Recording duration must be between 10 and 60 seconds")
        if mode not in MODES:
            raise ValueError("Recording mode must be live or fixed_pose")
        now = self.clock()
        with self._lock:
            self._events.clear()
            self._active = True
            self._started_mono = now
            self._ends_mono = now + duration
            self._stopped_mono = None
            self._duration_seconds = duration
            self._mode = mode
            self._stop_reason = "recording"
            self._fixed_pose = self._normalise_fixed_pose(fixed_pose) if mode == "fixed_pose" else None
            self._previous_raw = {}
            self._raw_session = None
            self._publication_sequence = 0
            self._dropped_by_capacity = 0
        return self.status()

    @staticmethod
    def _normalise_fixed_pose(value: dict[str, Any] | None) -> dict[str, Any]:
        source = value or {}
        pose: dict[str, Any] = {}
        defaults = {"x": 0.0, "y": 0.0, "z": 1.7, "height": 1.7, "pan": 0.0,
                    "tilt": 0.0, "roll": 0.0, "fov": 60.0,
                    "tracking_pan": 0.0, "tracking_tilt": 0.0}
        for field, default in defaults.items():
            raw = source.get(field, source.get("height", default) if field == "z" else default)
            try:
                number = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Fixed pose {field} must be numeric") from exc
            if not math.isfinite(number):
                raise ValueError(f"Fixed pose {field} must be finite")
            pose[field] = number
        pose.update({"valid": True, "world_valid": True, "source": "diagnostic_fixed_pose",
                     "interpolation_status": "FIXED_DIAGNOSTIC"})
        return pose

    def stop(self, reason: str = "operator") -> dict[str, Any]:
        with self._lock:
            self._stop_locked(self.clock(), reason)
        return self.status()

    def _stop_locked(self, now: float, reason: str) -> None:
        if self._active:
            self._active = False
            self._stopped_mono = now
            self._stop_reason = str(reason)[:80]
            self._fixed_pose = None

    def _expire_locked(self, now: float) -> None:
        if self._active and self._ends_mono is not None and now >= self._ends_mono:
            self._stop_locked(now, "duration_complete")

    def _append_locked(self, event: dict[str, Any]) -> None:
        if len(self._events) == self.capacity:
            self._dropped_by_capacity += 1
        self._events.append(event)

    def record_raw_packet(self, packet: Any, received_mono: float) -> None:
        if not self._active:
            return
        with self._lock:
            self._expire_locked(received_mono)
            if not self._active:
                return
            session = int(packet.session_id)
            if session != self._raw_session:
                self._previous_raw = {}
                self._raw_session = session
            counts = {axis: int(getattr(packet, axis)) for axis in AXES}
            deltas = {axis: counts[axis] - self._previous_raw.get(axis, counts[axis]) for axis in AXES}
            self._previous_raw = counts
            self._append_locked({
                "stage": "pico_packet", "mono": float(received_mono),
                "pico_session": session, "packet_sequence": int(packet.sequence),
                "pico_timestamp_us": int(packet.pico_us), "counts": counts, "deltas": deltas,
            })

    def record_acquisition(self, timestamp: float, samples: dict[str, dict[str, Any]],
                           tracking: dict[str, Any], camera: dict[str, Any],
                           render_pose: dict[str, Any]) -> None:
        if not self._active:
            return
        with self._lock:
            self._expire_locked(timestamp)
            if not self._active:
                return
            axes: dict[str, Any] = {}
            for axis in AXES:
                sample = samples.get(axis, {})
                runtime = tracking.get("axes", {}).get(axis, {})
                axes[axis] = {
                    "raw_count": sample.get("raw_count", runtime.get("raw_count")),
                    "mapped_value": runtime.get("value"),
                    "reference_id": runtime.get("reference_id"),
                    "health": runtime.get("health"),
                    "valid": bool(runtime.get("valid")),
                    "pico_session": sample.get("pico_session"),
                    "packet_sequence": sample.get("packet_seq"),
                    "packet_age_ms": sample.get("packet_age_ms"),
                }
            self._append_locked({
                "stage": "acquisition", "mono": float(timestamp), "axes": axes,
                "tracking_valid": bool(tracking.get("valid")),
                "tracking_health": tracking.get("integrity"),
                "camera": self._pose_fields(camera), "render_pose": self._pose_fields(render_pose),
            })

    def record_publication(self, timestamp: float, pose: dict[str, Any],
                           transport: dict[str, Any]) -> None:
        if not self._active:
            return
        with self._lock:
            self._expire_locked(timestamp)
            if not self._active:
                return
            self._publication_sequence += 1
            self._append_locked({
                "stage": "publication", "mono": float(timestamp),
                "publication_sequence": self._publication_sequence,
                "server_timestamp": pose.get("server_mono"),
                "render_pose": self._pose_fields(pose.get("render_camera", {})),
                "tracking_valid": bool(pose.get("tracking", {}).get("valid")),
                "transport": {
                    "clients": transport.get("clients", 0),
                    "queued_messages": transport.get("queued_messages", 0),
                    "coalesced_poses": transport.get("coalesced_poses", 0),
                    "critical_overflows": transport.get("critical_overflows", 0),
                },
            })

    @staticmethod
    def _pose_fields(value: dict[str, Any]) -> dict[str, Any]:
        return {field: value.get(field) for field in
                ("x", "y", "z", "pan", "tilt", "roll", "fov", "valid",
                 "world_valid", "render_timestamp", "interpolation_status")}

    def override_render_pose(self, render: dict[str, Any], timestamp: float) -> dict[str, Any]:
        if not self._active or self._mode != "fixed_pose":
            return render
        with self._lock:
            self._expire_locked(timestamp)
            if not self._active or self._fixed_pose is None:
                return render
            fixed = deepcopy(self._fixed_pose)
        fixed["render_timestamp"] = float(timestamp)
        return fixed

    def status(self) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            self._expire_locked(now)
            events = list(self._events)
            active = self._active
            started = self._started_mono
            ends = self._ends_mono
            result = {
                "active": active, "mode": self._mode, "duration_seconds": self._duration_seconds,
                "elapsed_seconds": round(max(0.0, now - started), 3) if started is not None else 0.0,
                "remaining_seconds": round(max(0.0, ends - now), 3) if active and ends is not None else 0.0,
                "event_count": len(events), "capacity": self.capacity,
                "dropped_by_capacity": self._dropped_by_capacity, "stop_reason": self._stop_reason,
            }
        result["summary"] = self._summary(events)
        return result

    @staticmethod
    def _summary(events: list[dict[str, Any]]) -> dict[str, Any]:
        movement = {axis: 0 for axis in AXES}
        raw_values = {axis: [] for axis in AXES}
        mapped_values = {axis: [] for axis in ("pan", "tilt")}
        camera_values = {axis: [] for axis in ("pan", "tilt")}
        render_values = {axis: [] for axis in ("pan", "tilt")}
        counts = {"pico_packet": 0, "acquisition": 0, "publication": 0}
        for event in events:
            stage = event.get("stage")
            counts[stage] = counts.get(stage, 0) + 1
            if stage == "pico_packet":
                for axis in AXES:
                    raw_values[axis].append(event["counts"][axis])
                    movement[axis] += int(event["deltas"][axis] != 0)
            elif stage == "acquisition":
                for axis in ("pan", "tilt"):
                    value = event["axes"][axis].get("mapped_value")
                    if isinstance(value, (int, float)):
                        mapped_values[axis].append(float(value))
                    camera = event["camera"].get(axis)
                    if isinstance(camera, (int, float)):
                        camera_values[axis].append(float(camera))
            elif stage == "publication":
                for axis in ("pan", "tilt"):
                    value = event["render_pose"].get(axis)
                    if isinstance(value, (int, float)):
                        render_values[axis].append(float(value))
        span = lambda values: round(max(values) - min(values), 9) if values else None
        return {
            "events_by_stage": counts,
            "raw_movement_events": movement,
            "raw_count_excursion": {axis: span(values) for axis, values in raw_values.items()},
            "mapped_angle_excursion_deg": {axis: span(values) for axis, values in mapped_values.items()},
            "camera_angle_excursion_deg": {axis: span(values) for axis, values in camera_values.items()},
            "render_angle_excursion_deg": {axis: span(values) for axis, values in render_values.items()},
        }

    def export_jsonl(self) -> str:
        with self._lock:
            events = list(self._events)
        return "".join(json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n" for event in events)

    def export_csv(self) -> str:
        with self._lock:
            events = list(self._events)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(("stage", "mono", "pico_session", "packet_sequence", "pico_timestamp_us",
                         "pan_count", "tilt_count", "zoom_count", "pan_delta", "tilt_delta",
                         "zoom_delta", "publication_sequence", "payload_json"))
        for event in events:
            counts, deltas = event.get("counts", {}), event.get("deltas", {})
            writer.writerow((event.get("stage"), event.get("mono"), event.get("pico_session"),
                             event.get("packet_sequence"), event.get("pico_timestamp_us"),
                             counts.get("pan"), counts.get("tilt"), counts.get("zoom"),
                             deltas.get("pan"), deltas.get("tilt"), deltas.get("zoom"),
                             event.get("publication_sequence"),
                             json.dumps(event, separators=(",", ":"), allow_nan=False)))
        return output.getvalue()
