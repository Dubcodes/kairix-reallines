from __future__ import annotations

import math
from copy import deepcopy
from typing import Any


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
    return {
        "pan_ratio": float(profile["axes"]["pan"]["mapping"]["encoder_revs_per_camera_rev"]),
        "tilt_ratio": float(profile["axes"]["tilt"]["mapping"]["encoder_revs_per_camera_rev"]),
        "horizontal_fov": float(profile.get("camera", {}).get("fov", 60.0)),
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
    return _camera_for_observation(
        observation,
        profile,
        params["pan_ratio"],
        params["tilt_ratio"],
        zoom_mapping,
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
    coverage, counts = _coverage_counts(dot_calibration)
    base_params = _base_parameters(profile, zoom_mapping)
    params = dict(base_params)

    wide, tight = zoom_mapping.get("wide"), zoom_mapping.get("tight")
    zoom_complete = bool(
        wide and tight
        and wide.get("reference_id")
        and wide.get("reference_id") == tight.get("reference_id")
        and int(wide.get("relative_count")) != int(tight.get("relative_count"))
    )
    # With an incomplete Zoom mapping the profile FOV is the only lens model
    # available. Repeated fixed-Zoom observations can therefore solve that FOV
    # jointly with Pan/Tilt gearing. Once both Zoom endpoints exist, their
    # physical mapping owns FOV and the dot solver only refines curve shape.
    use_profile_fov = not zoom_complete

    initial_bearing = _residuals_for_parameters(
        base_params, profile, zoom_mapping, dot_calibration,
        axes="both", use_profile_fov=use_profile_fov, metric="bearing",
    )
    if len(initial_bearing) < 8:
        raise DotCalibrationError("Need more camera movement: at least four repeated marker placements across two or more views")
    initial_full = _residuals_for_parameters(
        base_params, profile, zoom_mapping, dot_calibration,
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
            elif name == "horizontal_fov":
                result[name] = max(5.0, min(160.0, float(result[name])))
            elif name == "zoom_curvature":
                result[name] = max(-1.0, min(4.0, float(result[name])))
        return result

    def optimise(stage_names: list[str], axes: str) -> tuple[dict[str, float], bool]:
        if not stage_names:
            return dict(params), True
        start_params = dict(params)
        stage_initial = {name: start_params[name] for name in stage_names}
        initial_residual = _residuals_for_parameters(
            start_params, profile, zoom_mapping, dot_calibration,
            axes=axes, use_profile_fov=use_profile_fov,
        )
        if len(initial_residual) < 4:
            raise DotCalibrationError("Need more repeated marker placements for this calibration axis")
        current = dict(start_params)
        damping = 1e-2
        converged = False

        def objective(values: dict[str, float]) -> tuple[float, list[float]]:
            residual = _residuals_for_parameters(
                values, profile, zoom_mapping, dot_calibration,
                axes=axes, use_profile_fov=use_profile_fov,
            )
            if len(residual) != len(initial_residual):
                return float("inf"), residual
            cost = _cost(residual)
            # These are guard rails, not competing measurements. The dragged
            # marker correspondences should dominate the fitted camera model.
            for name in stage_names:
                value, initial = values[name], stage_initial[name]
                if name.endswith("ratio"):
                    scale = max(abs(initial) * 0.25, 0.05)
                    cost += 1e-8 * ((value - initial) / scale) ** 2
                elif name == "horizontal_fov":
                    cost += 1e-8 * ((value - initial) / 25.0) ** 2
                else:
                    cost += 1e-7 * ((value - initial) / 2.0) ** 2
            return cost, residual

        # One-dimensional stages are common and can be solved much more
        # deterministically than by repeated damped Newton steps. A coarse
        # scan followed by golden-section refinement prevents tiny solve/apply
        # cycles from walking the result.
        if len(stage_names) == 1:
            name = stage_names[0]
            if name == "pan_ratio":
                lo, hi = base_params["pan_ratio"] * 0.25, base_params["pan_ratio"] * 4.0
            elif name == "tilt_ratio":
                lo, hi = base_params["tilt_ratio"] * 0.25, base_params["tilt_ratio"] * 4.0
            elif name == "horizontal_fov":
                lo, hi = 5.0, 160.0
            else:
                lo, hi = -1.0, 4.0

            samples = 81
            grid = [lo + (hi - lo) * index / (samples - 1) for index in range(samples)]
            costs = []
            for value in grid:
                trial = dict(current)
                trial[name] = value
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

        for _ in range(120):
            base_cost, residual = objective(current)
            if not math.isfinite(base_cost):
                raise DotCalibrationError("Dot calibration objective is invalid")
            jacobian = [[0.0] * len(stage_names) for _ in residual]
            for column, name in enumerate(stage_names):
                if name.endswith("ratio"):
                    step = 1e-5 * max(1.0, abs(current[name]))
                elif name == "horizontal_fov":
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

    # Pan/FOV are strongly coupled in image space. Estimate FOV only when one
    # camera axis clearly dominates the session; with substantial simultaneous
    # Pan and Tilt movement the same sparse correspondences can trade FOV against
    # both gear ratios and become weakly conditioned. The common start-of-day
    # workflow (Pan sweep with Tilt nearly fixed) is therefore ideal and remains
    # fully free-form rather than requiring prescribed angles.
    pan_dominant = coverage["pan"] >= 8 and coverage["tilt"] < max(8, int(coverage["pan"] * 0.15))
    tilt_dominant = coverage["tilt"] >= 8 and coverage["pan"] < max(8, int(coverage["tilt"] * 0.15))
    pan_stage: list[str] = []
    tilt_stage: list[str] = []
    if coverage["pan"] >= 8:
        pan_stage = ["pan_ratio"]
        if use_profile_fov and pan_dominant:
            pan_stage.append("horizontal_fov")
    if coverage["tilt"] >= 8:
        tilt_stage = ["tilt_ratio"]
        if not pan_stage and use_profile_fov and tilt_dominant:
            tilt_stage.append("horizontal_fov")

    for name in pan_stage + tilt_stage:
        if name not in parameters:
            parameters.append(name)

    # Pan and Tilt rays are weakly coupled through the camera rotation. When
    # both axes have useful coverage, alternate the independent horizontal and
    # vertical fits a few times so applying the result and solving again does
    # not cause the ratios to walk.
    cycles = 3 if pan_stage and tilt_stage else 1
    for _ in range(cycles):
        if pan_stage:
            params, converged = optimise(pan_stage, "x")
            converged_all = converged_all and converged
        if tilt_stage:
            params, converged = optimise(tilt_stage, "y")
            converged_all = converged_all and converged

    zoom_saturated = False
    if zoom_complete and coverage["zoom"] >= 10:
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
        params, profile, zoom_mapping, dot_calibration,
        axes="both", include_models=True, use_profile_fov=use_profile_fov, metric="pixels",
    )
    if not final_full:
        raise DotCalibrationError("Dot calibration has no usable repeated marker correspondences")

    initial_x = _residuals_for_parameters(base_params, profile, zoom_mapping, dot_calibration, axes="x", use_profile_fov=use_profile_fov, metric="pixels")
    final_x = _residuals_for_parameters(params, profile, zoom_mapping, dot_calibration, axes="x", use_profile_fov=use_profile_fov, metric="pixels")
    initial_y = _residuals_for_parameters(base_params, profile, zoom_mapping, dot_calibration, axes="y", use_profile_fov=use_profile_fov, metric="pixels")
    final_y = _residuals_for_parameters(params, profile, zoom_mapping, dot_calibration, axes="y", use_profile_fov=use_profile_fov, metric="pixels")

    def rms(values: list[float]) -> float | None:
        return math.sqrt(_cost(values) / len(values)) if values else None

    initial_rms = rms(initial_full) or 0.0
    final_rms = rms(final_full) or 0.0

    # If a single Zoom endpoint already exists and all observations were made
    # very close to that endpoint, bind the newly solved constant FOV to that
    # endpoint as well as the profile. This prevents a future second endpoint
    # capture from resurrecting the old incorrect FOV value.
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

    warnings: list[str] = []
    if use_profile_fov and "horizontal_fov" not in parameters and coverage["pan"] >= 8 and coverage["tilt"] >= 8:
        warnings.append("Horizontal FOV was held because this observation set contains substantial Pan and Tilt movement. A mostly-Pan or mostly-Tilt sweep can solve FOV cleanly without coupling it to both gear ratios.")
    if zoom_saturated:
        warnings.append("Zoom fit hit the limit of the current one-curve lens model. Zoom correction was excluded from this proposal; Pan/Tilt/FOV still use the marker observations.")
    if "horizontal_fov" in parameters and abs(params["horizontal_fov"] - base_params["horizontal_fov"]) >= 10.0:
        warnings.append("The marker data indicates the current horizontal FOV is substantially wrong. This is expected to cause large Pan drift until the proposed FOV and Pan ratio are applied together.")
    if final_rms > 12.0:
        warnings.append("Residual image error is still high. Add more well-spread observations or review marker identity/placement before trusting the refinement.")

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
        "fov_anchor": fov_anchor,
        "warnings": warnings,
        "notes": [
            "Repeated marker placements are equal-authority image measurements; Observation 1 is not hidden 3D truth.",
            "Pan encoder ratio and horizontal FOV are solved together from horizontal marker motion when Zoom is not fully calibrated.",
            "Tilt ratio is solved only when the observation set contains meaningful Tilt movement.",
            "Approximate target distance supplies display scale only; it does not drive Pan/Tilt/FOV fitting.",
            "Absolute Pan/Tilt world-zero offsets are not observable from repeated image correspondences alone and are deliberately not invented.",
            "Rig-offset, absolute venue/world pose, and lens-distortion solving remain separate refinements.",
        ],
    }

