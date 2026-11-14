from __future__ import annotations

import math
import uuid
from copy import deepcopy
from itertools import combinations
from typing import Any

SCHEMA_VERSION = 1
MAX_MARKERS = 20


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def default_measured_targets() -> dict[str, Any]:
    return {"version": SCHEMA_VERSION, "active_target_id": None, "targets": []}


def new_measured_target(name: str = "Measured target", marker_count: int = 4) -> dict[str, Any]:
    if not 2 <= marker_count <= MAX_MARKERS:
        raise ValueError(f"A measured target needs 2 to {MAX_MARKERS} markers")
    markers = [{"id": f"M{index + 1}", "label": f"M{index + 1}", "height_m": None, "ground_reference": False}
               for index in range(marker_count)]
    return {"id": _id("measured"), "name": str(name).strip() or "Measured target", "markers": markers,
            "measurements": _all_pairs(markers, {}), "notes": ""}


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted((str(a), str(b))))


def _all_pairs(markers: list[dict[str, Any]], existing: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    pairs = []
    for first, second in combinations(markers, 2):
        key = _pair_key(first["id"], second["id"])
        old = existing.get(key) or {}
        pairs.append({"a": key[0], "b": key[1], "distance_m": old.get("distance_m"),
                      "constraint_type": old.get("constraint_type", "physical_distance")})
    return pairs


def normalise_measured_targets(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    version = int(source.get("version", SCHEMA_VERSION))
    if version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported measured-target schema version {version}")
    targets = []
    seen_targets: set[str] = set()
    for raw in source.get("targets") or []:
        if not isinstance(raw, dict):
            raise ValueError("Measured targets must be objects")
        target_id = str(raw.get("id") or _id("measured"))
        if target_id in seen_targets:
            raise ValueError("Measured target IDs must be unique")
        seen_targets.add(target_id)
        raw_markers = raw.get("markers") or []
        if not 2 <= len(raw_markers) <= MAX_MARKERS:
            raise ValueError(f"A measured target needs 2 to {MAX_MARKERS} markers")
        markers, seen_markers = [], set()
        for index, marker in enumerate(raw_markers):
            marker_id = str((marker or {}).get("id") or f"M{index + 1}")
            if marker_id in seen_markers:
                raise ValueError("Marker IDs must be unique within a measured target")
            seen_markers.add(marker_id)
            height = (marker or {}).get("height_m")
            if height in (None, ""):
                height = None
            else:
                height = float(height)
                if not math.isfinite(height) or height < 0:
                    raise ValueError("Marker height must be a finite value at or above ground")
            markers.append({"id": marker_id, "label": str((marker or {}).get("label") or marker_id)[:80],
                            "height_m": height, "ground_reference": bool((marker or {}).get("ground_reference", False))})
        existing: dict[tuple[str, str], dict[str, Any]] = {}
        for measurement in raw.get("measurements") or []:
            if not isinstance(measurement, dict):
                continue
            key = _pair_key(measurement.get("a"), measurement.get("b"))
            if key[0] == key[1] or not set(key) <= seen_markers:
                raise ValueError("Measured-target pair refers to an unknown marker")
            distance = measurement.get("distance_m")
            if distance in (None, ""):
                distance = None
            else:
                distance = float(distance)
                if not math.isfinite(distance) or distance <= 0:
                    raise ValueError("Marker distance must be a finite positive value")
            kind = measurement.get("constraint_type", "physical_distance")
            if kind not in {"physical_distance", "ground_slant"}:
                raise ValueError("Measurement type must be physical_distance or ground_slant")
            if kind == "ground_slant" and not any(marker["id"] in key and marker["ground_reference"] for marker in markers):
                raise ValueError("A ground-slant measurement must include an identified ground marker")
            if key in existing:
                raise ValueError("Each marker pair may have only one measurement")
            existing[key] = {"distance_m": distance, "constraint_type": kind}
        targets.append({"id": target_id, "name": str(raw.get("name") or "Measured target")[:80],
                        "markers": markers, "measurements": _all_pairs(markers, existing),
                        "notes": str(raw.get("notes") or "")[:2000]})
    active = source.get("active_target_id")
    active = str(active) if active is not None and str(active) in seen_targets else (targets[0]["id"] if targets else None)
    return {"version": SCHEMA_VERSION, "active_target_id": active, "targets": targets}


def validate_measured_target(target: dict[str, Any]) -> dict[str, Any]:
    clean = normalise_measured_targets({"version": SCHEMA_VERSION, "active_target_id": target.get("id"), "targets": [target]})["targets"][0]
    markers = {marker["id"]: marker for marker in clean["markers"]}
    measured = { _pair_key(item["a"], item["b"]): item["distance_m"] for item in clean["measurements"] if item["distance_m"] is not None }
    issues: list[dict[str, str]] = []
    for (a, b), distance in measured.items():
        ha, hb = markers[a]["height_m"], markers[b]["height_m"]
        if ha is not None and hb is not None and abs(ha - hb) > distance + 1e-6:
            issues.append({"severity": "error", "code": "height_distance_conflict", "message": f"{a}–{b}: height difference exceeds measured distance"})
    for a, b, c in combinations(markers, 3):
        sides = [measured.get(_pair_key(a, b)), measured.get(_pair_key(a, c)), measured.get(_pair_key(b, c))]
        if all(side is not None for side in sides):
            sides = sorted(sides)
            if sides[0] + sides[1] <= sides[2] + 1e-6:
                issues.append({"severity": "error", "code": "triangle_conflict", "message": f"{a}, {b}, {c}: distances violate the triangle inequality"})
    known_heights = sum(marker["height_m"] is not None for marker in markers.values())
    constraint_count = len(measured) + known_heights
    required = max(1, 3 * len(markers) - 6)
    if constraint_count < required:
        issues.append({"severity": "warning", "code": "underconstrained", "message": f"Geometry is underconstrained: {constraint_count} known constraints; at least {required} independent constraints are normally needed before pose/orientation can be estimated"})
    return {"valid": not any(issue["severity"] == "error" for issue in issues), "issues": issues,
            "marker_count": len(markers), "pair_count": len(clean["measurements"]),
            "measured_pair_count": len(measured), "known_height_count": known_heights,
            "preparatory_only": True}


def duplicate_measured_target(target: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    duplicate = deepcopy(target)
    duplicate["id"] = _id("measured")
    duplicate["name"] = str(name or f"{target.get('name', 'Measured target')} Copy")[:80]
    return duplicate
