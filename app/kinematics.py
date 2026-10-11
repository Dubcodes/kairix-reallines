from __future__ import annotations

import hashlib
import json
import math
from typing import Any


RIG_OFFSET_FIELDS = ("right_m", "forward_m", "up_m")
DEFAULT_RIG_OFFSET = {field: 0.0 for field in RIG_OFFSET_FIELDS}
MAX_ABS_RIG_OFFSET_METRES = 2.0


class RigGeometryError(ValueError):
    pass


def normalise_rig_offset(value: dict[str, Any] | None) -> dict[str, float]:
    source = value or {}
    result: dict[str, float] = {}
    for field in RIG_OFFSET_FIELDS:
        try:
            number = float(source.get(field, 0.0))
        except (TypeError, ValueError) as exc:
            raise RigGeometryError(f"Rig offset {field} must be numeric") from exc
        if not math.isfinite(number):
            raise RigGeometryError(f"Rig offset {field} must be finite")
        if abs(number) > MAX_ABS_RIG_OFFSET_METRES:
            raise RigGeometryError(
                f"Rig offset {field} must be within +/-{MAX_ABS_RIG_OFFSET_METRES:g} m"
            )
        result[field] = number
    return result


def camera_position(
    pivot: dict[str, Any],
    pan_degrees: float,
    tilt_degrees: float,
    rig_offset: dict[str, Any] | None,
) -> dict[str, float]:
    """Return optical-centre XYZ after local tilt followed by world pan.

    At zero angles local right/forward/up align with world +X/+Y/+Z.
    Positive tilt rotates local forward toward +Z; positive pan rotates +Y
    toward +X, matching the RealLines camera orientation convention.
    """
    offset = normalise_rig_offset(rig_offset)
    try:
        px, py, pz = (float(pivot[axis]) for axis in ("x", "y", "z"))
        pan = math.radians(float(pan_degrees))
        tilt = math.radians(float(tilt_degrees))
    except (KeyError, TypeError, ValueError) as exc:
        raise RigGeometryError("Pivot XYZ and Pan/Tilt must be numeric") from exc
    if not all(math.isfinite(value) for value in (px, py, pz, pan, tilt)):
        raise RigGeometryError("Pivot XYZ and Pan/Tilt must be finite")

    right = offset["right_m"]
    forward = offset["forward_m"]
    up = offset["up_m"]
    tilted_forward = forward * math.cos(tilt) - up * math.sin(tilt)
    tilted_up = forward * math.sin(tilt) + up * math.cos(tilt)
    world_x = right * math.cos(pan) + tilted_forward * math.sin(pan)
    world_y = -right * math.sin(pan) + tilted_forward * math.cos(pan)
    return {"x": px + world_x, "y": py + world_y, "z": pz + tilted_up}


def rig_geometry_fingerprint(rig_offset: dict[str, Any] | None) -> str:
    encoded = json.dumps(
        {"schema": 1, "model": "intersecting_pan_tilt_v1", "rig_offset": normalise_rig_offset(rig_offset)},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
