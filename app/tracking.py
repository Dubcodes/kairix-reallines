from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any


AXES = ("pan", "tilt", "zoom", "focus")
SOURCE_TYPES = ("simulator", "disabled", "quadrature_gpio", "linux_rotary", "imu", "external")
REQUIRED_AXES = ("pan", "tilt")
ENCODER_SOURCES = ("quadrature_gpio", "linux_rotary")
ALLOWED_DECODE_MULTIPLIERS = (1, 2, 4)
DEFAULT_GPIO_BY_AXIS = {
    "pan": {"chip": "/dev/gpiochip0", "line_a": 17, "line_b": 27},
    "tilt": {"chip": "/dev/gpiochip0", "line_a": 22, "line_b": 23},
}
DEFAULT_ROTARY_BY_AXIS = {
    "pan": {"discovery": "auto", "rel_axis": "REL_X", "pin_a": 17, "pin_b": 27, "steps_per_period": 4, "device_override": ""},
    "tilt": {"discovery": "auto", "rel_axis": "REL_Y", "pin_a": 22, "pin_b": 23, "steps_per_period": 4, "device_override": ""},
}


class TrackingConfigError(ValueError):
    pass


def default_axis_config(axis: str, source: str | None = None) -> dict[str, Any]:
    if axis not in AXES:
        raise TrackingConfigError(f"Unknown tracking axis: {axis}")
    selected = source or ("simulator" if axis in {"pan", "tilt", "zoom"} else "disabled")
    source_config = {
        "stale_timeout_seconds": 2.0,
        "chip": "",
        "line_a": None,
        "line_b": None,
        "bias": "as_is",
    }
    source_config.update(DEFAULT_GPIO_BY_AXIS.get(axis, {}))
    source_config.update(DEFAULT_ROTARY_BY_AXIS.get(axis, {}))
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
        "source_config": source_config,
    }


def default_profile(name: str = "Development Simulator") -> dict[str, Any]:
    return {
        "name": name,
        "axes": {axis: default_axis_config(axis) for axis in AXES},
        "camera": {
            "fov": 60.0,
            "height": 1.7,
            "rig_model": "intersecting_pan_tilt_v1",
            "rig_offset": {"right_m": 0.0, "forward_m": 0.0, "up_m": 0.0},
        },
        "calibration": {"marks": {}},
    }


def normalise_axis_config(axis: str, value: dict[str, Any] | None) -> dict[str, Any]:
    result = default_axis_config(axis)
    value = value or {}
    result["source"] = value.get("source", result["source"])
    result["mapping"].update(value.get("mapping") or {})
    source_config = deepcopy(value.get("source_config") or {})
    # Profiles written before the standard hardware mapping existed contain
    # explicit empty placeholders. Treat that exact legacy shape as
    # unconfigured, while preserving every real persisted GPIO assignment.
    if (
        axis in DEFAULT_GPIO_BY_AXIS
        and not source_config.get("chip")
        and source_config.get("line_a") is None
        and source_config.get("line_b") is None
    ):
        for key in ("chip", "line_a", "line_b"):
            source_config.pop(key, None)
    result["source_config"].update(source_config)
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
    if source in ENCODER_SOURCES:
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
        if source == "linux_rotary" and mapping.get("quadrature_multiplier") != 4:
            raise TrackingConfigError("Linux rotary encoder requires quadrature multiplier ×4")
    if source == "quadrature_gpio":
        source_config = config.get("source_config") or {}
        bias = source_config.get("bias", "as_is")
        if bias not in {"as_is", "pull_up", "pull_down"}:
            raise TrackingConfigError("GPIO bias must be as_is, pull_up or pull_down")
        line_a, line_b = source_config.get("line_a"), source_config.get("line_b")
        if line_a is not None or line_b is not None:
            if line_a is None or line_b is None:
                raise TrackingConfigError("Both GPIO line offsets are required")
            try:
                line_a, line_b = int(line_a), int(line_b)
            except (TypeError, ValueError) as exc:
                raise TrackingConfigError("GPIO line offsets must be integers") from exc
            if line_a < 0 or line_b < 0:
                raise TrackingConfigError("GPIO line offsets must be zero or greater")
            if line_a == line_b:
                raise TrackingConfigError("GPIO A and B must use different lines")
    elif source == "linux_rotary":
        if axis not in DEFAULT_ROTARY_BY_AXIS:
            raise TrackingConfigError("Linux rotary encoder is available for Pan and Tilt only")
        source_config = config.get("source_config") or {}
        expected_axis = DEFAULT_ROTARY_BY_AXIS[axis]["rel_axis"]
        if source_config.get("discovery", "auto") != "auto":
            raise TrackingConfigError("Linux rotary discovery must be auto")
        if source_config.get("rel_axis", expected_axis) != expected_axis:
            raise TrackingConfigError(f"{axis.title()} Linux rotary axis must be {expected_axis}")
        if source_config.get("steps_per_period", 4) != 4:
            raise TrackingConfigError("Linux rotary encoder requires steps-per-period=4")
        try:
            pin_a, pin_b = int(source_config["pin_a"]), int(source_config["pin_b"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TrackingConfigError("Linux rotary pin A and B must be GPIO integers") from exc
        if pin_a < 0 or pin_b < 0 or pin_a == pin_b:
            raise TrackingConfigError("Linux rotary GPIO pins must be distinct and non-negative")
        override = str(source_config.get("device_override", ""))
        if override and not override.startswith("/dev/input/event"):
            raise TrackingConfigError("Linux rotary device override must be /dev/input/eventN")


def merge_axis_update(axis: str, current: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    candidate = deepcopy(current)
    for key in ("source",):
        if key in updates:
            candidate[key] = updates[key]
    for section in ("mapping", "source_config"):
        if section in updates:
            candidate.setdefault(section, {}).update(updates[section] or {})
    if candidate.get("source") == "linux_rotary":
        candidate.setdefault("mapping", {})["quadrature_multiplier"] = 4
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
    if config["source"] in ENCODER_SOURCES:
        return offset + direction * float(raw) * degrees_per_count(config)
    return offset + direction * float(raw)


def map_referenced_count(config: dict[str, Any], raw_count: int, reference_count: int, reference_angle: float) -> float:
    mapping = config["mapping"]
    return (
        float(reference_angle)
        + float(mapping.get("offset", 0.0))
        + int(mapping["direction"]) * (int(raw_count) - int(reference_count)) * degrees_per_count(config)
    )


def tracking_fingerprint(profile: dict[str, Any]) -> str:
    """Stable identity of the tracking geometry a world solve depends upon."""
    axes: dict[str, Any] = {}
    for axis in REQUIRED_AXES:
        config = profile["axes"][axis]
        mapping = config.get("mapping") or {}
        source_config = config.get("source_config") or {}
        axes[axis] = {
            "source": config.get("source"),
            "direction": int(mapping.get("direction", 1)),
            "offset": float(mapping.get("offset", 0.0)),
            "ppr": float(mapping.get("ppr", 600)),
            "quadrature_multiplier": int(mapping.get("quadrature_multiplier", 4)),
            "encoder_revs_per_camera_rev": float(mapping.get("encoder_revs_per_camera_rev", 1.0)),
            "gpio": {
                "chip": str(source_config.get("chip", "")),
                "line_a": int(source_config["line_a"]) if source_config.get("line_a") is not None else None,
                "line_b": int(source_config["line_b"]) if source_config.get("line_b") is not None else None,
                "bias": source_config.get("bias", "as_is"),
            },
            "reference_semantics": "session_count_plus_absolute_angle_v1",
        }
        if config.get("source") == "linux_rotary":
            axes[axis]["linux_rotary"] = {
                "driver": "rotary-encoder",
                "discovery": source_config.get("discovery", "auto"),
                "rel_axis": source_config.get("rel_axis"),
                "pin_a": int(source_config["pin_a"]),
                "pin_b": int(source_config["pin_b"]),
                "steps_per_period": int(source_config.get("steps_per_period", 4)),
            }
    encoded = json.dumps({"schema": 1, "axes": axes}, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
