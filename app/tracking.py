from __future__ import annotations

from copy import deepcopy
from typing import Any


AXES = ("pan", "tilt", "zoom", "focus")
SOURCE_TYPES = ("simulator", "disabled", "quadrature_gpio", "imu", "external")
REQUIRED_AXES = ("pan", "tilt")
ALLOWED_DECODE_MULTIPLIERS = (1, 2, 4)


class TrackingConfigError(ValueError):
    pass


def default_axis_config(axis: str, source: str | None = None) -> dict[str, Any]:
    if axis not in AXES:
        raise TrackingConfigError(f"Unknown tracking axis: {axis}")
    selected = source or ("simulator" if axis in {"pan", "tilt", "zoom"} else "disabled")
    return {
        "source": selected,
        "mapping": {
            "direction": 1,
            "direction_learned": False,
            "offset": 0.0,
            "ppr": 600,
            "quadrature_multiplier": 4,
            "encoder_revs_per_camera_rev": 1.0,
        },
        "source_config": {"stale_timeout_seconds": 2.0},
    }


def default_profile(name: str = "Development Simulator") -> dict[str, Any]:
    return {
        "name": name,
        "axes": {axis: default_axis_config(axis) for axis in AXES},
        "camera": {"fov": 60.0, "height": 1.7},
        "calibration": {"marks": {}},
    }


def normalise_axis_config(axis: str, value: dict[str, Any] | None) -> dict[str, Any]:
    result = default_axis_config(axis)
    value = value or {}
    result["source"] = value.get("source", result["source"])
    result["mapping"].update(value.get("mapping") or {})
    result["source_config"].update(value.get("source_config") or {})
    validate_axis_config(axis, result)
    return result


def validate_axis_config(axis: str, config: dict[str, Any]) -> None:
    if axis not in AXES:
        raise TrackingConfigError(f"Unknown tracking axis: {axis}")
    source = config.get("source")
    if source not in SOURCE_TYPES:
        raise TrackingConfigError(f"Unknown source type: {source}")
    mapping = config.get("mapping") or {}
    direction = mapping.get("direction")
    if direction not in {-1, 1}:
        raise TrackingConfigError("Direction must be +1 or -1")
    try:
        float(mapping.get("offset", 0.0))
    except (TypeError, ValueError) as exc:
        raise TrackingConfigError("Offset must be numeric") from exc
    try:
        stale_timeout = float((config.get("source_config") or {}).get("stale_timeout_seconds", 2.0))
    except (TypeError, ValueError) as exc:
        raise TrackingConfigError("Stale timeout must be numeric") from exc
    if stale_timeout <= 0:
        raise TrackingConfigError("Stale timeout must be greater than zero")
    if source == "quadrature_gpio":
        try:
            ppr = float(mapping.get("ppr"))
            ratio = float(mapping.get("encoder_revs_per_camera_rev"))
        except (TypeError, ValueError) as exc:
            raise TrackingConfigError("PPR and gearing must be numeric") from exc
        if ppr <= 0:
            raise TrackingConfigError("PPR must be greater than zero")
        if mapping.get("quadrature_multiplier") not in ALLOWED_DECODE_MULTIPLIERS:
            raise TrackingConfigError("Quadrature multiplier must be 1, 2 or 4")
        if ratio <= 0:
            raise TrackingConfigError("Encoder-to-camera gearing must be greater than zero")


def merge_axis_update(axis: str, current: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    candidate = deepcopy(current)
    for key in ("source",):
        if key in updates:
            candidate[key] = updates[key]
    for section in ("mapping", "source_config"):
        if section in updates:
            candidate.setdefault(section, {}).update(updates[section] or {})
    validate_axis_config(axis, candidate)
    return candidate


def counts_per_camera_revolution(config: dict[str, Any]) -> float:
    mapping = config["mapping"]
    return float(mapping["ppr"]) * int(mapping["quadrature_multiplier"]) * float(mapping["encoder_revs_per_camera_rev"])


def degrees_per_count(config: dict[str, Any]) -> float:
    return 360.0 / counts_per_camera_revolution(config)


def map_raw_value(axis: str, raw: float, config: dict[str, Any]) -> float:
    validate_axis_config(axis, config)
    mapping = config["mapping"]
    direction = int(mapping["direction"])
    offset = float(mapping.get("offset", 0.0))
    if config["source"] == "quadrature_gpio":
        return offset + direction * float(raw) * degrees_per_count(config)
    return offset + direction * float(raw)
