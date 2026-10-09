from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


TARGET_POINTS = ("top_left", "top_right", "bottom_right", "bottom_left")
DEFAULT_MAX_RMS_DEGREES = 1.0


class CalibrationError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_world_calibration(name: str, profile_id: str) -> dict[str, Any]:
    now = utc_now()
    return {
        "name": name,
        "setup_profile_id": profile_id,
        "created": now,
        "modified": now,
        "target": {
            "center": {"x": 0.0, "y": 20.0, "z": 1.0},
            "width": 4.0,
            "height": 2.0,
            "yaw": 0.0,
            "plane": "vertical_rectangle",
        },
        "camera_hint": {"x": 0.0, "y": 0.0, "z": 1.8},
        "orientation_hint": {"pan_offset": 0.0, "tilt_offset": 0.0},
        "horizontal_fov": 60.0,
        "fixed_roll": 0.0,
        "current_target": TARGET_POINTS[0],
        "observations": {},
        "solution": None,
        "valid": False,
        "status": "NOT CALIBRATED",
    }


def validate_target(target: dict[str, Any]) -> None:
    if target.get("plane") != "vertical_rectangle":
        raise CalibrationError("Only vertical rectangular targets are supported")
    for key in ("width", "height"):
        value = _finite(target.get(key), f"Target {key}")
        if value <= 0:
            raise CalibrationError(f"Target {key} must be greater than zero")
    center = target.get("center") or {}
    for axis in ("x", "y", "z"):
        _finite(center.get(axis), f"Target centre {axis.upper()}")
    _finite(target.get("yaw", 0.0), "Target yaw")


def target_points(target: dict[str, Any]) -> dict[str, dict[str, float]]:
    validate_target(target)
    center = target["center"]
    width, height = float(target["width"]), float(target["height"])
    yaw = math.radians(float(target.get("yaw", 0.0)))
    right = (math.cos(yaw), math.sin(yaw))

    def point(horizontal: float, vertical: float) -> dict[str, float]:
        return {
            "x": float(center["x"]) + horizontal * width * right[0] / 2.0,
            "y": float(center["y"]) + horizontal * width * right[1] / 2.0,
            "z": float(center["z"]) + vertical * height / 2.0,
        }

    return {
        "top_left": point(-1.0, 1.0),
        "top_right": point(1.0, 1.0),
        "bottom_right": point(1.0, -1.0),
        "bottom_left": point(-1.0, -1.0),
    }


def world_angles(camera: dict[str, float], point: dict[str, float]) -> tuple[float, float]:
    dx = _finite(point.get("x"), "Point X") - _finite(camera.get("x"), "Camera X")
    dy = _finite(point.get("y"), "Point Y") - _finite(camera.get("y"), "Camera Y")
    dz = _finite(point.get("z"), "Point Z") - _finite(camera.get("z"), "Camera Z")
    horizontal = math.hypot(dx, dy)
    if horizontal < 1e-9 and abs(dz) < 1e-9:
        raise CalibrationError("Camera and target point cannot occupy the same position")
    return math.degrees(math.atan2(dx, dy)), math.degrees(math.atan2(dz, horizontal))


def tracking_angles(camera: dict[str, float], point: dict[str, float], pan_offset: float, tilt_offset: float) -> tuple[float, float]:
    pan, tilt = world_angles(camera, point)
    return wrap_degrees(pan - pan_offset), tilt - tilt_offset


def wrap_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def _finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CalibrationError(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise CalibrationError(f"{label} must be finite")
    return number


def _observed(calibration: dict[str, Any]) -> list[tuple[str, dict[str, float], float, float]]:
    points = target_points(calibration["target"])
    observations = calibration.get("observations") or {}
    missing = [name for name in TARGET_POINTS if name not in observations]
    if missing:
        raise CalibrationError(f"Missing target marks: {', '.join(missing)}")
    result = []
    for name in TARGET_POINTS:
        observation = observations[name]
        pan = _finite(observation.get("mapped_pan"), f"{name} pan")
        tilt = _finite(observation.get("mapped_tilt"), f"{name} tilt")
        result.append((name, points[name], pan, tilt))
    if len({(round(row[2], 9), round(row[3], 9)) for row in result}) < 3:
        raise CalibrationError("Calibration marks are not sufficiently distinct")
    return result


def _residuals(params: list[float], rows: list[tuple[str, dict[str, float], float, float]]) -> list[float]:
    camera = {"x": params[0], "y": params[1], "z": params[2]}
    result: list[float] = []
    for _, point, observed_pan, observed_tilt in rows:
        predicted_pan, predicted_tilt = tracking_angles(camera, point, params[3], params[4])
        result.extend((wrap_degrees(predicted_pan - observed_pan), predicted_tilt - observed_tilt))
    return result


def _cost(residuals: list[float]) -> float:
    return sum(value * value for value in residuals)


def _solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [matrix[row][:] + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise CalibrationError("Calibration geometry is degenerate")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [a - factor * b for a, b in zip(augmented[row], augmented[column])]
    return [augmented[row][-1] for row in range(size)]


def solve_world_calibration(calibration: dict[str, Any], max_rms_degrees: float = DEFAULT_MAX_RMS_DEGREES) -> dict[str, Any]:
    rows = _observed(calibration)
    hint = calibration.get("camera_hint") or {}
    orientation = calibration.get("orientation_hint") or {}
    initial = [
        _finite(hint.get("x", 0.0), "Camera hint X"),
        _finite(hint.get("y", 0.0), "Camera hint Y"),
        _finite(hint.get("z", 1.8), "Camera hint Z"),
        _finite(orientation.get("pan_offset", 0.0), "Pan offset hint"),
        _finite(orientation.get("tilt_offset", 0.0), "Tilt offset hint"),
    ]
    # Coplanar bearing-only calibration becomes ill-conditioned under small
    # angular noise, especially for height versus tilt offset. These weak
    # priors make the documented operator estimates real constraints without
    # overpowering consistent observations.
    weak_priors = (0.005, 0.005, 0.03, 0.0002, 0.0002)

    def optimise(start: list[float], prior_weights: tuple[float, ...]) -> tuple[list[float], bool]:
        params = start[:]

        def objective(values: list[float]) -> float:
            return _cost(_residuals(values, rows)) + sum(weight * (value - hint_value) ** 2 for weight, value, hint_value in zip(prior_weights, values, initial))

        damping = 1e-3
        converged = False
        for _ in range(120):
            residual = _residuals(params, rows)
            base_cost = objective(params)
            jacobian = [[0.0] * len(params) for _ in residual]
            for column in range(len(params)):
                step = 1e-5 * max(1.0, abs(params[column]))
                trial = params[:]
                trial[column] += step
                shifted = _residuals(trial, rows)
                for row in range(len(residual)):
                    jacobian[row][column] = (shifted[row] - residual[row]) / step
            normal = [[sum(jacobian[k][i] * jacobian[k][j] for k in range(len(residual))) for j in range(len(params))] for i in range(len(params))]
            gradient = [sum(jacobian[k][i] * residual[k] for k in range(len(residual))) for i in range(len(params))]
            for index in range(len(params)):
                normal[index][index] += prior_weights[index] + damping
                gradient[index] += prior_weights[index] * (params[index] - initial[index])
            try:
                delta = _solve_linear(normal, [-value for value in gradient])
            except CalibrationError:
                damping *= 10.0
                continue
            candidate = [value + change for value, change in zip(params, delta)]
            candidate_cost = objective(candidate)
            if candidate_cost < base_cost:
                params = candidate
                damping = max(1e-9, damping / 3.0)
                if max(abs(value) for value in delta) < 1e-8 or abs(base_cost - candidate_cost) < 1e-12:
                    converged = True
                    break
            else:
                damping *= 10.0
        return params, converged

    params, converged = optimise(initial, (0.0,) * len(initial))
    residual = _residuals(params, rows)
    if math.sqrt(_cost(residual) / len(residual)) > 1e-5:
        params, converged = optimise(params, weak_priors)
        residual = _residuals(params, rows)
    if not converged and _cost(residual) > 1e-8:
        raise CalibrationError("Calibration solver did not converge")
    per_point = {}
    for index, (name, _, _, _) in enumerate(rows):
        pan_error, tilt_error = residual[index * 2:index * 2 + 2]
        per_point[name] = {
            "pan_error": pan_error,
            "tilt_error": tilt_error,
            "angular_error": math.hypot(pan_error, tilt_error),
        }
    rms = math.sqrt(_cost(residual) / len(residual))
    maximum = max(value["angular_error"] for value in per_point.values())
    if not all(math.isfinite(value) for value in params + [rms, maximum]):
        raise CalibrationError("Calibration solution contains a non-finite value")
    if rms > max_rms_degrees:
        raise CalibrationError(f"Calibration RMS error {rms:.3f}° exceeds {max_rms_degrees:.3f}°")
    return {
        "solved": True,
        "camera": {"x": params[0], "y": params[1], "z": params[2]},
        "pan_offset": wrap_degrees(params[3]),
        "tilt_offset": params[4],
        "fixed_roll": _finite(calibration.get("fixed_roll", 0.0), "Fixed roll"),
        "horizontal_fov": _finite(calibration.get("horizontal_fov", 60.0), "Horizontal FOV"),
        "observation_count": len(rows),
        "rms_angular_error": rms,
        "max_angular_error": maximum,
        "residuals": per_point,
        "solved_at": utc_now(),
    }


def synthetic_observations(calibration: dict[str, Any], camera: dict[str, Any], pan_offset: float, tilt_offset: float, noise: dict[str, tuple[float, float]] | None = None) -> dict[str, Any]:
    points = target_points(calibration["target"])
    observations = {}
    for name in TARGET_POINTS:
        pan, tilt = tracking_angles(camera, points[name], pan_offset, tilt_offset)
        pan_noise, tilt_noise = (noise or {}).get(name, (0.0, 0.0))
        observations[name] = {
            "target": name,
            "raw_pan": pan + pan_noise,
            "raw_tilt": tilt + tilt_noise,
            "mapped_pan": pan + pan_noise,
            "mapped_tilt": tilt + tilt_noise,
            "captured_mono": 0.0,
            "setup_profile_id": calibration["setup_profile_id"],
            "synthetic": True,
        }
    return deepcopy(observations)
