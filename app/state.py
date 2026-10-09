from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from .debug import DebugRecorder
from .history import PoseHistory, PoseSample
from .tracking import AXES, REQUIRED_AXES, SOURCE_TYPES, TrackingConfigError, default_profile, map_raw_value, merge_axis_update, normalise_axis_config
from .world_calibration import CalibrationError, TARGET_POINTS, default_world_calibration, solve_world_calibration, synthetic_observations, target_points, utc_now, validate_target

DEFAULT_CAMERA = {"source": "simulator", "x": 0.0, "y": 0.0, "z": 1.7, "pan": 0.0, "tilt": -8.0, "roll": 0.0, "fov": 60.0, "height": 1.7, "valid": True, "world_valid": False, "raw_pan": 0, "raw_tilt": 0, "updated_mono": 0.0}
DEFAULT_CALIBRATION = {"pan_direction": None, "tilt_direction": None, "marks": {}}
DEFAULT_ENGINEERING = {"profile_name": "Development Simulator", "debug_enabled": True, "input_sources": {"pan": "simulator", "tilt": "simulator", "zoom": "simulator", "focus": "disabled"}}
DEFAULT_SYNC = {"graphics_delay_ms": 0, "history_duration_seconds": 5.0}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())[:8]


def _demo_scene() -> dict[str, Any]:
    group_id = "home-run"
    items: list[dict[str, Any]] = []
    members: list[str] = []
    for label, y in (("Finish", 0.0), ("50M", 50.0), ("100M", 100.0), ("200M", 200.0)):
        line_id, marker_id = f"demo-{label.lower()}-line", f"demo-{label.lower()}-marker"
        items.extend([
            {"id": line_id, "type": "line", "name": f"{label} Line", "visible": True, "color": "#ffffff", "width": 5, "x1": -5.0, "y1": y, "z1": 0.0, "x2": 5.0, "y2": y, "z2": 0.0},
            {"id": marker_id, "type": "marker", "name": f"{label} Marker", "visible": True, "color": "#ffffff", "size": 0.5, "label": label, "show_label": True, "x": 0.0, "y": y, "z": 0.05},
        ])
        members.extend([line_id, marker_id])
    return {"background": "#00ff00", "groups": [{"id": group_id, "name": "Home Run", "visible": True, "item_ids": members}], "items": items}


class StateStore:
    def __init__(self, data_dir: Path, debug: DebugRecorder) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        legacy_engineering_exists = (self.data_dir / "engineering.json").exists()
        self.layouts_dir = self.data_dir / "layouts"
        self.layouts_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir = self.data_dir / "profiles"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.world_calibrations_dir = self.data_dir / "world_calibrations"
        self.world_calibrations_dir.mkdir(parents=True, exist_ok=True)
        self.debug = debug
        self.camera = deepcopy(DEFAULT_CAMERA)
        self.calibration = self._load("calibration.json", DEFAULT_CALIBRATION)
        self.engineering = self._load("engineering.json", DEFAULT_ENGINEERING)
        self.profile_index = self._load_or_migrate_profiles(legacy_engineering_exists)
        self.profile = self._read_profile(self.profile_index["active_id"])
        self._sync_calibration_from_profile()
        self.world_calibration_index = self._load_or_create_world_calibrations()
        self.world_calibration = self._read_world_calibration(self.world_calibration_index["active_id"])
        self.sync_config = self._normalise_sync_config(self._load("sync.json", DEFAULT_SYNC))
        self.pose_history = PoseHistory(self.sync_config["history_duration_seconds"])
        self._pose_reference_key: tuple[Any, ...] | None = None
        self._last_history_debug_mono = 0.0
        self._last_render_debug_mono = 0.0
        self._last_sync_status: str | None = None
        self.tracking: dict[str, Any] = {"axes": {}, "valid": False, "required_axes": list(REQUIRED_AXES)}
        self._refresh_tracking_from_profile(initial=True)
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()
        self.layout_dirty = False
        self.layout_save_error: str | None = None
        self.layout_index = self._load_or_migrate_layouts()
        self.scene = self._read_layout(self.layout_index["active_id"])

    def _atomic_write_path(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(path)
        except Exception as exc:
            self.debug.error("persistence_error", path=str(path), error=repr(exc))
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    def _load(self, filename: str, default: dict[str, Any]) -> dict[str, Any]:
        path = self.data_dir / filename
        if not path.exists():
            self._atomic_write_path(path, default)
            return deepcopy(default)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.debug.error("state_load_failed", file=filename, error=repr(exc))
            return deepcopy(default)

    def _save(self, filename: str, data: dict[str, Any]) -> None:
        self._atomic_write_path(self.data_dir / filename, data)

    def _normalise_sync_config(self, value: dict[str, Any]) -> dict[str, Any]:
        try:
            delay = int(value.get("graphics_delay_ms", 0))
            duration = float(value.get("history_duration_seconds", 5.0))
        except (TypeError, ValueError):
            delay, duration = 0, 5.0
        result = {"graphics_delay_ms": max(0, min(2000, delay)), "history_duration_seconds": max(2.1, min(30.0, duration))}
        if result != value:
            self._save("sync.json", result)
        return result

    def _current_pose_reference(self) -> tuple[Any, ...]:
        solution = self.world_calibration.get("solution") or {}
        mappings = tuple((axis, self.profile["axes"][axis]["source"], self.profile["axes"][axis]["mapping"]["direction"], self.profile["axes"][axis]["mapping"]["offset"]) for axis in ("pan", "tilt"))
        return (
            self.profile_index["active_id"],
            self.world_calibration_index["active_id"],
            bool(self.world_calibration.get("valid")),
            solution.get("solved_at"),
            mappings,
            self.camera.get("fov"),
            self.camera.get("height"),
        )

    def _record_pose_sample(self, timestamp: float | None = None) -> None:
        now = time.perf_counter() if timestamp is None else float(timestamp)
        if self.pose_history.samples and now <= self.pose_history.samples[-1].timestamp:
            now = self.pose_history.samples[-1].timestamp + 1e-9
        pan = self.tracking["axes"].get("pan", {})
        tilt = self.tracking["axes"].get("tilt", {})
        sample = PoseSample(
            timestamp=now,
            tracking_pan=float(pan.get("value") or 0.0),
            tracking_tilt=float(tilt.get("value") or 0.0),
            pan=float(self.camera.get("pan", 0.0)),
            tilt=float(self.camera.get("tilt", 0.0)),
            x=float(self.camera.get("x", 0.0)),
            y=float(self.camera.get("y", 0.0)),
            z=float(self.camera.get("z", self.camera.get("height", 1.7))),
            roll=float(self.camera.get("roll", 0.0)),
            fov=float(self.camera.get("fov", 60.0)),
            valid=bool(self.camera.get("valid")),
            world_valid=bool(self.camera.get("world_valid")),
            profile_id=self.profile_index["active_id"],
            calibration_id=self.world_calibration_index["active_id"],
        )
        self.pose_history.append(sample)
        if now - self._last_history_debug_mono >= 1.0:
            self._last_history_debug_mono = now
            self.debug.tracking("tracking_history_sampled", samples=len(self.pose_history.samples), timestamp=now, pan=sample.pan, tilt=sample.tilt, valid=sample.valid)

    def _refresh_pose_reference_and_sample(self) -> None:
        reference = self._current_pose_reference()
        if self._pose_reference_key is not None and reference != self._pose_reference_key:
            self.pose_history.reset("tracking reference changed")
            self.debug.tracking("tracking_discontinuity", previous=repr(self._pose_reference_key), current=repr(reference))
            self.debug.tracking("tracking_history_reset", reason="tracking reference changed", generation=self.pose_history.generation)
        self._pose_reference_key = reference
        self._record_pose_sample()

    def _render_pose_state(self, now: float) -> tuple[dict[str, Any], dict[str, Any]]:
        requested = now - self.sync_config["graphics_delay_ms"] / 1000.0
        result = self.pose_history.query(requested)
        pose = result.get("pose")
        if pose is None:
            render = deepcopy(self.camera)
            render.update({"valid": False, "world_valid": False, "render_timestamp": requested, "interpolation_status": result["status"]})
        else:
            render = {
                "source": self.camera.get("source"),
                "x": pose["x"], "y": pose["y"], "z": pose["z"], "height": pose["z"],
                "pan": pose["pan"], "tilt": pose["tilt"], "roll": pose["roll"], "fov": pose["fov"],
                "tracking_pan": pose["tracking_pan"], "tracking_tilt": pose["tracking_tilt"],
                "valid": result["status"] == "OK" and pose["valid"],
                "world_valid": result["status"] == "OK" and pose["world_valid"],
                "profile_id": pose["profile_id"], "world_calibration_id": pose["calibration_id"],
                "render_timestamp": pose["timestamp"], "interpolation_status": result["status"],
            }
        stats = self.pose_history.stats(now)
        newest = self.pose_history.samples[-1].timestamp if self.pose_history.samples else None
        effective = (newest - pose["timestamp"]) * 1000.0 if newest is not None and pose is not None else None
        sync = {
            **deepcopy(self.sync_config), **stats,
            "requested_render_age_ms": self.sync_config["graphics_delay_ms"],
            "interpolation": result["status"],
            "interpolation_mode": result.get("mode"),
            "effective_difference_ms": round(max(0.0, effective), 3) if effective is not None else None,
            "before_timestamp": result.get("before"), "after_timestamp": result.get("after"),
        }
        return render, sync

    def _profile_path(self, profile_id: str) -> Path:
        return self.profiles_dir / f"{profile_id}.json"

    def _profile_meta(self, profile_id: str | None = None) -> dict[str, Any] | None:
        wanted = profile_id or self.profile_index["active_id"]
        return next((entry for entry in self.profile_index["profiles"] if entry["id"] == wanted), None)

    def _normalise_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(profile)
        result.setdefault("name", "Setup Profile")
        result.setdefault("camera", {})
        result["camera"].setdefault("fov", 60.0)
        result["camera"].setdefault("height", 1.7)
        result.setdefault("calibration", {}).setdefault("marks", {})
        axes = result.setdefault("axes", {})
        for axis in AXES:
            try:
                axes[axis] = normalise_axis_config(axis, axes.get(axis))
            except TrackingConfigError as exc:
                self.debug.error("tracking_config_invalid", axis=axis, error=str(exc))
                axes[axis] = normalise_axis_config(axis, None)
        return result

    def _read_profile(self, profile_id: str) -> dict[str, Any]:
        return self._normalise_profile(json.loads(self._profile_path(profile_id).read_text(encoding="utf-8")))

    def _write_profile_index(self) -> None:
        self._atomic_write_path(self.profiles_dir / "index.json", self.profile_index)

    def _write_active_profile_locked(self) -> None:
        profile_id = self.profile_index["active_id"]
        meta = self._profile_meta(profile_id)
        now = _utc_now()
        self.profile["id"] = profile_id
        self.profile["modified"] = now
        if meta:
            meta["name"] = self.profile["name"]
            meta["modified"] = now
        self._atomic_write_path(self._profile_path(profile_id), self.profile)
        self._write_profile_index()

    def _load_or_migrate_profiles(self, legacy_engineering_exists: bool) -> dict[str, Any]:
        index_path = self.profiles_dir / "index.json"
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
                if index.get("profiles") and any(x["id"] == index.get("active_id") for x in index["profiles"]):
                    return index
            except Exception as exc:
                self.debug.error("state_load_failed", file="profiles/index.json", error=repr(exc))
        profile_id, now = _new_id(), _utc_now()
        profile = default_profile(self.engineering.get("profile_name") or "Development Simulator")
        for axis, source in (self.engineering.get("input_sources") or {}).items():
            if axis in AXES and source in SOURCE_TYPES:
                profile["axes"][axis]["source"] = source
        for axis in ("pan", "tilt"):
            direction = self.calibration.get(f"{axis}_direction")
            if direction in {-1, 1}:
                profile["axes"][axis]["mapping"]["direction"] = direction
                profile["axes"][axis]["mapping"]["direction_learned"] = True
        profile["calibration"]["marks"] = deepcopy(self.calibration.get("marks") or {})
        profile.update({"id": profile_id, "created": now, "modified": now})
        meta = {key: profile[key] for key in ("id", "name", "created", "modified")}
        self._atomic_write_path(self._profile_path(profile_id), profile)
        index = {"active_id": profile_id, "profiles": [meta]}
        self._atomic_write_path(index_path, index)
        self.debug.app("profile_migrated" if legacy_engineering_exists else "profile_created", profile_id=profile_id, name=profile["name"], legacy_preserved=legacy_engineering_exists)
        return index

    def _sync_engineering_compat_locked(self) -> None:
        self.engineering["profile_name"] = self.profile["name"]
        self.engineering["input_sources"] = {axis: self.profile["axes"][axis]["source"] for axis in AXES}
        self._save("engineering.json", self.engineering)

    def _sync_calibration_from_profile(self) -> None:
        self.calibration = {
            "pan_direction": self.profile["axes"]["pan"]["mapping"]["direction"] if self.profile["axes"]["pan"]["mapping"].get("direction_learned") else None,
            "tilt_direction": self.profile["axes"]["tilt"]["mapping"]["direction"] if self.profile["axes"]["tilt"]["mapping"].get("direction_learned") else None,
            "marks": deepcopy(self.profile.get("calibration", {}).get("marks") or {}),
        }

    def _world_calibration_path(self, calibration_id: str) -> Path:
        return self.world_calibrations_dir / f"{calibration_id}.json"

    def _world_calibration_meta(self, calibration_id: str | None = None) -> dict[str, Any] | None:
        wanted = calibration_id or self.world_calibration_index["active_id"]
        return next((entry for entry in self.world_calibration_index["calibrations"] if entry["id"] == wanted), None)

    def _normalise_world_calibration(self, calibration: dict[str, Any]) -> dict[str, Any]:
        profile_id = calibration.get("setup_profile_id") or self.profile_index["active_id"]
        result = default_world_calibration(calibration.get("name") or "World Calibration", profile_id)
        result.update(deepcopy(calibration))
        result["target"] = {**default_world_calibration("", profile_id)["target"], **deepcopy(calibration.get("target") or {})}
        result["target"]["center"] = {**{"x": 0.0, "y": 20.0, "z": 1.0}, **deepcopy((calibration.get("target") or {}).get("center") or {})}
        result["camera_hint"] = {**{"x": 0.0, "y": 0.0, "z": 1.8}, **deepcopy(calibration.get("camera_hint") or {})}
        result["orientation_hint"] = {**{"pan_offset": 0.0, "tilt_offset": 0.0}, **deepcopy(calibration.get("orientation_hint") or {})}
        result["observations"] = deepcopy(calibration.get("observations") or {})
        if result.get("current_target") not in TARGET_POINTS:
            result["current_target"] = TARGET_POINTS[0]
        return result

    def _read_world_calibration(self, calibration_id: str) -> dict[str, Any]:
        return self._normalise_world_calibration(json.loads(self._world_calibration_path(calibration_id).read_text(encoding="utf-8")))

    def _load_or_create_world_calibrations(self) -> dict[str, Any]:
        index_path = self.world_calibrations_dir / "index.json"
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
                if index.get("calibrations") and any(item["id"] == index.get("active_id") for item in index["calibrations"]):
                    return index
            except Exception as exc:
                self.debug.error("state_load_failed", file="world_calibrations/index.json", error=repr(exc))
        calibration_id = _new_id()
        calibration = default_world_calibration("Default World Calibration", self.profile_index["active_id"])
        calibration["id"] = calibration_id
        meta = {key: calibration[key] for key in ("id", "name", "setup_profile_id", "created", "modified")}
        self._atomic_write_path(self._world_calibration_path(calibration_id), calibration)
        index = {"active_id": calibration_id, "calibrations": [meta]}
        self._atomic_write_path(index_path, index)
        self.debug.app("world_calibration_created", calibration_id=calibration_id, profile_id=calibration["setup_profile_id"])
        return index

    def _write_world_calibration_index(self) -> None:
        self._atomic_write_path(self.world_calibrations_dir / "index.json", self.world_calibration_index)

    def _write_active_world_calibration_locked(self) -> None:
        calibration_id = self.world_calibration_index["active_id"]
        now = utc_now()
        self.world_calibration.update({"id": calibration_id, "modified": now})
        meta = self._world_calibration_meta(calibration_id)
        if meta:
            meta.update({"name": self.world_calibration["name"], "setup_profile_id": self.world_calibration["setup_profile_id"], "modified": now})
        self._atomic_write_path(self._world_calibration_path(calibration_id), self.world_calibration)
        self._write_world_calibration_index()

    def _world_calibration_matches_profile(self) -> bool:
        return self.world_calibration.get("setup_profile_id") == self.profile_index["active_id"]

    def _apply_axis_runtime(self, axis: str, raw: float | None = None, initial: bool = False) -> None:
        config = self.profile["axes"][axis]
        source = config["source"]
        previous = self.tracking["axes"].get(axis, {})
        now = time.monotonic()
        if raw is None:
            raw = previous.get("raw")
        if raw is None and source == "simulator":
            raw = self.camera.get(axis, 0.0) if axis in {"pan", "tilt"} else 0.0
        runtime = {
            "source": source,
            "enabled": source != "disabled",
            "raw": raw,
            "value": None,
            "unit": "deg" if axis in {"pan", "tilt"} else "normalized",
            "valid": False,
            "health": "DISABLED" if source == "disabled" else "NOT_CONFIGURED",
            "status": "Disabled" if source == "disabled" else f"{source.replace('_', ' ').title()} is not implemented",
            "updated_mono": previous.get("updated_mono", 0.0),
            "update_hz": previous.get("update_hz"),
            "expects_periodic_samples": source not in {"simulator", "disabled"},
        }
        if source == "simulator":
            try:
                runtime["value"] = map_raw_value(axis, float(raw), config)
                runtime.update({"valid": True, "health": "VALID", "status": "Simulator", "updated_mono": now})
                previous_time = previous.get("updated_mono", 0.0)
                if previous_time and not initial and now > previous_time:
                    runtime["update_hz"] = round(1.0 / (now - previous_time), 2)
            except (TypeError, ValueError, TrackingConfigError) as exc:
                runtime.update({"health": "ERROR", "status": str(exc)})
                self.debug.error("tracking_mapping_error", axis=axis, error=str(exc))
        self.tracking["axes"][axis] = runtime

    def _refresh_camera_from_tracking(self) -> None:
        tracked: dict[str, float | None] = {}
        for axis in ("pan", "tilt"):
            runtime = self.tracking["axes"][axis]
            tracked[axis] = runtime["value"] if runtime["valid"] else None
            self.camera[f"raw_{axis}"] = runtime["raw"]
        sources = {self.tracking["axes"][axis]["source"] for axis in REQUIRED_AXES}
        self.tracking["valid"] = all(self.tracking["axes"][axis]["valid"] for axis in REQUIRED_AXES)
        profile_match = self._world_calibration_matches_profile()
        solution = self.world_calibration.get("solution")
        calibration_valid = bool(self.world_calibration.get("valid") and solution and solution.get("solved") and profile_match)
        if calibration_valid:
            solved_camera = solution["camera"]
            self.camera.update({"x": solved_camera["x"], "y": solved_camera["y"], "z": solved_camera["z"], "height": solved_camera["z"], "roll": solution.get("fixed_roll", 0.0), "fov": solution.get("horizontal_fov", self.camera["fov"])})
        else:
            height = float(self.profile.get("camera", {}).get("height", self.camera.get("height", 1.7)))
            self.camera.update({"x": 0.0, "y": 0.0, "z": height, "height": height, "roll": 0.0})
        for axis in ("pan", "tilt"):
            if tracked[axis] is not None:
                offset = float(solution.get(f"{axis}_offset", 0.0)) if calibration_valid else 0.0
                self.camera[f"tracking_{axis}"] = tracked[axis]
                self.camera[axis] = tracked[axis] + offset
        self.camera["source"] = next(iter(sources)) if len(sources) == 1 else "mixed"
        self.camera["valid"] = self.tracking["valid"]
        self.camera["world_valid"] = self.tracking["valid"] and calibration_valid
        self.camera["world_calibration_id"] = self.world_calibration_index["active_id"]
        self.camera["calibration_profile_match"] = profile_match
        self.camera["updated_mono"] = max((self.tracking["axes"][axis]["updated_mono"] for axis in REQUIRED_AXES), default=0.0)
        self._refresh_pose_reference_and_sample()

    def _refresh_tracking_from_profile(self, initial: bool = False) -> None:
        self.camera["fov"] = float(self.profile.get("camera", {}).get("fov", self.camera["fov"]))
        self.camera["height"] = float(self.profile.get("camera", {}).get("height", self.camera["height"]))
        for axis in AXES:
            self._apply_axis_runtime(axis, initial=initial)
        self._refresh_camera_from_tracking()

    def _layout_path(self, layout_id: str) -> Path:
        return self.layouts_dir / f"{layout_id}.json"

    def _normalise_scene(self, scene: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(scene)
        result.setdefault("background", "#00ff00")
        result.setdefault("groups", [])
        result.setdefault("items", [])
        valid_ids = {item.get("id") for item in result["items"]}
        assigned: set[str] = set()
        for group in result["groups"]:
            group.setdefault("id", _new_id())
            group.setdefault("name", "Group")
            group.setdefault("visible", True)
            members = []
            for item_id in group.get("item_ids", []):
                if item_id in valid_ids and item_id not in assigned:
                    members.append(item_id); assigned.add(item_id)
            group["item_ids"] = members
        return result

    def _read_layout(self, layout_id: str) -> dict[str, Any]:
        return self._normalise_scene(json.loads(self._layout_path(layout_id).read_text(encoding="utf-8")))

    def _load_or_migrate_layouts(self) -> dict[str, Any]:
        index_path = self.layouts_dir / "index.json"
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
                if index.get("layouts") and any(x["id"] == index.get("active_id") for x in index["layouts"]):
                    return index
            except Exception as exc:
                self.debug.error("state_load_failed", file="layouts/index.json", error=repr(exc))
        legacy_path = self.data_dir / "scene.json"
        migrated = legacy_path.exists()
        if migrated:
            try:
                scene = self._normalise_scene(json.loads(legacy_path.read_text(encoding="utf-8")))
            except Exception as exc:
                self.debug.error("state_load_failed", file="scene.json", error=repr(exc)); scene = _demo_scene(); migrated = False
        else:
            scene = _demo_scene()
        layout_id, now = _new_id(), _utc_now()
        metadata = {"id": layout_id, "name": "Default Layout", "created": now, "modified": now}
        self._atomic_write_path(self._layout_path(layout_id), scene)
        index = {"active_id": layout_id, "layouts": [metadata]}
        self._atomic_write_path(index_path, index)
        self.debug.app("layout_migration" if migrated else "layout_created", layout_id=layout_id, name=metadata["name"], legacy_preserved=migrated)
        return index

    def _layout_meta(self, layout_id: str | None = None) -> dict[str, Any] | None:
        wanted = layout_id or self.layout_index["active_id"]
        return next((entry for entry in self.layout_index["layouts"] if entry["id"] == wanted), None)

    def _write_index(self) -> None:
        self._atomic_write_path(self.layouts_dir / "index.json", self.layout_index)

    def _persist_active_locked(self) -> None:
        try:
            self._atomic_write_path(self._layout_path(self.layout_index["active_id"]), self.scene)
            meta = self._layout_meta()
            if meta:
                meta["modified"] = _utc_now()
            self._write_index()
            self.layout_dirty, self.layout_save_error = False, None
        except Exception as exc:
            self.layout_dirty, self.layout_save_error = True, str(exc)
            raise

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        history_now = time.perf_counter()
        tracking = deepcopy(self.tracking)
        for axis, runtime in tracking["axes"].items():
            runtime["age_seconds"] = round(max(0.0, now - runtime["updated_mono"]), 3) if runtime["updated_mono"] else None
            timeout = float(self.profile["axes"][axis].get("source_config", {}).get("stale_timeout_seconds", 0.0))
            if runtime["valid"] and runtime["expects_periodic_samples"] and timeout > 0 and runtime["age_seconds"] is not None and runtime["age_seconds"] > timeout:
                runtime.update({"valid": False, "health": "STALE", "status": f"No sample for {runtime['age_seconds']:.3f} seconds"})
        tracking["valid"] = all(tracking["axes"][axis]["valid"] for axis in REQUIRED_AXES)
        camera = deepcopy(self.camera)
        camera["valid"] = tracking["valid"]
        camera["world_valid"] = bool(camera.get("world_valid") and tracking["valid"])
        render_camera, sync = self._render_pose_state(history_now)
        if sync["interpolation"] != self._last_sync_status:
            event = "history_underrun" if sync["interpolation"] in {"HISTORY_UNDERRUN", "BUFFERING"} else "render_pose_invalid" if sync["interpolation"] == "INVALID" else "render_pose_interpolated"
            self.debug.tracking(event, status=sync["interpolation"], delay_ms=sync["graphics_delay_ms"], samples=sync["sample_count"])
            self._last_sync_status = sync["interpolation"]
        elif sync["interpolation"] == "OK" and history_now - self._last_render_debug_mono >= 1.0:
            self._last_render_debug_mono = history_now
            self.debug.tracking("render_pose_interpolated", mode=sync["interpolation_mode"], delay_ms=sync["graphics_delay_ms"], effective_ms=sync["effective_difference_ms"], pan=render_camera.get("pan"), tilt=render_camera.get("tilt"))
        world = deepcopy(self.world_calibration)
        world["target_points"] = target_points(world["target"])
        world_state = {"active_id": self.world_calibration_index["active_id"], "items": deepcopy(self.world_calibration_index["calibrations"]), "active": world, "profile_match": self._world_calibration_matches_profile()}
        return {"camera": camera, "live_camera": deepcopy(camera), "render_camera": render_camera, "sync": sync, "tracking": tracking, "profiles": {"active_id": self.profile_index["active_id"], "items": deepcopy(self.profile_index["profiles"]), "active": deepcopy(self.profile)}, "world_calibrations": world_state, "scene": deepcopy(self.scene), "calibration": deepcopy(self.calibration), "engineering": deepcopy(self.engineering), "layouts": {"active_id": self.layout_index["active_id"], "items": deepcopy(self.layout_index["layouts"]), "dirty": self.layout_dirty, "save_error": self.layout_save_error}, "clients": len(self.clients), "server_mono": now}

    async def broadcast(self, message: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for client in list(self.clients):
            try:
                await client.send_json(message)
            except Exception:
                dead.append(client)
        for client in dead:
            self.clients.discard(client)

    async def broadcast_state(self) -> None:
        await self.broadcast({"type": "state", "data": self.snapshot()})

    async def history_heartbeat(self) -> None:
        """Keep static poses timestamped and wake clients when buffering completes."""
        while True:
            await asyncio.sleep(0.05)
            previous_status = self._last_sync_status
            async with self.lock:
                self._record_pose_sample()
                current_status = self._render_pose_state(time.perf_counter())[1]["interpolation"]
            if previous_status is not None and current_status != previous_status:
                await self.broadcast_state()

    async def update_sync_config(self, values: dict[str, Any]) -> dict[str, Any]:
        candidate = {**self.sync_config, **values}
        try:
            delay = int(candidate["graphics_delay_ms"])
            duration = float(candidate["history_duration_seconds"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Delay and history duration must be numeric") from exc
        if not 0 <= delay <= 2000:
            raise ValueError("Graphics delay must be between 0 and 2000 ms")
        if not 2.1 <= duration <= 30.0:
            raise ValueError("History duration must be between 2.1 and 30 seconds")
        async with self.lock:
            duration_changed = duration != self.sync_config["history_duration_seconds"]
            before_delay = self.sync_config["graphics_delay_ms"]
            self.sync_config = {"graphics_delay_ms": delay, "history_duration_seconds": duration}
            self._save("sync.json", self.sync_config)
            if duration_changed:
                self.pose_history = PoseHistory(duration)
                self.pose_history.reset("history duration changed")
                self._record_pose_sample()
                self.debug.tracking("tracking_history_reset", reason="history duration changed", duration_seconds=duration)
            self._last_sync_status = None
        if delay != before_delay:
            self.debug.app("graphics_delay_changed", before_ms=before_delay, after_ms=delay)
        await self.broadcast_state(); return deepcopy(self.sync_config)

    async def set_camera(self, updates: dict[str, Any]) -> dict[str, Any]:
        simulator = {axis: updates[axis] for axis in ("pan", "tilt", "zoom", "focus") if axis in updates}
        for axis in ("pan", "tilt"):
            if f"raw_{axis}" in updates:
                simulator[axis] = updates[f"raw_{axis}"]
        camera_updates = {key: float(updates[key]) for key in ("fov", "height") if key in updates}
        changed_axes: list[tuple[str, float]] = []
        async with self.lock:
            self.camera.update(camera_updates)
            if camera_updates:
                self.profile.setdefault("camera", {}).update(camera_updates)
                self._write_active_profile_locked()
            for axis, value in simulator.items():
                if self.profile["axes"][axis]["source"] != "simulator":
                    continue
                previous_valid = self.tracking["axes"][axis]["valid"]
                self._apply_axis_runtime(axis, float(value))
                changed_axes.append((axis, float(value)))
                if previous_valid != self.tracking["axes"][axis]["valid"]:
                    self.debug.tracking("axis_validity_changed", axis=axis, valid=self.tracking["axes"][axis]["valid"], health=self.tracking["axes"][axis]["health"])
            self._refresh_camera_from_tracking()
            result = deepcopy(self.camera)
        for axis, raw in changed_axes:
            runtime = self.tracking["axes"][axis]
            self.debug.input("axis_raw_update", axis=axis, source="simulator", raw=raw)
            self.debug.tracking("axis_mapped_update", axis=axis, raw=raw, value=runtime["value"], unit=runtime["unit"])
        if camera_updates:
            self.debug.input("camera_settings", **camera_updates)
            self.debug.app("profile_updated", profile_id=self.profile_index["active_id"], camera=camera_updates)
        self.debug.tracking("camera_state", **self.camera)
        await self.broadcast_state(); return result

    async def create_profile(self, name: str) -> dict[str, Any]:
        profile_id, now = _new_id(), _utc_now()
        profile = default_profile(name.strip() or "New Setup Profile")
        profile.update({"id": profile_id, "created": now, "modified": now})
        meta = {key: profile[key] for key in ("id", "name", "created", "modified")}
        async with self.lock:
            self._atomic_write_path(self._profile_path(profile_id), profile)
            self.profile_index["profiles"].append(meta)
            self.profile_index["active_id"] = profile_id
            self.profile = profile
            self._write_profile_index()
            self._sync_calibration_from_profile()
            self._save("calibration.json", self.calibration)
            self._sync_engineering_compat_locked()
            self._refresh_tracking_from_profile()
        self.debug.app("profile_created", profile_id=profile_id, name=profile["name"])
        await self.broadcast_state(); return deepcopy(meta)

    async def select_profile(self, profile_id: str) -> dict[str, Any] | None:
        async with self.lock:
            meta = self._profile_meta(profile_id)
            if meta is None:
                return None
            self.profile = self._read_profile(profile_id)
            self.profile_index["active_id"] = profile_id
            self._write_profile_index()
            self._sync_calibration_from_profile()
            self._save("calibration.json", self.calibration)
            self._sync_engineering_compat_locked()
            self._refresh_tracking_from_profile()
        self.debug.app("profile_selected", profile_id=profile_id, name=meta["name"])
        if not self._world_calibration_matches_profile():
            self.debug.app("calibration_profile_mismatch", calibration_id=self.world_calibration_index["active_id"], calibration_profile_id=self.world_calibration.get("setup_profile_id"), active_profile_id=profile_id)
        await self.broadcast_state(); return deepcopy(meta)

    async def rename_profile(self, profile_id: str, name: str) -> dict[str, Any] | None:
        async with self.lock:
            meta = self._profile_meta(profile_id)
            if meta is None:
                return None
            profile = self.profile if profile_id == self.profile_index["active_id"] else self._read_profile(profile_id)
            before = profile["name"]
            profile["name"] = name.strip() or before
            profile["modified"] = _utc_now()
            meta["name"], meta["modified"] = profile["name"], profile["modified"]
            self._atomic_write_path(self._profile_path(profile_id), profile)
            self._write_profile_index()
            if profile_id == self.profile_index["active_id"]:
                self.profile = profile
                self._sync_engineering_compat_locked()
            saved = deepcopy(meta)
        self.debug.app("profile_renamed", profile_id=profile_id, before=before, after=saved["name"])
        await self.broadcast_state(); return saved

    async def duplicate_profile(self, profile_id: str, name: str | None = None) -> dict[str, Any] | None:
        async with self.lock:
            source_meta = self._profile_meta(profile_id)
            if source_meta is None:
                return None
            profile = self._read_profile(profile_id)
            new_id, now = _new_id(), _utc_now()
            profile.update({"id": new_id, "name": (name or f"{profile['name']} Copy").strip(), "created": now, "modified": now})
            meta = {key: profile[key] for key in ("id", "name", "created", "modified")}
            self._atomic_write_path(self._profile_path(new_id), profile)
            self.profile_index["profiles"].append(meta)
            self.profile_index["active_id"] = new_id
            self.profile = profile
            self._write_profile_index()
            self._sync_calibration_from_profile()
            self._save("calibration.json", self.calibration)
            self._sync_engineering_compat_locked()
            self._refresh_tracking_from_profile()
        self.debug.app("profile_duplicated", source_id=profile_id, profile_id=new_id, name=profile["name"])
        await self.broadcast_state(); return deepcopy(meta)

    async def delete_profile(self, profile_id: str) -> bool:
        async with self.lock:
            meta = self._profile_meta(profile_id)
            if meta is None or len(self.profile_index["profiles"]) <= 1:
                return False
            self.profile_index["profiles"] = [entry for entry in self.profile_index["profiles"] if entry["id"] != profile_id]
            if self.profile_index["active_id"] == profile_id:
                self.profile_index["active_id"] = self.profile_index["profiles"][0]["id"]
                self.profile = self._read_profile(self.profile_index["active_id"])
                self._sync_calibration_from_profile()
                self._save("calibration.json", self.calibration)
                self._refresh_tracking_from_profile()
                self._sync_engineering_compat_locked()
            self._write_profile_index()
            try:
                self._profile_path(profile_id).unlink()
            except FileNotFoundError:
                pass
        self.debug.app("profile_deleted", profile_id=profile_id, name=meta["name"], active_id=self.profile_index["active_id"])
        await self.broadcast_state(); return True

    async def update_axis_config(self, profile_id: str, axis: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        if axis not in AXES:
            raise TrackingConfigError(f"Unknown tracking axis: {axis}")
        async with self.lock:
            if profile_id != self.profile_index["active_id"]:
                return None
            previous = self.profile["axes"][axis]
            candidate = merge_axis_update(axis, previous, updates)
            source_changed = previous["source"] != candidate["source"]
            self.profile["axes"][axis] = candidate
            self._write_active_profile_locked()
            if axis in {"pan", "tilt"}:
                self._sync_calibration_from_profile()
                self._save("calibration.json", self.calibration)
            self._sync_engineering_compat_locked()
            self._apply_axis_runtime(axis)
            self._refresh_camera_from_tracking()
            saved = deepcopy(candidate)
        self.debug.app("tracking_source_selected" if source_changed else "profile_updated", profile_id=profile_id, axis=axis, source=candidate["source"])
        self.debug.tracking("tracking_source_status", axis=axis, source=candidate["source"], health=self.tracking["axes"][axis]["health"], status=self.tracking["axes"][axis]["status"])
        await self.broadcast_state(); return saved

    async def create_world_calibration(self, name: str) -> dict[str, Any]:
        calibration_id = _new_id()
        calibration = default_world_calibration(name.strip() or "New World Calibration", self.profile_index["active_id"])
        calibration["id"] = calibration_id
        meta = {key: calibration[key] for key in ("id", "name", "setup_profile_id", "created", "modified")}
        async with self.lock:
            self._atomic_write_path(self._world_calibration_path(calibration_id), calibration)
            self.world_calibration_index["calibrations"].append(meta)
            self.world_calibration_index["active_id"] = calibration_id
            self.world_calibration = calibration
            self._write_world_calibration_index()
            self._refresh_camera_from_tracking()
        self.debug.app("world_calibration_created", calibration_id=calibration_id, profile_id=calibration["setup_profile_id"], name=calibration["name"])
        await self.broadcast_state(); return deepcopy(meta)

    async def select_world_calibration(self, calibration_id: str) -> dict[str, Any] | None:
        async with self.lock:
            meta = self._world_calibration_meta(calibration_id)
            if meta is None:
                return None
            self.world_calibration = self._read_world_calibration(calibration_id)
            self.world_calibration_index["active_id"] = calibration_id
            self._write_world_calibration_index()
            self._refresh_camera_from_tracking()
            profile_match = self._world_calibration_matches_profile()
        self.debug.app("world_calibration_selected", calibration_id=calibration_id, profile_id=meta["setup_profile_id"], profile_match=profile_match)
        if not profile_match:
            self.debug.app("calibration_profile_mismatch", calibration_id=calibration_id, calibration_profile_id=meta["setup_profile_id"], active_profile_id=self.profile_index["active_id"])
        await self.broadcast_state(); return deepcopy(meta)

    async def update_world_calibration(self, values: dict[str, Any]) -> dict[str, Any]:
        candidate = deepcopy(self.world_calibration)
        for key in ("name", "horizontal_fov", "fixed_roll"):
            if key in values:
                candidate[key] = values[key]
        for section in ("camera_hint", "orientation_hint"):
            if section in values:
                candidate.setdefault(section, {}).update(values[section] or {})
        if "target" in values:
            target_update = values["target"] or {}
            candidate.setdefault("target", {}).update({key: value for key, value in target_update.items() if key != "center"})
            if "center" in target_update:
                candidate["target"].setdefault("center", {}).update(target_update["center"] or {})
        validate_target(candidate["target"])
        for section, keys in (("camera_hint", ("x", "y", "z")), ("orientation_hint", ("pan_offset", "tilt_offset"))):
            for key in keys:
                number = float(candidate[section][key])
                if not math.isfinite(number):
                    raise CalibrationError(f"{section}.{key} must be finite")
                candidate[section][key] = number
        candidate["horizontal_fov"] = float(candidate["horizontal_fov"])
        candidate["fixed_roll"] = float(candidate["fixed_roll"])
        if not 5.0 <= candidate["horizontal_fov"] <= 160.0:
            raise CalibrationError("Horizontal FOV must be between 5 and 160 degrees")
        if not math.isfinite(candidate["fixed_roll"]):
            raise CalibrationError("Fixed roll must be finite")
        candidate["name"] = str(candidate["name"]).strip() or self.world_calibration["name"]
        geometry_changed = "target" in values
        if geometry_changed:
            candidate.update({"valid": False, "status": "TARGET CHANGED — SOLVE REQUIRED"})
        async with self.lock:
            self.world_calibration = candidate
            self._write_active_world_calibration_locked()
            self._refresh_camera_from_tracking()
        self.debug.app("calibration_target_updated", calibration_id=candidate["id"], target=candidate["target"], camera_hint=candidate["camera_hint"])
        await self.broadcast_state(); return deepcopy(candidate)

    async def select_world_target(self, target: str) -> str:
        if target not in TARGET_POINTS:
            raise CalibrationError("Unknown calibration target point")
        async with self.lock:
            self.world_calibration["current_target"] = target
            self._write_active_world_calibration_locked()
        await self.broadcast_state(); return target

    async def move_world_target(self, step: int) -> str:
        current = self.world_calibration.get("current_target", TARGET_POINTS[0])
        target = TARGET_POINTS[(TARGET_POINTS.index(current) + step) % len(TARGET_POINTS)]
        return await self.select_world_target(target)

    async def mark_world_target(self, target: str | None = None) -> dict[str, Any]:
        selected = target or self.world_calibration.get("current_target", TARGET_POINTS[0])
        if selected not in TARGET_POINTS:
            raise CalibrationError("Unknown calibration target point")
        if not self.tracking["valid"]:
            raise CalibrationError("Valid Pan and Tilt tracking is required to capture a mark")
        pan, tilt = self.tracking["axes"]["pan"], self.tracking["axes"]["tilt"]
        if not pan["valid"] or not tilt["valid"] or pan["raw"] is None or tilt["raw"] is None:
            raise CalibrationError("Pan and Tilt samples are unavailable")
        observation = {
            "target": selected,
            "captured_mono": time.monotonic(),
            "raw_pan": float(pan["raw"]),
            "raw_tilt": float(tilt["raw"]),
            "mapped_pan": float(pan["value"]),
            "mapped_tilt": float(tilt["value"]),
            "setup_profile_id": self.profile_index["active_id"],
            "synthetic": False,
        }
        async with self.lock:
            replaced = selected in self.world_calibration["observations"]
            self.world_calibration["observations"][selected] = observation
            self.world_calibration.update({"valid": False, "status": "MARKS CHANGED — SOLVE REQUIRED"})
            self._write_active_world_calibration_locked()
            self._refresh_camera_from_tracking()
        self.debug.app("calibration_mark_replaced" if replaced else "calibration_mark_recorded", calibration_id=self.world_calibration["id"], **observation)
        await self.broadcast_state(); return deepcopy(observation)

    async def clear_world_mark(self, target: str) -> bool:
        if target not in TARGET_POINTS:
            raise CalibrationError("Unknown calibration target point")
        async with self.lock:
            removed = self.world_calibration["observations"].pop(target, None)
            if removed is None:
                return False
            self.world_calibration.update({"valid": False, "status": "MARK CLEARED — SOLVE REQUIRED"})
            self._write_active_world_calibration_locked()
            self._refresh_camera_from_tracking()
        self.debug.app("calibration_mark_cleared", calibration_id=self.world_calibration["id"], target=target)
        await self.broadcast_state(); return True

    async def solve_active_world_calibration(self) -> dict[str, Any]:
        if not self.tracking["valid"]:
            raise CalibrationError("Valid Pan and Tilt tracking is required to solve")
        candidate = deepcopy(self.world_calibration)
        calibration_id = candidate["id"]
        self.debug.app("calibration_solve_started", calibration_id=calibration_id, observation_count=len(candidate.get("observations") or {}))
        try:
            solution = solve_world_calibration(candidate)
        except CalibrationError as exc:
            async with self.lock:
                self.world_calibration.update({"valid": False, "status": f"SOLVE FAILED: {exc}", "last_solve_error": str(exc)})
                self._write_active_world_calibration_locked()
                self._refresh_camera_from_tracking()
            self.debug.error("calibration_solve_failed", calibration_id=calibration_id, error=str(exc))
            await self.broadcast_state()
            raise
        async with self.lock:
            self.world_calibration.update({"solution": solution, "valid": True, "status": "CALIBRATION VALID", "last_solve_error": None})
            self._write_active_world_calibration_locked()
            self._refresh_camera_from_tracking()
        self.debug.app("calibration_solve_success", calibration_id=calibration_id, camera=solution["camera"], pan_offset=solution["pan_offset"], tilt_offset=solution["tilt_offset"], rms=solution["rms_angular_error"], maximum=solution["max_angular_error"], residuals=solution["residuals"])
        if solution["rms_angular_error"] > 0.5:
            self.debug.app("calibration_residual_warning", calibration_id=calibration_id, rms=solution["rms_angular_error"], maximum=solution["max_angular_error"])
        await self.broadcast_state(); return deepcopy(solution)

    async def reset_world_calibration(self) -> None:
        async with self.lock:
            self.world_calibration.update({"current_target": TARGET_POINTS[0], "observations": {}, "solution": None, "valid": False, "status": "NOT CALIBRATED", "last_solve_error": None})
            self._write_active_world_calibration_locked()
            self._refresh_camera_from_tracking()
        self.debug.app("world_calibration_reset", calibration_id=self.world_calibration["id"])
        await self.broadcast_state()

    async def generate_synthetic_world_calibration(self, values: dict[str, Any]) -> dict[str, Any]:
        camera = {axis: float((values.get("camera") or {}).get(axis, self.world_calibration["camera_hint"][axis])) for axis in ("x", "y", "z")}
        pan_offset = float(values.get("pan_offset", 0.0))
        tilt_offset = float(values.get("tilt_offset", 0.0))
        update: dict[str, Any] = {"camera_hint": camera, "orientation_hint": {"pan_offset": pan_offset, "tilt_offset": tilt_offset}}
        if "target" in values:
            update["target"] = values["target"]
        await self.update_world_calibration(update)
        observations = synthetic_observations(self.world_calibration, camera, pan_offset, tilt_offset)
        async with self.lock:
            self.world_calibration["observations"] = observations
            self.world_calibration["synthetic_ground_truth"] = {"camera": camera, "pan_offset": pan_offset, "tilt_offset": tilt_offset}
            self.world_calibration.update({"valid": False, "status": "SYNTHETIC MARKS GENERATED"})
            self._write_active_world_calibration_locked()
        self.debug.app("synthetic_calibration_generated", calibration_id=self.world_calibration["id"], camera=camera, pan_offset=pan_offset, tilt_offset=tilt_offset, target=self.world_calibration["target"])
        await self.broadcast_state()
        return await self.solve_active_world_calibration()

    def _new_item(self, item_type: str) -> dict[str, Any]:
        common = {"id": _new_id(), "type": item_type, "visible": True, "color": "#ffffff"}
        if item_type == "text":
            return {**common, "name": "Text", "text": "TEXT", "size": 42, "x": 0.0, "y": 10.0, "z": 0.1}
        if item_type == "marker":
            return {**common, "name": "Marker", "size": 0.5, "label": "MARKER", "show_label": True, "x": 0.0, "y": 10.0, "z": 0.0}
        return {**common, "name": "Line", "width": 5, "x1": -2.0, "y1": 10.0, "z1": 0.0, "x2": 2.0, "y2": 10.0, "z2": 0.0}

    async def add_item(self, item_type: str) -> dict[str, Any]:
        item = self._new_item(item_type)
        async with self.lock:
            self.scene["items"].append(item); self._persist_active_locked()
        self.debug.app("marker_created" if item_type == "marker" else "scene_item_added", id=item["id"], type=item_type, name=item["name"])
        await self.broadcast_state(); return deepcopy(item)

    def _set_item_group_locked(self, item_id: str, group_id: str | None) -> bool:
        before = next((g["id"] for g in self.scene["groups"] if item_id in g.get("item_ids", [])), None)
        for group in self.scene["groups"]:
            group["item_ids"] = [value for value in group.get("item_ids", []) if value != item_id]
        target = next((g for g in self.scene["groups"] if g["id"] == group_id), None) if group_id else None
        if group_id and target is None:
            raise ValueError("Group not found")
        if target:
            target["item_ids"].append(item_id)
        return before != group_id

    async def update_item(self, item_id: str, updates: dict[str, Any], persist: bool = True) -> dict[str, Any] | None:
        group_changed = False
        async with self.lock:
            item = next((x for x in self.scene["items"] if x.get("id") == item_id), None)
            if item is None:
                return None
            if "group_id" in updates:
                group_changed = self._set_item_group_locked(item_id, updates.get("group_id"))
            for key, value in updates.items():
                if key not in {"id", "type", "group_id"}:
                    item[key] = value
            if persist:
                self._persist_active_locked()
            else:
                self.layout_dirty = True
            saved = deepcopy(item)
        if group_changed:
            self.debug.app("object_group_changed", id=item_id, group_id=updates.get("group_id"))
        self.debug.app("scene_item_updated" if persist else "scene_item_transient", id=item_id, type=item.get("type"), keys=sorted(updates))
        await self.broadcast_state(); return saved

    async def delete_item(self, item_id: str) -> bool:
        async with self.lock:
            before = len(self.scene["items"]); self.scene["items"] = [x for x in self.scene["items"] if x.get("id") != item_id]
            changed = len(self.scene["items"]) != before
            if changed:
                for group in self.scene["groups"]:
                    group["item_ids"] = [value for value in group.get("item_ids", []) if value != item_id]
                self._persist_active_locked()
        if changed:
            self.debug.app("scene_item_deleted", id=item_id); await self.broadcast_state()
        return changed

    @staticmethod
    def _offset_item(item: dict[str, Any], dx: float, dy: float) -> None:
        if item["type"] == "line":
            item["x1"] = float(item["x1"]) + dx; item["y1"] = float(item["y1"]) + dy
            item["x2"] = float(item["x2"]) + dx; item["y2"] = float(item["y2"]) + dy
        else:
            item["x"] = float(item["x"]) + dx; item["y"] = float(item["y"]) + dy

    async def duplicate_item(self, item_id: str) -> dict[str, Any] | None:
        async with self.lock:
            source = next((x for x in self.scene["items"] if x.get("id") == item_id), None)
            if source is None:
                return None
            duplicate = deepcopy(source); duplicate["id"] = _new_id(); duplicate["name"] = f"{source.get('name', source['type'])} Copy"; self._offset_item(duplicate, 0.5, 0.5)
            self.scene["items"].append(duplicate)
            source_group = next((g for g in self.scene["groups"] if item_id in g.get("item_ids", [])), None)
            if source_group:
                source_group["item_ids"].append(duplicate["id"])
            self._persist_active_locked()
        self.debug.app("object_duplicated", source_id=item_id, id=duplicate["id"], group_id=source_group["id"] if source_group else None)
        await self.broadcast_state(); return deepcopy(duplicate)

    async def set_scene_background(self, color: str) -> None:
        async with self.lock:
            self.scene["background"] = color; self._persist_active_locked()
        self.debug.app("scene_background", color=color); await self.broadcast_state()

    async def add_group(self, name: str) -> dict[str, Any]:
        group = {"id": _new_id(), "name": name.strip() or "Group", "visible": True, "item_ids": []}
        async with self.lock:
            self.scene["groups"].append(group); self._persist_active_locked()
        self.debug.app("group_created", id=group["id"], name=group["name"]); await self.broadcast_state(); return deepcopy(group)

    async def update_group(self, group_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        async with self.lock:
            group = next((g for g in self.scene["groups"] if g["id"] == group_id), None)
            if group is None:
                return None
            before_visible = group["visible"]
            for key in ("name", "visible"):
                if key in updates:
                    group[key] = updates[key]
            self._persist_active_locked(); saved = deepcopy(group)
        if before_visible != group["visible"]:
            self.debug.app("group_visibility_changed", id=group_id, visible=group["visible"])
        await self.broadcast_state(); return saved

    async def delete_group(self, group_id: str, delete_contents: bool = False) -> bool:
        async with self.lock:
            group = next((g for g in self.scene["groups"] if g["id"] == group_id), None)
            if group is None:
                return False
            members = set(group.get("item_ids", [])); self.scene["groups"] = [g for g in self.scene["groups"] if g["id"] != group_id]
            if delete_contents:
                self.scene["items"] = [item for item in self.scene["items"] if item["id"] not in members]
            self._persist_active_locked()
        self.debug.app("group_deleted", id=group_id, name=group["name"], delete_contents=delete_contents, object_count=len(members)); await self.broadcast_state(); return True

    async def translate_group(self, group_id: str, dx: float, dy: float, persist: bool = True) -> dict[str, Any] | None:
        async with self.lock:
            group = next((g for g in self.scene["groups"] if g["id"] == group_id), None)
            if group is None:
                return None
            members = set(group.get("item_ids", []))
            for item in self.scene["items"]:
                if item["id"] in members:
                    self._offset_item(item, dx, dy)
            if persist:
                self._persist_active_locked()
            else:
                self.layout_dirty = True
            result = {"id": group_id, "dx": dx, "dy": dy, "object_count": len(members)}
        self.debug.app("group_drag_commit" if persist else "group_drag_sample", **result); await self.broadcast_state(); return result

    async def duplicate_group(self, group_id: str) -> dict[str, Any] | None:
        async with self.lock:
            source = next((g for g in self.scene["groups"] if g["id"] == group_id), None)
            if source is None:
                return None
            item_map = {item["id"]: item for item in self.scene["items"]}; duplicates = []
            for item_id in source.get("item_ids", []):
                if item_id in item_map:
                    item = deepcopy(item_map[item_id]); item["id"] = _new_id(); self._offset_item(item, 0.5, 0.5); duplicates.append(item)
            group = {"id": _new_id(), "name": f"{source['name']} Copy", "visible": source["visible"], "item_ids": [item["id"] for item in duplicates]}
            self.scene["items"].extend(duplicates); self.scene["groups"].append(group); self._persist_active_locked()
        self.debug.app("group_duplicated", source_id=group_id, id=group["id"], object_count=len(duplicates)); await self.broadcast_state(); return deepcopy(group)

    async def save_layout(self) -> dict[str, Any]:
        async with self.lock:
            self._persist_active_locked(); meta = deepcopy(self._layout_meta())
        self.debug.app("layout_saved", layout_id=meta["id"], name=meta["name"]); await self.broadcast_state(); return meta

    async def create_layout(self, name: str, copy_current: bool = False) -> dict[str, Any]:
        layout_id, now = _new_id(), _utc_now(); meta = {"id": layout_id, "name": name.strip() or "Untitled Layout", "created": now, "modified": now}
        scene = deepcopy(self.scene) if copy_current else {"background": self.scene.get("background", "#00ff00"), "groups": [], "items": []}
        async with self.lock:
            self._atomic_write_path(self._layout_path(layout_id), scene); self.layout_index["layouts"].append(meta); self.layout_index["active_id"] = layout_id
            self._write_index(); self.scene = scene; self.layout_dirty, self.layout_save_error = False, None
        self.debug.app("layout_created", layout_id=layout_id, name=meta["name"], copy_current=copy_current); await self.broadcast_state(); return deepcopy(meta)

    async def load_layout(self, layout_id: str) -> dict[str, Any] | None:
        async with self.lock:
            meta = self._layout_meta(layout_id)
            if meta is None:
                return None
            self.scene = self._read_layout(layout_id); self.layout_index["active_id"] = layout_id; self._write_index(); self.layout_dirty, self.layout_save_error = False, None
        self.debug.app("layout_loaded", layout_id=layout_id, name=meta["name"]); await self.broadcast_state(); return deepcopy(meta)

    async def rename_layout(self, layout_id: str, name: str) -> dict[str, Any] | None:
        async with self.lock:
            meta = self._layout_meta(layout_id)
            if meta is None:
                return None
            old_name = meta["name"]; meta["name"] = name.strip() or old_name; meta["modified"] = _utc_now(); self._write_index(); saved = deepcopy(meta)
        self.debug.app("layout_renamed", layout_id=layout_id, before=old_name, after=saved["name"]); await self.broadcast_state(); return saved

    async def duplicate_layout(self, layout_id: str, name: str) -> dict[str, Any] | None:
        async with self.lock:
            source = self._layout_meta(layout_id)
            if source is None:
                return None
            scene = self._read_layout(layout_id); new_id, now = _new_id(), _utc_now(); meta = {"id": new_id, "name": name.strip() or f"{source['name']} Copy", "created": now, "modified": now}
            self._atomic_write_path(self._layout_path(new_id), scene); self.layout_index["layouts"].append(meta); self.layout_index["active_id"] = new_id; self._write_index(); self.scene = scene; self.layout_dirty, self.layout_save_error = False, None
        self.debug.app("layout_duplicated", source_id=layout_id, layout_id=new_id, name=meta["name"]); await self.broadcast_state(); return deepcopy(meta)

    async def delete_layout(self, layout_id: str) -> bool:
        async with self.lock:
            meta = self._layout_meta(layout_id)
            if meta is None:
                return False
            remaining = [entry for entry in self.layout_index["layouts"] if entry["id"] != layout_id]
            if not remaining:
                now, new_id = _utc_now(), _new_id(); replacement = {"id": new_id, "name": "Default Layout", "created": now, "modified": now}
                self._atomic_write_path(self._layout_path(new_id), _demo_scene()); remaining = [replacement]
            self.layout_index["layouts"] = remaining
            if self.layout_index["active_id"] == layout_id:
                self.layout_index["active_id"] = remaining[0]["id"]; self.scene = self._read_layout(remaining[0]["id"])
            self._write_index()
            try:
                self._layout_path(layout_id).unlink()
            except FileNotFoundError:
                pass
            self.layout_dirty, self.layout_save_error = False, None
        self.debug.app("layout_deleted", layout_id=layout_id, name=meta["name"], active_id=self.layout_index["active_id"]); await self.broadcast_state(); return True

    async def calibration_mark(self, mark: str) -> dict[str, Any]:
        axis = "pan" if mark in {"pan_left", "pan_right"} else "tilt"
        raw = self.tracking["axes"][axis]["raw"]
        if raw is None:
            raise ValueError(f"{axis.title()} source has no raw value")
        value = float(raw)
        async with self.lock:
            marks = self.profile.setdefault("calibration", {}).setdefault("marks", {})
            marks[mark] = value
            self.calibration["marks"] = deepcopy(marks)
            learned: int | None = None
            if "pan_left" in marks and "pan_right" in marks and marks["pan_left"] != marks["pan_right"]:
                self.calibration["pan_direction"] = 1 if marks["pan_right"] > marks["pan_left"] else -1
                if axis == "pan":
                    learned = self.calibration["pan_direction"]
            if "tilt_down" in marks and "tilt_up" in marks and marks["tilt_down"] != marks["tilt_up"]:
                self.calibration["tilt_direction"] = 1 if marks["tilt_up"] > marks["tilt_down"] else -1
                if axis == "tilt":
                    learned = self.calibration["tilt_direction"]
            if learned is not None:
                self.profile["axes"][axis]["mapping"]["direction"] = learned
                self.profile["axes"][axis]["mapping"]["direction_learned"] = True
                self._apply_axis_runtime(axis)
                self._refresh_camera_from_tracking()
            # Persist every mark, not just the mark that completes a pair. This
            # keeps partially completed direction setup scoped to the profile
            # even if the operator switches profiles before taking mark two.
            self._write_active_profile_locked()
            self._save("calibration.json", self.calibration); result = deepcopy(self.calibration)
        self.debug.app("direction_mark_recorded", profile_id=self.profile_index["active_id"], axis=axis, mark=mark, value=value)
        if learned is not None:
            self.debug.app("direction_learned", profile_id=self.profile_index["active_id"], axis=axis, direction=learned)
        await self.broadcast_state(); return result

    async def reset_calibration(self) -> None:
        async with self.lock:
            self.calibration = deepcopy(DEFAULT_CALIBRATION)
            self.profile.setdefault("calibration", {})["marks"] = {}
            for axis in ("pan", "tilt"):
                self.profile["axes"][axis]["mapping"]["direction"] = 1
                self.profile["axes"][axis]["mapping"]["direction_learned"] = False
                self._apply_axis_runtime(axis)
            self._refresh_camera_from_tracking()
            self._write_active_profile_locked()
            self._save("calibration.json", self.calibration)
        self.debug.app("calibration_reset"); await self.broadcast_state()
