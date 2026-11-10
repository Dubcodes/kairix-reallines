from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .kinematics import camera_position, normalise_rig_offset


REFERENCE_WIDTH = 1920.0
REFERENCE_HEIGHT = 1080.0
MAX_TARGETS = 32
MAX_MARKERS = 20
MAX_OBSERVATIONS = 120


class DotCalibrationError(ValueError):
    pass


def _finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DotCalibrationError(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise DotCalibrationError(f"{label} must be finite")
    return number


def default_dot_calibration() -> dict[str, Any]:
    return {
        "targets": [],
        "active_target_id": None,
        "last_solution": None,
    }


def normalise_dot_calibration(value: dict[str, Any] | None) -> dict[str, Any]:
    source = deepcopy(value or {})
    targets = source.get("targets") or []
    if not isinstance(targets, list) or len(targets) > MAX_TARGETS:
        raise DotCalibrationError(f"Dot calibration supports at most {MAX_TARGETS} targets")
    normalised_targets = []
    seen_targets: set[str] = set()
    for raw_target in targets:
        if not isinstance(raw_target, dict):
            raise DotCalibrationError("Dot calibration targets must be objects")
        target_id = str(raw_target.get("id") or "").strip()
        if not target_id or target_id in seen_targets:
            raise DotCalibrationError("Each Dot calibration target needs a unique id")
        seen_targets.add(target_id)
        target_type = str(raw_target.get("type") or "free")
        if target_type not in {"free", "rectangle"}:
            raise DotCalibrationError("Dot calibration target type must be free or rectangle")
        distance = _finite(raw_target.get("approx_distance_m", 10.0), "Approximate target distance")
        if not 0.05 <= distance <= 5000.0:
            raise DotCalibrationError("Approximate target distance must be between 0.05 and 5000 m")
        width = raw_target.get("width_m")
        height = raw_target.get("height_m")
        if width is not None:
            width = _finite(width, "Target width")
            if width <= 0:
                raise DotCalibrationError("Target width must be greater than zero")
        if height is not None:
            height = _finite(height, "Target height")
            if height <= 0:
                raise DotCalibrationError("Target height must be greater than zero")
        markers = raw_target.get("markers") or []
        if not isinstance(markers, list) or len(markers) > MAX_MARKERS:
            raise DotCalibrationError(f"A Dot calibration target supports at most {MAX_MARKERS} markers")
        normalised_markers = []
        seen_markers: set[str] = set()
        for index, marker in enumerate(markers):
            marker = marker or {}
            marker_id = str(marker.get("id") or f"M{index + 1}").strip()
            if not marker_id or marker_id in seen_markers:
                raise DotCalibrationError("Dot marker ids must be unique")
            seen_markers.add(marker_id)
            normalised_markers.append({"id": marker_id, "label": str(marker.get("label") or marker_id)[:32]})
        observations = raw_target.get("observations") or []
        if not isinstance(observations, list) or len(observations) > MAX_OBSERVATIONS:
            raise DotCalibrationError(f"A Dot calibration target supports at most {MAX_OBSERVATIONS} observations")
        clean_observations = []
        for obs_index, observation in enumerate(observations):
            if not isinstance(observation, dict):
                raise DotCalibrationError("Dot calibration observations must be objects")
            points = observation.get("points") or {}
            if not isinstance(points, dict):
                raise DotCalibrationError("Observation points must be an object keyed by marker id")
            clean_points: dict[str, dict[str, float]] = {}
            for marker_id, point in points.items():
                if marker_id not in seen_markers or not isinstance(point, dict):
                    continue
                x = _finite(point.get("x"), f"{marker_id} X")
                y = _finite(point.get("y"), f"{marker_id} Y")
                if not -0.25 <= x <= 1.25 or not -0.25 <= y <= 1.25:
                    raise DotCalibrationError("Dot coordinates are outside the supported viewport range")
                clean_points[marker_id] = {"x": x, "y": y}
            axes = deepcopy(observation.get("axes") or {})
            camera = deepcopy(observation.get("camera") or {})
            clean_observations.append({
                "id": str(observation.get("id") or f"O{obs_index + 1}"),
                "captured_mono": float(observation.get("captured_mono") or 0.0),
                "references": deepcopy(observation.get("references") or {}),
                "axes": axes,
                "camera": camera,
                "points": clean_points,
            })
        model_points = raw_target.get("model_points") or {}
        clean_model: dict[str, dict[str, float]] = {}
        if isinstance(model_points, dict):
            for marker_id, point in model_points.items():
                if marker_id not in seen_markers or not isinstance(point, dict):
                    continue
                try:
                    clean_model[marker_id] = {axis: _finite(point.get(axis), f"{marker_id} model {axis}") for axis in ("x", "y", "z")}
                except DotCalibrationError:
                    continue
        normalised_targets.append({
            "id": target_id,
            "name": str(raw_target.get("name") or f"Target {len(normalised_targets) + 1}")[:80],
            "type": target_type,
            "approx_distance_m": distance,
            "width_m": width,
            "height_m": height,
            "bottom_height_m": _finite(raw_target.get("bottom_height_m", 0.0), "Target bottom height"),
            "edge_midpoints": bool(raw_target.get("edge_midpoints", False)),
            "markers": normalised_markers,
            "observations": clean_observations,
            "model_points": clean_model,
        })
    active_target_id = source.get("active_target_id")
    if active_target_id not in seen_targets:
        active_target_id = normalised_targets[0]["id"] if normalised_targets else None
    return {
        "targets": normalised_targets,
        "active_target_id": active_target_id,
        "last_solution": deepcopy(source.get("last_solution")),
    }


def zoom_fov_for_count(count: int | float | None, zoom_mapping: dict[str, Any], curvature: float | None = None) -> float | None:
    wide, tight = zoom_mapping.get("wide"), zoom_mapping.get("tight")
    if count is None or not wide or not tight:
        return None
    wide_count, tight_count = int(wide["relative_count"]), int(tight["relative_count"])
    if wide_count == tight_count:
        return None
    wide_fov, tight_fov = float(wide["fov"]), float(tight["fov"])
    t = (float(count) - wide_count) / (tight_count - wide_count)
    t = max(0.0, min(1.0, t))
    wide_proxy = 1.0 / math.tan(math.radians(wide_fov) / 2.0)
    tight_proxy = 1.0 / math.tan(math.radians(tight_fov) / 2.0)
    curve = float(zoom_mapping.get("curvature", 0.0) if curvature is None else curvature)
    live_proxy = wide_proxy + t * (tight_proxy - wide_proxy) + curve * t * (1.0 - t) * (tight_proxy - wide_proxy)
    if live_proxy <= 1e-9:
        return None
    return math.degrees(2.0 * math.atan(1.0 / live_proxy))


def _axis_angle(observation: dict[str, Any], axis: str, config: dict[str, Any], ratio: float) -> float:
    axis_obs = (observation.get("axes") or {}).get(axis) or {}
    try:
        raw_count = int(axis_obs["raw_count"])
        reference_count = int(axis_obs["reference_count"])
        reference_angle = float(axis_obs.get("reference_angle", 0.0))
    except (KeyError, TypeError, ValueError) as exc:
        raise DotCalibrationError(f"Observation is missing {axis} encoder reference data") from exc
    mapping = config["mapping"]
    ppr = float(mapping["ppr"])
    multiplier = int(mapping["quadrature_multiplier"])
    direction = int(mapping["direction"])
    offset = float(mapping.get("offset", 0.0))
    dpc = 360.0 / (ppr * multiplier * ratio)
    return reference_angle + offset + direction * (raw_count - reference_count) * dpc


def _camera_for_observation(
    observation: dict[str, Any], profile: dict[str, Any], pan_ratio: float, tilt_ratio: float,
    zoom_mapping: dict[str, Any], curvature: float, horizontal_fov: float | None = None,
) -> dict[str, float]:
    captured = observation.get("camera") or {}
    camera = {
        "x": float(captured.get("x", 0.0)),
        "y": float(captured.get("y", 0.0)),
        "z": float(captured.get("z", captured.get("height", 1.7))),
        "height": float(captured.get("height", captured.get("z", 1.7))),
        "roll": float(captured.get("roll", 0.0)),
        "pan": _axis_angle(observation, "pan", profile["axes"]["pan"], pan_ratio),
        "tilt": _axis_angle(observation, "tilt", profile["axes"]["tilt"], tilt_ratio),
    }
    zoom_axis = (observation.get("axes") or {}).get("zoom") or {}
    zoom_count = zoom_axis.get("relative_count")
    fov = None if horizontal_fov is not None else zoom_fov_for_count(zoom_count, zoom_mapping, curvature)
    if fov is None:
        fov = float(horizontal_fov if horizontal_fov is not None else captured.get("fov", profile.get("camera", {}).get("fov", 60.0)))
    camera["fov"] = fov
    return camera


def _camera_ray(point: dict[str, float], camera: dict[str, float]) -> tuple[dict[str, float], dict[str, float]]:
    fov = max(5.0, min(160.0, float(camera.get("fov", 60.0)))) * math.pi / 180.0
    focal = REFERENCE_WIDTH / (2.0 * math.tan(fov / 2.0))
    camera_x = (float(point["x"]) * REFERENCE_WIDTH - REFERENCE_WIDTH / 2.0) / focal
    camera_z = (REFERENCE_HEIGHT / 2.0 - float(point["y"]) * REFERENCE_HEIGHT) / focal
    tilt = -math.radians(float(camera.get("tilt", 0.0)))
    pan = math.radians(float(camera.get("pan", 0.0)))
    roll = math.radians(float(camera.get("roll", 0.0)))
    unrolled_x = math.cos(roll) * camera_x - math.sin(roll) * camera_z
    unrolled_z = math.sin(roll) * camera_x + math.cos(roll) * camera_z
    pan_y = math.cos(tilt) + math.sin(tilt) * unrolled_z
    dz = -math.sin(tilt) + math.cos(tilt) * unrolled_z
    origin = {"x": float(camera.get("x", 0.0)), "y": float(camera.get("y", 0.0)), "z": float(camera.get("z", 1.7))}
    direction = {
        "x": math.cos(pan) * unrolled_x + math.sin(pan) * pan_y,
        "y": -math.sin(pan) * unrolled_x + math.cos(pan) * pan_y,
        "z": dz,
    }
    return origin, direction


def _norm(v: dict[str, float]) -> dict[str, float]:
    length = math.sqrt(sum(v[k] * v[k] for k in ("x", "y", "z")))
    return {k: v[k] / length for k in ("x", "y", "z")}


def _world_points_for_baseline(target: dict[str, Any], observation: dict[str, Any], camera: dict[str, float]) -> dict[str, dict[str, float]]:
    centre_origin, optical = _camera_ray({"x": 0.5, "y": 0.5}, camera)
    optical = _norm(optical)

    def at_distance(distance: float) -> dict[str, dict[str, float]]:
        plane_point = {axis: centre_origin[axis] + optical[axis] * distance for axis in ("x", "y", "z")}
        result: dict[str, dict[str, float]] = {}
        for marker_id, screen in (observation.get("points") or {}).items():
            origin, direction = _camera_ray(screen, camera)
            denominator = sum(direction[a] * optical[a] for a in ("x", "y", "z"))
            if abs(denominator) < 1e-9:
                continue
            travel = sum((plane_point[a] - origin[a]) * optical[a] for a in ("x", "y", "z")) / denominator
            if travel <= 0:
                continue
            result[marker_id] = {a: origin[a] + direction[a] * travel for a in ("x", "y", "z")}
        return result

    distance = float(target.get("approx_distance_m") or 10.0)
    # A measured rectangle gives us a useful metric scale estimate without forcing
    # the operator to know the exact camera-to-plane distance.
    if target.get("type") == "rectangle" and target.get("width_m") and target.get("height_m"):
        unit = at_distance(1.0)
        ratios: list[float] = []
        def separation(a: str, b: str) -> float | None:
            if a not in unit or b not in unit:
                return None
            return math.sqrt(sum((unit[a][axis] - unit[b][axis]) ** 2 for axis in ("x", "y", "z")))
        for pair in (("M1", "M2"), ("M4", "M3")):
            measured = separation(*pair)
            if measured and measured > 1e-9:
                ratios.append(float(target["width_m"]) / measured)
        for pair in (("M1", "M4"), ("M2", "M3")):
            measured = separation(*pair)
            if measured and measured > 1e-9:
                ratios.append(float(target["height_m"]) / measured)
        if ratios:
            ratios.sort()
            distance = ratios[len(ratios) // 2]
    return at_distance(max(0.05, min(5000.0, distance)))


def _project(point: dict[str, float], camera: dict[str, float]) -> dict[str, float] | None:
    pan = math.radians(float(camera.get("pan", 0.0)))
    tilt = -math.radians(float(camera.get("tilt", 0.0)))
    roll = -math.radians(float(camera.get("roll", 0.0)))
    x = point["x"] - float(camera.get("x", 0.0))
    y = point["y"] - float(camera.get("y", 0.0))
    z = point["z"] - float(camera.get("z", 1.7))
    x, y = x * math.cos(pan) - y * math.sin(pan), x * math.sin(pan) + y * math.cos(pan)
    y, z = y * math.cos(tilt) - z * math.sin(tilt), y * math.sin(tilt) + z * math.cos(tilt)
    x, z = x * math.cos(roll) - z * math.sin(roll), x * math.sin(roll) + z * math.cos(roll)
    if y <= 0.05:
        return None
    fov = max(5.0, min(160.0, float(camera.get("fov", 60.0)))) * math.pi / 180.0
    focal = REFERENCE_WIDTH / (2.0 * math.tan(fov / 2.0))
    return {
        "x": (REFERENCE_WIDTH / 2.0 + x / y * focal) / REFERENCE_WIDTH,
        "y": (REFERENCE_HEIGHT / 2.0 - z / y * focal) / REFERENCE_HEIGHT,
    }


def _coverage_counts(dot_calibration: dict[str, Any]) -> tuple[dict[str, int], dict[str, list[int]]]:
    counts: dict[str, list[int]] = {axis: [] for axis in ("pan", "tilt", "zoom")}
    for target in dot_calibration.get("targets") or []:
        for observation in target.get("observations") or []:
            axes = observation.get("axes") or {}
            for axis in ("pan", "tilt"):
                value = (axes.get(axis) or {}).get("raw_count")
                if value is not None:
                    try:
                        counts[axis].append(int(value))
                    except (TypeError, ValueError):
                        pass
            value = (axes.get("zoom") or {}).get("relative_count")
            if value is not None:
                try:
                    counts["zoom"].append(int(value))
                except (TypeError, ValueError):
                    pass
    coverage = {axis: (max(values) - min(values) if values else 0) for axis, values in counts.items()}
    return coverage, counts


def _base_parameters(profile: dict[str, Any], zoom_mapping: dict[str, Any]) -> dict[str, float]:
    profile_fov = float(profile.get("camera", {}).get("fov", 60.0))
    wide = zoom_mapping.get("wide") or {}
    tight = zoom_mapping.get("tight") or {}
    return {
        "pan_ratio": float(profile["axes"]["pan"]["mapping"]["encoder_revs_per_camera_rev"]),
        "tilt_ratio": float(profile["axes"]["tilt"]["mapping"]["encoder_revs_per_camera_rev"]),
        "horizontal_fov": profile_fov,
        "wide_fov": float(wide.get("fov", profile_fov)),
        "tight_fov": float(tight.get("fov", max(5.0, min(profile_fov, 10.0)))),
        "zoom_curvature": float(zoom_mapping.get("curvature", 0.0)),
    }


def _project_direction(direction: dict[str, float], camera: dict[str, float]) -> dict[str, float] | None:
    """Project a world-space bearing through a camera without assuming target depth."""
    origin = {axis: float(camera.get(axis, 0.0 if axis != "z" else 1.7)) for axis in ("x", "y", "z")}
    point = {axis: origin[axis] + direction[axis] * 1000.0 for axis in ("x", "y", "z")}
    return _project(point, camera)


def _mean_direction(directions: list[dict[str, float]]) -> dict[str, float] | None:
    if not directions:
        return None
    vector = {axis: sum(direction[axis] for direction in directions) for axis in ("x", "y", "z")}
    length = math.sqrt(sum(vector[axis] * vector[axis] for axis in ("x", "y", "z")))
    if length <= 1e-9:
        return None
    return {axis: vector[axis] / length for axis in ("x", "y", "z")}


def _model_points_from_bearings(
    target: dict[str, Any],
    marker_directions: dict[str, dict[str, float]],
    reference_camera: dict[str, float],
) -> dict[str, dict[str, float]]:
    """Give solved bearings a finite depth for overlay rendering.

    Approximate distance is a scale hint only. Once repeated observations exist,
    marker bearings are solved from all image measurements equally. A measured
    rectangle can replace the distance hint with metric scale.
    """
    if not marker_directions:
        return {}
    origin = {axis: float(reference_camera.get(axis, 0.0 if axis != "z" else 1.7)) for axis in ("x", "y", "z")}

    def points_at(distance: float) -> dict[str, dict[str, float]]:
        return {
            marker_id: {axis: origin[axis] + direction[axis] * distance for axis in ("x", "y", "z")}
            for marker_id, direction in marker_directions.items()
        }

    distance = float(target.get("approx_distance_m") or 10.0)
    if target.get("type") == "rectangle" and (target.get("width_m") or target.get("height_m")):
        unit = points_at(1.0)
        ratios: list[float] = []

        def separation(a: str, b: str) -> float | None:
            if a not in unit or b not in unit:
                return None
            return math.sqrt(sum((unit[a][axis] - unit[b][axis]) ** 2 for axis in ("x", "y", "z")))

        if target.get("width_m"):
            for pair in (("M1", "M2"), ("M4", "M3")):
                measured = separation(*pair)
                if measured and measured > 1e-9:
                    ratios.append(float(target["width_m"]) / measured)
        if target.get("height_m"):
            for pair in (("M1", "M4"), ("M2", "M3")):
                measured = separation(*pair)
                if measured and measured > 1e-9:
                    ratios.append(float(target["height_m"]) / measured)
        if ratios:
            ratios.sort()
            distance = ratios[len(ratios) // 2]
    return points_at(max(0.05, min(5000.0, distance)))


def _camera_for_parameters(
    observation: dict[str, Any], profile: dict[str, Any], zoom_mapping: dict[str, Any],
    params: dict[str, float], use_profile_fov: bool,
) -> dict[str, float]:
    """Build the candidate camera for one saved observation.

    When Zoom is not fully calibrated, ``horizontal_fov`` is the candidate lens
    FOV and is solved jointly with encoder gearing. Once both Zoom endpoints are
    available, the physical Zoom mapping owns FOV and only its curvature may be
    refined by repeated observations.
    """
    horizontal_fov = params["horizontal_fov"] if use_profile_fov else None
    effective_zoom = zoom_mapping
    if not use_profile_fov and (zoom_mapping.get("wide") or zoom_mapping.get("tight")):
        effective_zoom = deepcopy(zoom_mapping)
        if effective_zoom.get("wide"):
            effective_zoom["wide"]["fov"] = float(params.get("wide_fov", effective_zoom["wide"]["fov"]))
        if effective_zoom.get("tight"):
            effective_zoom["tight"]["fov"] = float(params.get("tight_fov", effective_zoom["tight"]["fov"]))
    return _camera_for_observation(
        observation,
        profile,
        params["pan_ratio"],
        params["tilt_ratio"],
        effective_zoom,
        params["zoom_curvature"],
        horizontal_fov=horizontal_fov,
    )


def _wrap_degrees(value: float) -> float:
    return (float(value) + 180.0) % 360.0 - 180.0


def _direction_angles(direction: dict[str, float]) -> tuple[float, float]:
    horizontal = math.hypot(direction["x"], direction["y"])
    return (
        math.degrees(math.atan2(direction["x"], direction["y"])),
        math.degrees(math.atan2(direction["z"], horizontal)),
    )


def _residuals_for_parameters(
    params: dict[str, float], profile: dict[str, Any], zoom_mapping: dict[str, Any],
    dot_calibration: dict[str, Any], *, axes: str = "both", include_models: bool = False,
    use_profile_fov: bool = True, metric: str = "bearing",
):
    """Return symmetric residuals for repeated marker observations.

    ``metric='bearing'`` is used for optimisation. Every dragged image point is
    converted into a world-space ray using the candidate camera model. All rays
    for the same physical marker must agree on one common azimuth/elevation.
    This is unbiased for pure camera rotation and cleanly separates Pan/FOV
    fitting from absolute target depth.

    ``metric='pixels'`` is used only for operator-facing RMS diagnostics and
    target-model generation. It reprojects the common ray into each view.
    """
    if axes not in {"x", "y", "both"}:
        raise DotCalibrationError("Dot calibration residual axis must be x, y or both")
    if metric not in {"bearing", "pixels"}:
        raise DotCalibrationError("Dot calibration residual metric must be bearing or pixels")
    result: list[float] = []
    models: dict[str, dict[str, dict[str, float]]] = {}
    used_points = 0
    used_observation_ids: set[str] = set()

    for target in dot_calibration.get("targets") or []:
        observations = target.get("observations") or []
        if len(observations) < 2:
            continue

        tracks: dict[str, list[tuple[dict[str, Any], dict[str, float], dict[str, float], dict[str, float]]]] = {}
        reference_camera: dict[str, float] | None = None
        for observation in observations:
            points = observation.get("points") or {}
            if not points:
                continue
            try:
                camera = _camera_for_parameters(observation, profile, zoom_mapping, params, use_profile_fov)
            except DotCalibrationError:
                continue
            if reference_camera is None:
                reference_camera = camera
            for marker_id, observed in points.items():
                try:
                    _, direction = _camera_ray(observed, camera)
                    direction = _norm(direction)
                except (ValueError, ZeroDivisionError):
                    continue
                tracks.setdefault(str(marker_id), []).append((observation, camera, observed, direction))

        marker_directions: dict[str, dict[str, float]] = {}
        for marker_id, entries in tracks.items():
            if len(entries) < 2:
                continue
            common = _mean_direction([entry[3] for entry in entries])
            if common is None:
                continue
            marker_directions[marker_id] = common
            common_pan, common_tilt = _direction_angles(common)
            for observation, camera, observed, direction in entries:
                if metric == "bearing":
                    ray_pan, ray_tilt = _direction_angles(direction)
                    if axes in {"x", "both"}:
                        result.append(_wrap_degrees(ray_pan - common_pan))
                    if axes in {"y", "both"}:
                        result.append(ray_tilt - common_tilt)
                else:
                    projected = _project_direction(common, camera)
                    if not projected:
                        continue
                    if axes in {"x", "both"}:
                        result.append((projected["x"] - float(observed["x"])) * REFERENCE_WIDTH)
                    if axes in {"y", "both"}:
                        result.append((projected["y"] - float(observed["y"])) * REFERENCE_HEIGHT)
                used_points += 1
                used_observation_ids.add(str(observation.get("id") or id(observation)))

        if include_models and reference_camera is not None and marker_directions:
            models[target["id"]] = _model_points_from_bearings(target, marker_directions, reference_camera)

    if include_models:
        return result, models, used_points, len(used_observation_ids)
    return result

def _observation_pose_counts(observation: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    axes = observation.get("axes") or {}
    def value(axis: str, key: str) -> int | None:
        raw = (axes.get(axis) or {}).get(key)
        try:
            return None if raw is None else int(raw)
        except (TypeError, ValueError):
            return None
    return value("pan", "raw_count"), value("tilt", "raw_count"), value("zoom", "relative_count")


def _deduplicated_dot_calibration(dot_calibration: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Collapse near-identical camera poses so repeated clicks do not double-weight a solve.

    Multiple saves at essentially the same Pan/Tilt/Zoom pose are averaged in
    image space and retained as one representative observation for fitting.
    The original saved history is never modified.
    """
    result = deepcopy(dot_calibration)
    duplicate_count = 0
    for target in result.get("targets") or []:
        observations = target.get("observations") or []
        clusters: list[list[dict[str, Any]]] = []
        for observation in observations:
            pan, tilt, zoom = _observation_pose_counts(observation)
            placed = set((observation.get("points") or {}).keys())
            matched = None
            for cluster in clusters:
                ref = cluster[0]
                rpan, rtilt, rzoom = _observation_pose_counts(ref)
                rplaced = set((ref.get("points") or {}).keys())
                if placed != rplaced:
                    continue
                if pan is None or tilt is None or rpan is None or rtilt is None:
                    continue
                zoom_ok = (zoom is None and rzoom is None) or (
                    zoom is not None and rzoom is not None and abs(zoom - rzoom) <= 5
                )
                if abs(pan - rpan) <= 2 and abs(tilt - rtilt) <= 2 and zoom_ok:
                    matched = cluster
                    break
            if matched is None:
                clusters.append([observation])
            else:
                matched.append(observation)
                duplicate_count += 1

        collapsed = []
        for cluster in clusters:
            if len(cluster) == 1:
                collapsed.append(cluster[0])
                continue
            representative = deepcopy(cluster[-1])
            common_markers = set.intersection(*(set((item.get("points") or {}).keys()) for item in cluster))
            averaged_points = {}
            for marker_id in common_markers:
                averaged_points[marker_id] = {
                    "x": sum(float(item["points"][marker_id]["x"]) for item in cluster) / len(cluster),
                    "y": sum(float(item["points"][marker_id]["y"]) for item in cluster) / len(cluster),
                }
            representative["points"] = averaged_points
            collapsed.append(representative)
        target["observations"] = collapsed
    return result, duplicate_count


def _subset_dot_calibration(dot_calibration: dict[str, Any], observation_ids: set[str]) -> dict[str, Any]:
    result = deepcopy(dot_calibration)
    for target in result.get("targets") or []:
        target["observations"] = [
            observation for observation in target.get("observations") or []
            if str(observation.get("id")) in observation_ids
        ]
    return result


def _best_pan_fov_group(dot_calibration: dict[str, Any]) -> dict[str, Any] | None:
    """Find the strongest near-fixed-Tilt, near-fixed-Zoom Pan sweep in a mixed session."""
    candidates: list[dict[str, Any]] = []
    observations = [
        observation
        for target in dot_calibration.get("targets") or []
        for observation in target.get("observations") or []
        if (observation.get("points") or {})
    ]
    for anchor in observations:
        apan, atilt, azoom = _observation_pose_counts(anchor)
        if apan is None or atilt is None:
            continue
        group = []
        for observation in observations:
            pan, tilt, zoom = _observation_pose_counts(observation)
            if pan is None or tilt is None:
                continue
            zoom_ok = (azoom is None and zoom is None) or (
                azoom is not None and zoom is not None and abs(zoom - azoom) <= 20
            )
            if abs(tilt - atilt) <= 12 and zoom_ok:
                group.append(observation)
        if len(group) < 2:
            continue
        pans = [int((obs["axes"]["pan"])["raw_count"]) for obs in group]
        coverage = max(pans) - min(pans)
        if coverage < 80:
            continue
        placements = sum(len(obs.get("points") or {}) for obs in group)
        zooms = [z for obs in group if (z := _observation_pose_counts(obs)[2]) is not None]
        candidates.append({
            "ids": {str(obs.get("id")) for obs in group},
            "coverage": coverage,
            "placements": placements,
            "tilt_center": round(sum(int(obs["axes"]["tilt"]["raw_count"]) for obs in group) / len(group)),
            "zoom_count": round(sum(zooms) / len(zooms)) if zooms else None,
        })
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item["coverage"] * max(1, item["placements"]), item["coverage"], item["placements"]))


def _zoom_endpoint_for_count(zoom_count: int | None, zoom_mapping: dict[str, Any]) -> str | None:
    if zoom_count is None:
        return None
    points = []
    for name in ("wide", "tight"):
        point = zoom_mapping.get(name)
        if point and point.get("relative_count") is not None:
            points.append((name, int(point["relative_count"])))
    if len(points) != 2:
        return points[0][0] if len(points) == 1 and abs(zoom_count - points[0][1]) <= 60 else None
    points.sort(key=lambda item: item[1])
    low, high = points
    if zoom_count <= low[1] or abs(zoom_count - low[1]) <= max(60, int(abs(high[1] - low[1]) * 0.12)):
        return low[0]
    if zoom_count >= high[1] or abs(zoom_count - high[1]) <= max(60, int(abs(high[1] - low[1]) * 0.12)):
        return high[0]
    return None


def _rectangle_world_points(target: dict[str, Any]) -> dict[str, dict[str, float]]:
    if target.get("type") != "rectangle":
        raise DotCalibrationError("World alignment requires a measured rectangle target")
    width = target.get("width_m")
    height = target.get("height_m")
    if width is None or height is None:
        raise DotCalibrationError("Enter the measured rectangle width and height before solving the venue/world")
    width, height = float(width), float(height)
    if width <= 0 or height <= 0:
        raise DotCalibrationError("Measured rectangle width and height must be greater than zero")
    bottom = float(target.get("bottom_height_m", 0.0))
    points = {
        "M1": {"x": -width / 2.0, "y": 0.0, "z": bottom + height},
        "M2": {"x": width / 2.0, "y": 0.0, "z": bottom + height},
        "M3": {"x": width / 2.0, "y": 0.0, "z": bottom},
        "M4": {"x": -width / 2.0, "y": 0.0, "z": bottom},
    }
    if target.get("edge_midpoints"):
        points.update({
            "M5": {"x": 0.0, "y": 0.0, "z": bottom + height},
            "M6": {"x": width / 2.0, "y": 0.0, "z": bottom + height / 2.0},
            "M7": {"x": 0.0, "y": 0.0, "z": bottom},
            "M8": {"x": -width / 2.0, "y": 0.0, "z": bottom + height / 2.0},
        })
    return points


def solve_dot_world_reference(
    profile: dict[str, Any],
    dot_calibration: dict[str, Any],
    zoom_mapping: dict[str, Any],
    target_id: str | None = None,
) -> dict[str, Any]:
    """Solve a metric camera/world frame from a measured vertical rectangle.

    The rectangle defines the local venue frame: its centre is X=0/Y=0, its
    plane is vertical, and ``bottom_height_m`` defines its lower edge above
    ground Z=0. The measured dimensions provide metric scale.
    """
    dot_calibration = normalise_dot_calibration(dot_calibration)
    target = next(
        (item for item in dot_calibration.get("targets") or [] if item.get("id") == (target_id or dot_calibration.get("active_target_id"))),
        None,
    )
    if target is None:
        raise DotCalibrationError("Select a Dot calibration target first")
    world_points = _rectangle_world_points(target)
    deduped, duplicates = _deduplicated_dot_calibration({"targets": [target], "active_target_id": target["id"], "last_solution": None})
    observations = deduped["targets"][0].get("observations") or []
    usable = []
    for observation in observations:
        points = {
            marker_id: observed
            for marker_id, observed in (observation.get("points") or {}).items()
            if marker_id in world_points
        }
        if len(points) >= 3:
            copy = deepcopy(observation)
            copy["points"] = points
            usable.append(copy)
    if len(usable) < 2 or sum(len(obs["points"]) for obs in usable) < 8:
        raise DotCalibrationError("World alignment needs at least two measured-rectangle observations with eight total marker placements")

    rig = normalise_rig_offset(profile.get("camera", {}).get("rig_offset"))
    first = usable[0]
    distance = max(0.25, float(target.get("approx_distance_m") or 10.0))
    height = float(profile.get("camera", {}).get("height", 1.8))
    target_center_z = float(target.get("bottom_height_m", 0.0)) + float(target["height_m"]) / 2.0
    pan_ratio = float(profile["axes"]["pan"]["mapping"]["encoder_revs_per_camera_rev"])
    tilt_ratio = float(profile["axes"]["tilt"]["mapping"]["encoder_revs_per_camera_rev"])
    first_pan = _axis_angle(first, "pan", profile["axes"]["pan"], pan_ratio)
    first_tilt = _axis_angle(first, "tilt", profile["axes"]["tilt"], tilt_ratio)
    initial_world_tilt = math.degrees(math.atan2(target_center_z - height, distance))
    initial = [0.0, -distance, height, -first_pan, initial_world_tilt - first_tilt]

    def camera_for(observation: dict[str, Any], values: list[float]) -> dict[str, float]:
        pivot = {"x": values[0], "y": values[1], "z": values[2]}
        tracking_pan = _axis_angle(observation, "pan", profile["axes"]["pan"], pan_ratio)
        tracking_tilt = _axis_angle(observation, "tilt", profile["axes"]["tilt"], tilt_ratio)
        world_pan = tracking_pan + values[3]
        world_tilt = tracking_tilt + values[4]
        optical = camera_position(pivot, world_pan, world_tilt, rig)
        zoom_axis = (observation.get("axes") or {}).get("zoom") or {}
        fov = zoom_fov_for_count(zoom_axis.get("relative_count"), zoom_mapping)
        if fov is None:
            fov = float((observation.get("camera") or {}).get("fov", profile.get("camera", {}).get("fov", 60.0)))
        return {
            **optical,
            "height": optical["z"],
            "pan": world_pan,
            "tilt": world_tilt,
            "roll": float((observation.get("camera") or {}).get("roll", 0.0)),
            "fov": fov,
        }

    def residuals(values: list[float]) -> list[float]:
        result = []
        for observation in usable:
            camera = camera_for(observation, values)
            for marker_id, observed in observation["points"].items():
                predicted = _project(world_points[marker_id], camera)
                if predicted is None:
                    result.extend((5000.0, 5000.0))
                    continue
                result.extend((
                    (predicted["x"] - float(observed["x"])) * REFERENCE_WIDTH,
                    (predicted["y"] - float(observed["y"])) * REFERENCE_HEIGHT,
                ))
        return result

    def bounded(values: list[float]) -> list[float]:
        span = max(10.0, distance * 10.0)
        return [
            max(-span, min(span, values[0])),
            max(-span, min(-0.05, values[1])),
            max(-5.0, min(30.0, values[2])),
            _wrap_degrees(values[3]),
            max(-89.0, min(89.0, values[4])),
        ]

    params = bounded(initial)
    damping = 1e-2
    converged = False
    for _ in range(160):
        base = residuals(params)
        base_cost = _cost(base)
        jacobian = [[0.0] * 5 for _ in base]
        for column in range(5):
            step = 1e-4 * max(1.0, abs(params[column]))
            trial = params[:]
            trial[column] += step
            trial = bounded(trial)
            shifted = residuals(trial)
            actual_step = trial[column] - params[column]
            if abs(actual_step) < 1e-12:
                continue
            for row in range(len(base)):
                jacobian[row][column] = (shifted[row] - base[row]) / actual_step
        normal = [[sum(jacobian[k][i] * jacobian[k][j] for k in range(len(base))) for j in range(5)] for i in range(5)]
        gradient = [sum(jacobian[k][i] * base[k] for k in range(len(base))) for i in range(5)]
        # weak stabilising priors around the operator's distance/height hint
        priors = (1e-4, 1e-4, 2e-3, 1e-7, 1e-7)
        for index in range(5):
            normal[index][index] += damping + priors[index]
            gradient[index] += priors[index] * (params[index] - initial[index])
        try:
            delta = _solve_linear(normal, [-value for value in gradient])
        except DotCalibrationError:
            damping *= 10.0
            continue
        candidate = bounded([value + change for value, change in zip(params, delta)])
        candidate_cost = _cost(residuals(candidate))
        if candidate_cost < base_cost:
            params = candidate
            damping = max(1e-9, damping / 3.0)
            if max(abs(change) for change in delta) < 1e-7:
                converged = True
                break
        else:
            damping *= 10.0

    final = residuals(params)
    rms = math.sqrt(_cost(final) / len(final))
    maximum = max(math.hypot(final[index], final[index + 1]) for index in range(0, len(final), 2))
    if not math.isfinite(rms) or rms > 30.0:
        raise DotCalibrationError(f"Measured-rectangle world solve is not reliable yet ({rms:.1f} px RMS). Refine the markers or add more views.")
    solution = {
        "solved": True,
        "pivot": {"x": params[0], "y": params[1], "z": params[2]},
        "camera": {"x": params[0], "y": params[1], "z": params[2]},
        "pan_offset": _wrap_degrees(params[3]),
        "tilt_offset": params[4],
        "rig_offset": rig,
        "fixed_roll": 0.0,
        "horizontal_fov": float((zoom_mapping.get("wide") or {}).get("fov", profile.get("camera", {}).get("fov", 60.0))),
        "observation_count": sum(len(obs["points"]) for obs in usable),
        "view_count": len(usable),
        "rms_pixel_error": rms,
        "max_pixel_error": maximum,
        "target_source": target["id"],
        "target_name": target["name"],
        "deduplicated_observations": duplicates,
        "converged": converged,
        "solved_at": datetime.now(timezone.utc).isoformat(),
    }
    world_target = {
        "center": {
            "x": 0.0,
            "y": 0.0,
            "z": float(target.get("bottom_height_m", 0.0)) + float(target["height_m"]) / 2.0,
        },
        "width": float(target["width_m"]),
        "height": float(target["height_m"]),
        "yaw": 0.0,
        "plane": "vertical_rectangle",
    }
    return {"solution": solution, "target": world_target}

def _cost(residuals: list[float]) -> float:
    return sum(value * value for value in residuals)


def _solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [matrix[row][:] + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise DotCalibrationError("Dot calibration geometry is degenerate")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [a - factor * b for a, b in zip(augmented[row], augmented[column])]
    return [augmented[row][-1] for row in range(size)]


def solve_dot_calibration(profile: dict[str, Any], dot_calibration: dict[str, Any], zoom_mapping: dict[str, Any]) -> dict[str, Any]:
    dot_calibration = normalise_dot_calibration(dot_calibration)
    solve_data, duplicate_count = _deduplicated_dot_calibration(dot_calibration)
    coverage, counts = _coverage_counts(solve_data)
    base_params = _base_parameters(profile, zoom_mapping)
    params = dict(base_params)

    wide, tight = zoom_mapping.get("wide"), zoom_mapping.get("tight")
    zoom_complete = bool(
        wide and tight
        and wide.get("reference_id")
        and wide.get("reference_id") == tight.get("reference_id")
        and int(wide.get("relative_count")) != int(tight.get("relative_count"))
    )
    use_profile_fov = not zoom_complete

    initial_bearing = _residuals_for_parameters(
        base_params, profile, zoom_mapping, solve_data,
        axes="both", use_profile_fov=use_profile_fov, metric="bearing",
    )
    if len(initial_bearing) < 8:
        raise DotCalibrationError("Need more camera movement: at least four repeated marker placements across two or more views")
    initial_full = _residuals_for_parameters(
        base_params, profile, zoom_mapping, solve_data,
        axes="both", use_profile_fov=use_profile_fov, metric="pixels",
    )

    parameters: list[str] = []
    converged_all = True

    def bounded(candidate: dict[str, float], stage_names: list[str]) -> dict[str, float]:
        result = dict(candidate)
        for name in stage_names:
            if name == "pan_ratio":
                base = base_params["pan_ratio"]
                result[name] = max(base * 0.25, min(base * 4.0, float(result[name])))
            elif name == "tilt_ratio":
                base = base_params["tilt_ratio"]
                result[name] = max(base * 0.25, min(base * 4.0, float(result[name])))
            elif name in {"horizontal_fov", "wide_fov", "tight_fov"}:
                result[name] = max(5.0, min(160.0, float(result[name])))
            elif name == "zoom_curvature":
                result[name] = max(-1.0, min(4.0, float(result[name])))
        if "wide_fov" in result and "tight_fov" in result:
            if result["wide_fov"] <= result["tight_fov"] + 0.1:
                if "wide_fov" in stage_names:
                    result["wide_fov"] = min(160.0, result["tight_fov"] + 0.1)
                elif "tight_fov" in stage_names:
                    result["tight_fov"] = max(5.0, result["wide_fov"] - 0.1)
        return result

    def optimise(
        stage_names: list[str],
        axes: str,
        *,
        calibration_data: dict[str, Any] | None = None,
    ) -> tuple[dict[str, float], bool]:
        if not stage_names:
            return dict(params), True
        data = calibration_data or solve_data
        start_params = dict(params)
        stage_initial = {name: start_params[name] for name in stage_names}
        initial_residual = _residuals_for_parameters(
            start_params, profile, zoom_mapping, data,
            axes=axes, use_profile_fov=use_profile_fov,
        )
        if len(initial_residual) < 4:
            raise DotCalibrationError("Need more repeated marker placements for this calibration axis")
        current = dict(start_params)
        damping = 1e-2
        converged = False

        def objective(values: dict[str, float]) -> tuple[float, list[float]]:
            residual = _residuals_for_parameters(
                values, profile, zoom_mapping, data,
                axes=axes, use_profile_fov=use_profile_fov,
            )
            if len(residual) != len(initial_residual):
                return float("inf"), residual
            cost = _cost(residual)
            for name in stage_names:
                value, initial = values[name], stage_initial[name]
                if name.endswith("ratio"):
                    scale = max(abs(initial) * 0.25, 0.05)
                    cost += 1e-8 * ((value - initial) / scale) ** 2
                elif name.endswith("_fov") or name == "horizontal_fov":
                    cost += 1e-8 * ((value - initial) / 25.0) ** 2
                else:
                    cost += 1e-7 * ((value - initial) / 2.0) ** 2
            return cost, residual

        if len(stage_names) == 1:
            name = stage_names[0]
            if name == "pan_ratio":
                lo, hi = base_params["pan_ratio"] * 0.25, base_params["pan_ratio"] * 4.0
            elif name == "tilt_ratio":
                lo, hi = base_params["tilt_ratio"] * 0.25, base_params["tilt_ratio"] * 4.0
            elif name in {"horizontal_fov", "wide_fov", "tight_fov"}:
                lo, hi = 5.0, 160.0
            else:
                lo, hi = -1.0, 4.0

            samples = 81
            grid = [lo + (hi - lo) * index / (samples - 1) for index in range(samples)]
            costs = []
            for value in grid:
                trial = dict(current)
                trial[name] = value
                trial = bounded(trial, stage_names)
                costs.append(objective(trial)[0])
            best_index = min(range(samples), key=lambda index: costs[index])
            left = grid[max(0, best_index - 1)]
            right = grid[min(samples - 1, best_index + 1)]
            if right <= left:
                result = dict(current)
                result[name] = grid[best_index]
                return bounded(result, stage_names), True

            phi = (math.sqrt(5.0) - 1.0) / 2.0
            x1 = right - phi * (right - left)
            x2 = left + phi * (right - left)

            def scalar_cost(value: float) -> float:
                trial = dict(current)
                trial[name] = value
                trial = bounded(trial, stage_names)
                return objective(trial)[0]

            f1, f2 = scalar_cost(x1), scalar_cost(x2)
            for _ in range(100):
                if f1 <= f2:
                    right, x2, f2 = x2, x1, f1
                    x1 = right - phi * (right - left)
                    f1 = scalar_cost(x1)
                else:
                    left, x1, f1 = x1, x2, f2
                    x2 = left + phi * (right - left)
                    f2 = scalar_cost(x2)
                if abs(right - left) < 1e-11 * max(1.0, abs((right + left) / 2.0)):
                    break
            result = dict(current)
            result[name] = (left + right) / 2.0
            return bounded(result, stage_names), True

        for _ in range(140):
            base_cost, residual = objective(current)
            if not math.isfinite(base_cost):
                raise DotCalibrationError("Dot calibration objective is invalid")
            jacobian = [[0.0] * len(stage_names) for _ in residual]
            for column, name in enumerate(stage_names):
                if name.endswith("ratio"):
                    step = 1e-5 * max(1.0, abs(current[name]))
                elif name.endswith("_fov") or name == "horizontal_fov":
                    step = 1e-3
                else:
                    step = 1e-4
                trial = dict(current)
                trial[name] += step
                trial = bounded(trial, stage_names)
                _, shifted = objective(trial)
                if len(shifted) != len(residual):
                    continue
                actual_step = trial[name] - current[name]
                if abs(actual_step) < 1e-12:
                    continue
                for row in range(len(residual)):
                    jacobian[row][column] = (shifted[row] - residual[row]) / actual_step
            normal = [
                [sum(jacobian[k][i] * jacobian[k][j] for k in range(len(residual))) for j in range(len(stage_names))]
                for i in range(len(stage_names))
            ]
            gradient = [sum(jacobian[k][i] * residual[k] for k in range(len(residual))) for i in range(len(stage_names))]
            for index in range(len(stage_names)):
                normal[index][index] += damping
            try:
                delta = _solve_linear(normal, [-value for value in gradient])
            except DotCalibrationError:
                damping *= 10.0
                continue
            candidate = dict(current)
            for name, change in zip(stage_names, delta):
                candidate[name] += change
            candidate = bounded(candidate, stage_names)
            candidate_cost, _ = objective(candidate)
            if candidate_cost < base_cost:
                current = candidate
                damping = max(1e-9, damping / 3.0)
                if max(abs(change) for change in delta) < 1e-7:
                    converged = True
                    break
            else:
                damping *= 10.0
        return current, converged

    pan_group = _best_pan_fov_group(solve_data) if coverage["pan"] >= 8 else None
    pan_stage: list[str] = []
    pan_stage_data = solve_data
    fov_group = None
    endpoint_anchor = None
    if coverage["pan"] >= 8:
        pan_stage = ["pan_ratio"]
        if pan_group:
            group_data = _subset_dot_calibration(solve_data, pan_group["ids"])
            if use_profile_fov:
                pan_stage.append("horizontal_fov")
                pan_stage_data = group_data
                fov_group = pan_group
            else:
                # Once both physical Zoom endpoints exist, keep those endpoint
                # FOVs authoritative and use the full deduplicated session for
                # Pan ratio. The clean Pan group is still reported as evidence,
                # but does not override the globally better Pan fit.
                fov_group = pan_group

    tilt_stage = ["tilt_ratio"] if coverage["tilt"] >= 8 else []
    for name in pan_stage + tilt_stage:
        if name not in parameters:
            parameters.append(name)

    cycles = 3 if pan_stage and tilt_stage else 1
    for _ in range(cycles):
        if pan_stage:
            params, converged = optimise(pan_stage, "x", calibration_data=pan_stage_data)
            converged_all = converged_all and converged
        if tilt_stage:
            params, converged = optimise(tilt_stage, "y")
            converged_all = converged_all and converged

    zoom_saturated = False
    zoom_levels: list[int] = []
    for count in sorted(counts["zoom"]):
        if not zoom_levels or abs(count - zoom_levels[-1]) > 25:
            zoom_levels.append(count)
    if zoom_complete and coverage["zoom"] >= 10 and len(zoom_levels) >= 3:
        before_zoom = params["zoom_curvature"]
        candidate, converged = optimise(["zoom_curvature"], "both")
        zoom_value = candidate["zoom_curvature"]
        zoom_saturated = zoom_value <= -0.9995 or zoom_value >= 3.9995
        if not zoom_saturated:
            params = candidate
            converged_all = converged_all and converged
            parameters.append("zoom_curvature")
        else:
            params["zoom_curvature"] = before_zoom

    if not parameters:
        raise DotCalibrationError("Need more camera movement before Dot calibration can refine the model")

    final_full, models, used_points, used_observations = _residuals_for_parameters(
        params, profile, zoom_mapping, solve_data,
        axes="both", include_models=True, use_profile_fov=use_profile_fov, metric="pixels",
    )
    if not final_full:
        raise DotCalibrationError("Dot calibration has no usable repeated marker correspondences")

    initial_x = _residuals_for_parameters(base_params, profile, zoom_mapping, solve_data, axes="x", use_profile_fov=use_profile_fov, metric="pixels")
    final_x = _residuals_for_parameters(params, profile, zoom_mapping, solve_data, axes="x", use_profile_fov=use_profile_fov, metric="pixels")
    initial_y = _residuals_for_parameters(base_params, profile, zoom_mapping, solve_data, axes="y", use_profile_fov=use_profile_fov, metric="pixels")
    final_y = _residuals_for_parameters(params, profile, zoom_mapping, solve_data, axes="y", use_profile_fov=use_profile_fov, metric="pixels")

    def rms(values: list[float]) -> float | None:
        return math.sqrt(_cost(values) / len(values)) if values else None

    initial_rms = rms(initial_full) or 0.0
    final_rms = rms(final_full) or 0.0

    fov_anchor = None
    if "horizontal_fov" in parameters and counts["zoom"]:
        sorted_zoom = sorted(counts["zoom"])
        median_zoom = sorted_zoom[len(sorted_zoom) // 2]
        existing = [(name, zoom_mapping.get(name)) for name in ("wide", "tight") if zoom_mapping.get(name)]
        if len(existing) == 1:
            point_name, point = existing[0]
            endpoint_count = int(point["relative_count"])
            if abs(median_zoom - endpoint_count) <= 50:
                fov_anchor = {"point": point_name, "observation_count": median_zoom, "endpoint_count": endpoint_count}
    if endpoint_anchor and fov_group:
        point = zoom_mapping.get(endpoint_anchor) or {}
        fov_anchor = {
            "point": endpoint_anchor,
            "observation_count": fov_group.get("zoom_count"),
            "endpoint_count": point.get("relative_count"),
            "group_observation_ids": sorted(fov_group["ids"]),
        }

    warnings: list[str] = []
    if duplicate_count:
        warnings.append(f"{duplicate_count} near-duplicate observation(s) were averaged so repeated saves at the same pose did not receive extra weight.")
    if fov_group:
        action = "Pan/FOV fitting used" if use_profile_fov else "A clean Pan sweep was identified from"
        warnings.append(
            f"{action} {len(fov_group['ids'])} near-constant Tilt/Zoom view(s) spanning {fov_group['coverage']} Pan counts."
        )
    elif coverage["pan"] >= 8 and zoom_complete:
        warnings.append("No clean fixed-Tilt/fixed-Zoom Pan sweep was found, so the lens endpoint FOV was held. Add two or more Pan-separated views without changing Tilt or Zoom.")
    if zoom_complete and coverage["zoom"] >= 10 and len(zoom_levels) < 3:
        warnings.append("Zoom curve was held because fewer than three distinct Zoom positions were observed. Add at least one intermediate Zoom position before fitting lens curvature.")
    if zoom_saturated:
        warnings.append("Zoom fit hit the limit of the current one-curve lens model. Zoom correction was excluded from this proposal.")
    if final_rms > 12.0:
        warnings.append("Residual image error is still high. Review marker placement, add well-spread motion groups, or solve the measured rectangle as the venue/world reference.")

    return {
        "solved": True,
        "converged": converged_all,
        "parameters": parameters,
        "current": dict(base_params),
        "proposed": dict(params),
        "rms_before_px": initial_rms,
        "rms_after_px": final_rms,
        "rms_x_before_px": rms(initial_x),
        "rms_x_after_px": rms(final_x),
        "rms_y_before_px": rms(initial_y),
        "rms_y_after_px": rms(final_y),
        "improvement_percent": 0.0 if initial_rms <= 1e-9 else max(-999.0, min(100.0, (initial_rms - final_rms) * 100.0 / initial_rms)),
        "matched_points": used_points,
        "observation_sets": used_observations,
        "coverage": coverage,
        "target_models": models,
        "zoom_saturated": zoom_saturated,
        "zoom_levels": zoom_levels,
        "fov_anchor": fov_anchor,
        "analysis": {
            "duplicate_observations": duplicate_count,
            "pan_fov_group": None if not fov_group else {
                "observation_ids": sorted(fov_group["ids"]),
                "pan_coverage": fov_group["coverage"],
                "tilt_center": fov_group["tilt_center"],
                "zoom_count": fov_group["zoom_count"],
                "endpoint": endpoint_anchor,
            },
        },
        "warnings": warnings,
        "notes": [
            "Repeated marker placements are equal-authority image measurements; Observation 1 is not hidden 3D truth.",
            "Mixed sessions are split into useful motion groups: a fixed-Tilt/fixed-Zoom Pan sweep can refine Pan gearing and lens FOV while other views refine Tilt.",
            "Near-duplicate camera poses are averaged for solving instead of receiving repeated statistical weight.",
            "Zoom curvature is fitted only after at least three distinct Zoom positions exist.",
            "Approximate target distance supplies display scale only; measured rectangle dimensions can additionally define a metric venue/world frame.",
        ],
    }

