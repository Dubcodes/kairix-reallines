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
MODES = {"live", "fixed_pose", "fixed_fov"}


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
        self._pre_events: deque[dict[str, Any]] = deque(maxlen=1_000)
        self._active = False
        self._started_mono: float | None = None
        self._ends_mono: float | None = None
        self._stopped_mono: float | None = None
        self._duration_seconds = 10.0
        self._mode = "live"
        self._stop_reason = "not_started"
        self._fixed_pose: dict[str, Any] | None = None
        self._fixed_fov: float | None = None
        self._previous_raw: dict[str, int] = {}
        self._raw_session: int | None = None
        self._publication_sequence = 0
        self._dropped_by_capacity = 0
        self._automatic = False
        self._trigger_reason: str | None = None
        self._previous_probe: dict[str, Any] | None = None
        self._previous_publication_probe: dict[str, Any] | None = None

    def start(self, duration_seconds: float, *, mode: str = "live",
              fixed_pose: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            duration = float(duration_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("Recording duration must be numeric") from exc
        if not math.isfinite(duration) or not 10.0 <= duration <= 60.0:
            raise ValueError("Recording duration must be between 10 and 60 seconds")
        if mode not in MODES:
            raise ValueError("Recording mode must be live, fixed_pose or fixed_fov")
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
            self._fixed_fov = self._normalise_fixed_fov(fixed_pose) if mode == "fixed_fov" else None
            self._previous_raw = {}
            self._raw_session = None
            self._publication_sequence = 0
            self._dropped_by_capacity = 0
            self._automatic = False
            self._trigger_reason = None
            self._previous_publication_probe = None
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

    @staticmethod
    def _normalise_fixed_fov(value: dict[str, Any] | None) -> float:
        try:
            fov = float((value or {}).get("fov", 60.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("Fixed diagnostic FOV must be numeric") from exc
        if not math.isfinite(fov) or not 0.0 < fov < 180.0:
            raise ValueError("Fixed diagnostic FOV must be greater than 0 and less than 180 degrees")
        return fov

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
            self._fixed_fov = None

    def _expire_locked(self, now: float) -> None:
        if self._active and self._ends_mono is not None and now >= self._ends_mono:
            self._stop_locked(now, "duration_complete")

    def _append_locked(self, event: dict[str, Any]) -> None:
        if len(self._events) == self.capacity:
            self._dropped_by_capacity += 1
        self._events.append(event)

    def _trigger_locked(self, now: float, reason: str) -> None:
        if self._active:
            return
        self._events.clear()
        # Each pre-event is an immutable-by-convention, freshly built
        # snapshot. Neither producer nor consumer mutates it after append.
        # Copying the entire 2-second history recursively in the 200 Hz
        # acquisition task caused measurable event-loop stalls. A deque
        # transfer retains the same evidence without recursive copying.
        self._events.extend(self._pre_events)
        self._active = True
        self._automatic = True
        self._trigger_reason = str(reason)[:120]
        self._started_mono = now
        self._ends_mono = now + 5.0
        self._stopped_mono = None
        self._duration_seconds = 5.0
        self._mode = "live"
        self._stop_reason = "automatic_post_event"

    def observe_acquisition(self, timestamp: float, samples: dict[str, dict[str, Any]],
                            tracking: dict[str, Any], camera: dict[str, Any]) -> bool:
        """Cheap always-on probe; returns whether full correlation is required."""
        axes = tracking.get("axes", {})
        probe = {
            "stage": "pre_event", "mono": float(timestamp),
            "raw_counts": {axis: samples.get(axis, {}).get("input_raw_count", samples.get(axis, {}).get("raw_count")) for axis in AXES},
            "filtered_counts": {axis: samples.get(axis, {}).get("filtered_count", samples.get(axis, {}).get("raw_count")) for axis in AXES},
            "mapped": {axis: axes.get(axis, {}).get("value") for axis in ("pan", "tilt")},
            "references": {axis: axes.get(axis, {}).get("reference_id") for axis in AXES},
            "tracking_valid": bool(tracking.get("valid")),
            "camera": self._pose_fields(camera),
        }
        with self._lock:
            self._expire_locked(timestamp)
            while self._pre_events and timestamp - self._pre_events[0]["mono"] > 2.0:
                self._pre_events.popleft()
            self._pre_events.append(probe)
            previous = self._previous_probe
            self._previous_probe = probe
            if previous is not None and not self._active:
                reason = None
                if previous["references"] != probe["references"]:
                    reason = "reference_identity_changed"
                elif previous["tracking_valid"] != probe["tracking_valid"]:
                    reason = "tracking_validity_changed"
                else:
                    for axis in ("pan", "tilt"):
                        before, after = previous["mapped"].get(axis), probe["mapped"].get(axis)
                        if isinstance(before, (int, float)) and isinstance(after, (int, float)) and abs(after - before) >= 0.25:
                            counts_constant = (
                                previous["raw_counts"].get(axis) == probe["raw_counts"].get(axis)
                                and previous["filtered_counts"].get(axis) == probe["filtered_counts"].get(axis)
                            )
                            reason = (f"downstream_large_{axis}_pose_change_constant_counts"
                                      if counts_constant else f"large_{axis}_pose_change")
                            break
                if reason:
                    self._trigger_locked(timestamp, reason)
            return self._active

    def observe_scheduling(self, stage: str, gap_ms: float, timestamp: float) -> None:
        if gap_ms < 40.0:
            return
        with self._lock:
            if not self._active:
                self._trigger_locked(timestamp, f"{stage}_scheduling_gap_{gap_ms:.1f}ms")
            self._append_locked({"stage": "scheduling_gap", "mono": float(timestamp),
                                 "loop": stage, "gap_ms": round(float(gap_ms), 3)})

    def record_blocking_operation(self, name: str, elapsed_ms: float, timestamp: float) -> None:
        with self._lock:
            if elapsed_ms >= 40.0 and not self._active:
                self._trigger_locked(timestamp, f"blocking_{name}_{elapsed_ms:.1f}ms")
            if self._active:
                self._append_locked({"stage": "blocking_operation", "mono": float(timestamp),
                                     "operation": str(name), "elapsed_ms": round(float(elapsed_ms), 3)})

    def record_browser_metrics(self, report: dict[str, Any], timestamp: float) -> None:
        if not self._active:
            return
        metrics = report.get("metrics") or {}
        with self._lock:
            if self._active:
                self._append_locked({
                    "stage": "browser_metrics", "mono": float(timestamp),
                    "client_id": report.get("client_id"), "page": report.get("page"),
                    "label": report.get("label"),
                    "pose_identity": metrics.get("last_pose_identity"),
                    "rendered_pose_identity": metrics.get("last_rendered_pose_identity"),
                    "incoming_pose_hz_5s": metrics.get("incoming_pose_hz_5s"),
                    "animation_frame_hz_5s": metrics.get("animation_frame_hz_5s"),
                    "frame_interval_p95_ms_5s": metrics.get("frame_interval_p95_ms_5s"),
                    "missed_frame_deadlines_5s": metrics.get("missed_frame_deadlines_5s"),
                    "transport_mode": metrics.get("transport_mode"),
                })

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
                    "raw_count": sample.get("input_raw_count", sample.get("raw_count", runtime.get("raw_count"))),
                    "filtered_count": sample.get("filtered_count", sample.get("raw_count", runtime.get("raw_count"))),
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
                           transport: dict[str, Any], context: dict[str, Any] | None = None) -> None:
        render_pose = self._pose_fields(pose.get("render_camera", {}))
        # Normal 50 Hz publishing should not recursively copy calibration
        # context. Take a lightweight immutable fingerprint for change detection;
        # capture the full context only while a recording is active.
        diagnostic = context or {}
        probe = {
            "render_pose": render_pose,
            "overlay_revision": diagnostic.get("overlay_revision"),
            "reference_observation_id": diagnostic.get("reference_observation_id"),
            "reference_points": repr(diagnostic.get("reference_points")),
            "world_calibration_id": diagnostic.get("world_calibration_id"),
            "world_valid": diagnostic.get("world_valid"),
        }
        with self._lock:
            self._expire_locked(timestamp)
            previous = self._previous_publication_probe
            self._previous_publication_probe = probe
            if previous is not None and not self._active:
                pose_constant = previous["render_pose"] == probe["render_pose"]
                overlay_changed = (
                    previous["overlay_revision"] != probe["overlay_revision"]
                    or previous["reference_observation_id"] != probe["reference_observation_id"]
                    or previous["reference_points"] != probe["reference_points"]
                )
                world_changed = (
                    previous["world_calibration_id"] != probe["world_calibration_id"]
                    or previous["world_valid"] != probe["world_valid"]
                )
                if overlay_changed:
                    suffix = "constant_pose" if pose_constant else "with_pose_change"
                    self._trigger_locked(timestamp, f"calibration_overlay_state_changed_{suffix}")
                elif world_changed:
                    self._trigger_locked(timestamp, "graphics_world_state_changed")
                elif (previous["render_pose"].get("fov") != render_pose.get("fov")
                      and previous["render_pose"].get("pan") == render_pose.get("pan")
                      and previous["render_pose"].get("tilt") == render_pose.get("tilt")):
                    self._trigger_locked(timestamp, "render_fov_changed_constant_pan_tilt")
            if not self._active:
                return
            # Copy only for a live/automatic recording; never on ordinary frames.
            # A captured event must remain stable if the operator edits calibration.
            diagnostic = deepcopy(diagnostic)
            self._publication_sequence += 1
            self._append_locked({
                "stage": "publication", "mono": float(timestamp),
                "publication_sequence": self._publication_sequence,
                "server_timestamp": pose.get("server_mono"),
                "render_pose": render_pose,
                "tracking_valid": bool(pose.get("tracking", {}).get("valid")),
                "diagnostic_context": diagnostic,
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
        if not self._active or self._mode not in {"fixed_pose", "fixed_fov"}:
            return render
        with self._lock:
            self._expire_locked(timestamp)
            if not self._active:
                return render
            if self._mode == "fixed_fov":
                if self._fixed_fov is None:
                    return render
                fixed = deepcopy(render)
                fixed["fov"] = self._fixed_fov
                fixed["diagnostic_fixed_fov"] = True
                return fixed
            if self._fixed_pose is None:
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
                "automatic": self._automatic, "trigger_reason": self._trigger_reason,
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
