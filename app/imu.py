from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any, Iterable


class ImuCalibrationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ImuSample:
    timestamp: float
    gyro_x: float
    gyro_y: float
    gyro_z: float
    accel_x: float
    accel_y: float
    accel_z: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


def _vector(sample: ImuSample, prefix: str) -> tuple[float, float, float]:
    return tuple(float(getattr(sample, f"{prefix}_{axis}")) for axis in "xyz")  # type: ignore[return-value]


def _magnitude(vector: Iterable[float]) -> float:
    return math.sqrt(sum(float(value) ** 2 for value in vector))


def _dot(a: Iterable[float], b: Iterable[float]) -> float:
    return sum(float(left) * float(right) for left, right in zip(a, b))


def _unit(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    magnitude = _magnitude(vector)
    if magnitude <= 1e-9:
        raise ImuCalibrationError("IMU movement was too small to determine an axis")
    return tuple(value / magnitude for value in vector)  # type: ignore[return-value]


def _validate_samples(samples: list[ImuSample], minimum: int = 2) -> None:
    if len(samples) < minimum:
        raise ImuCalibrationError("Not enough IMU samples were collected")
    if any(samples[index].timestamp <= samples[index - 1].timestamp for index in range(1, len(samples))):
        raise ImuCalibrationError("IMU sample timestamps must increase monotonically")
    if not all(math.isfinite(value) for sample in samples for value in asdict(sample).values()):
        raise ImuCalibrationError("IMU samples must be finite")


def estimate_stationary_bias(samples: Iterable[ImuSample]) -> dict[str, Any]:
    values = list(samples)
    _validate_samples(values, minimum=20)
    duration = values[-1].timestamp - values[0].timestamp
    if duration < 0.5:
        raise ImuCalibrationError("Keep the camera still for at least 0.5 seconds")
    gyro_axes = list(zip(*(_vector(sample, "gyro") for sample in values)))
    accel_magnitudes = [_magnitude(_vector(sample, "accel")) for sample in values]
    gyro_std = tuple(pstdev(axis) for axis in gyro_axes)
    if max(gyro_std) > 0.8:
        raise ImuCalibrationError("Camera moved during stationary gyro-bias measurement")
    if not 0.8 <= fmean(accel_magnitudes) <= 1.2 or pstdev(accel_magnitudes) > 0.05:
        raise ImuCalibrationError("Accelerometer gravity measurement was unstable")
    bias = tuple(fmean(axis) for axis in gyro_axes)
    return {
        "gyro_bias_dps": {axis: bias[index] for index, axis in enumerate("xyz")},
        "gyro_std_dps": {axis: gyro_std[index] for index, axis in enumerate("xyz")},
        "sample_count": len(values),
        "duration_seconds": duration,
    }


def integrate_gyro(samples: Iterable[ImuSample], bias: dict[str, float]) -> tuple[float, float, float]:
    values = list(samples)
    _validate_samples(values)
    result = [0.0, 0.0, 0.0]
    bias_vector = tuple(float(bias[axis]) for axis in "xyz")
    for previous, current in zip(values, values[1:]):
        dt = current.timestamp - previous.timestamp
        if dt > 0.1:
            raise ImuCalibrationError("IMU sample gap was too large")
        before = _vector(previous, "gyro")
        after = _vector(current, "gyro")
        for index in range(3):
            result[index] += ((before[index] + after[index]) / 2.0 - bias_vector[index]) * dt
    return tuple(result)  # type: ignore[return-value]


def mounting_alignment(
    pan_samples: Iterable[ImuSample],
    tilt_samples: Iterable[ImuSample],
    bias: dict[str, float],
) -> dict[str, Any]:
    pan_integral = integrate_gyro(pan_samples, bias)
    tilt_integral = integrate_gyro(tilt_samples, bias)
    if _magnitude(pan_integral) < 5.0 or _magnitude(tilt_integral) < 5.0:
        raise ImuCalibrationError("Use a clear Pan-right and Tilt-up movement of at least 5 degrees")
    pan_axis, tilt_axis = _unit(pan_integral), _unit(tilt_integral)
    orthogonality = abs(_dot(pan_axis, tilt_axis))
    if orthogonality > 0.35:
        raise ImuCalibrationError("Pan and Tilt movements did not establish distinct IMU axes; check where the sensor is mounted")
    return {
        "pan_axis": {axis: pan_axis[index] for index, axis in enumerate("xyz")},
        "tilt_axis": {axis: tilt_axis[index] for index, axis in enumerate("xyz")},
        "axis_dot": _dot(pan_axis, tilt_axis),
        "quality": round(1.0 - orthogonality, 6),
    }


def movement_observation(
    samples: Iterable[ImuSample],
    bias: dict[str, float],
    axis_vector: dict[str, float],
    start_count: int,
    end_count: int,
) -> dict[str, float | int]:
    integral = integrate_gyro(samples, bias)
    axis = tuple(float(axis_vector[name]) for name in "xyz")
    angle = _dot(integral, axis)
    total = _magnitude(integral)
    leakage = math.sqrt(max(0.0, total * total - angle * angle)) / max(total, 1e-9)
    delta = int(end_count) - int(start_count)
    if abs(angle) < 3.0:
        raise ImuCalibrationError("Camera movement was too small; use a short movement greater than 3 degrees")
    if delta == 0:
        raise ImuCalibrationError("Encoder count did not change during the IMU movement")
    if leakage > 0.25:
        raise ImuCalibrationError("Movement crossed another IMU axis or the temporary mount shifted")
    return {"start_count": int(start_count), "end_count": int(end_count), "delta_count": delta, "angle_deg": angle, "cross_axis_fraction": leakage}


def calibration_proposal(observations: Iterable[dict[str, Any]], ppr: float, multiplier: int) -> dict[str, Any]:
    values = list(observations)
    if len(values) < 3:
        raise ImuCalibrationError("Collect at least three movements, including a reversal")
    cycles, decode = float(ppr), int(multiplier)
    if cycles <= 0 or decode not in {1, 2, 4}:
        raise ImuCalibrationError("Encoder PPR or quadrature multiplier is invalid")
    angles = [float(value["angle_deg"]) for value in values]
    if not any(angle > 0 for angle in angles) or not any(angle < 0 for angle in angles):
        raise ImuCalibrationError("Collect movements in both directions")
    directions = [1 if int(value["delta_count"]) * angle > 0 else -1 for value, angle in zip(values, angles)]
    if len(set(directions)) != 1:
        raise ImuCalibrationError("Encoder direction was inconsistent across movements")
    ratios = [abs(int(value["delta_count"])) / (cycles * decode) / (abs(angle) / 360.0) for value, angle in zip(values, angles)]
    mean_ratio = fmean(ratios)
    coefficient = pstdev(ratios) / mean_ratio if mean_ratio else math.inf
    maximum_leakage = max(float(value.get("cross_axis_fraction", 0.0)) for value in values)
    confidence = max(0.0, 1.0 - max(coefficient * 4.0, maximum_leakage * 1.5))
    if coefficient > 0.1 or confidence < 0.7:
        raise ImuCalibrationError("Repeated IMU movements were inconsistent; use manual calibration or repeat after checking the mount")
    return {
        "encoder_revs_per_camera_rev": mean_ratio,
        "direction": directions[0],
        "confidence": confidence,
        "observation_count": len(values),
        "coefficient_of_variation": coefficient,
        "maximum_cross_axis_fraction": maximum_leakage,
        "ratios": ratios,
    }


class Mpu9250Adapter:
    """Minimal MPU-9250 adapter. smbus2 is imported only when explicitly enabled."""

    def __init__(self, bus: int = 1, address: int = 0x68) -> None:
        self.bus_number = int(bus)
        self.address = int(address)
        self._bus: Any = None

    def open(self) -> None:
        try:
            from smbus2 import SMBus  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Optional smbus2 package is not installed") from exc
        bus = SMBus(self.bus_number)
        try:
            identity = bus.read_byte_data(self.address, 0x75)
            if identity not in {0x71, 0x73}:
                raise RuntimeError(f"MPU-9250 not detected at I2C address 0x{self.address:02x}")
            bus.write_byte_data(self.address, 0x6B, 0x00)
            bus.write_byte_data(self.address, 0x1B, 0x08)  # gyro +/-500 dps
            bus.write_byte_data(self.address, 0x1C, 0x00)  # accelerometer +/-2 g
        except Exception:
            bus.close()
            raise
        self._bus = bus

    @staticmethod
    def _signed(high: int, low: int) -> int:
        value = (high << 8) | low
        return value - 65536 if value & 0x8000 else value

    def read(self) -> ImuSample:
        if self._bus is None:
            raise RuntimeError("MPU-9250 adapter is not open")
        data = self._bus.read_i2c_block_data(self.address, 0x3B, 14)
        accel = [self._signed(data[index], data[index + 1]) / 16384.0 for index in (0, 2, 4)]
        gyro = [self._signed(data[index], data[index + 1]) / 65.5 for index in (8, 10, 12)]
        return ImuSample(time.monotonic(), *gyro, *accel)

    def close(self) -> None:
        bus, self._bus = self._bus, None
        if bus is not None:
            bus.close()


class ImuAssistanceService:
    """Independent, optional IMU acquisition and calibration-proposal service."""

    DEFAULT_CONFIG = {"enabled": False, "source": "disabled", "bus": 1, "address": 0x68, "sample_rate_hz": 100.0}

    def __init__(self, data_dir: Path, debug: Any) -> None:
        self.path = Path(data_dir) / "imu.json"
        self.debug = debug
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._adapter: Mpu9250Adapter | None = None
        self._samples: deque[ImuSample] = deque(maxlen=6000)
        self.config = self._load_config()
        self.health = "DISABLED"
        self.status = "Optional IMU assistance is off"
        self.last_error: str | None = None
        self.bias: dict[str, Any] | None = None
        self.alignment_samples: dict[str, list[ImuSample]] = {}
        self.alignment: dict[str, Any] | None = None
        self.observations: dict[str, list[dict[str, Any]]] = {"pan": [], "tilt": []}
        self.proposals: dict[str, dict[str, Any] | None] = {"pan": None, "tilt": None}
        self.active: dict[str, Any] | None = None

    def _load_config(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            value = {}
        result = {**self.DEFAULT_CONFIG, **value}
        if result["source"] not in {"disabled", "mpu9250", "simulated"}:
            result["source"] = "disabled"
        try:
            result["bus"], result["address"] = int(result["bus"]), int(result["address"])
            result["sample_rate_hz"] = float(result["sample_rate_hz"])
            if result["bus"] < 0 or not 0x03 <= result["address"] <= 0x77 or not 10 <= result["sample_rate_hz"] <= 500:
                raise ValueError
        except (TypeError, ValueError):
            result.update({key: self.DEFAULT_CONFIG[key] for key in ("bus", "address", "sample_rate_hz")})
        result["enabled"] = bool(result["enabled"]) and result["source"] != "disabled"
        return result

    def _save_config(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.config, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def start(self) -> None:
        if not self.config["enabled"]:
            self.health, self.status = "DISABLED", "Optional IMU assistance is off"
            return
        if self.config["source"] == "simulated":
            self.health, self.status = "READY", "Simulated IMU ready"
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.health, self.status = "CONNECTING", "Looking for optional MPU-9250"
        self._thread = threading.Thread(target=self._run, name="optional-mpu9250", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._adapter:
            self._adapter.close()
        self._adapter = None

    def configure(self, values: dict[str, Any]) -> dict[str, Any]:
        unknown = set(values) - {"enabled", "source", "bus", "address", "sample_rate_hz"}
        if unknown:
            raise ImuCalibrationError(f"Unknown IMU setting: {sorted(unknown)[0]}")
        candidate = {**self.config, **values}
        if candidate["source"] not in {"disabled", "mpu9250", "simulated"}:
            raise ImuCalibrationError("IMU source must be disabled, mpu9250 or simulated")
        candidate["enabled"] = bool(candidate["enabled"]) and candidate["source"] != "disabled"
        candidate["bus"], candidate["address"] = int(candidate["bus"]), int(candidate["address"])
        candidate["sample_rate_hz"] = float(candidate["sample_rate_hz"])
        if candidate["bus"] < 0 or not 0x03 <= candidate["address"] <= 0x77:
            raise ImuCalibrationError("IMU I2C bus or address is invalid")
        if not 10 <= candidate["sample_rate_hz"] <= 500:
            raise ImuCalibrationError("IMU sample rate must be between 10 and 500 Hz")
        self.stop()
        with self._lock:
            self.config = candidate
            self._samples.clear()
            self.active = None
            self.last_error = None
            self._save_config()
        self.start()
        return self.snapshot()

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            try:
                adapter = Mpu9250Adapter(self.config["bus"], self.config["address"])
                adapter.open()
                self._adapter = adapter
                backoff = 0.5
                with self._lock:
                    self.health, self.status, self.last_error = "READY", "MPU-9250 ready", None
                interval = 1.0 / self.config["sample_rate_hz"]
                while not self._stop.wait(interval):
                    sample = adapter.read()
                    with self._lock:
                        self._samples.append(sample)
                break
            except Exception as exc:
                if self._adapter:
                    self._adapter.close()
                self._adapter = None
                with self._lock:
                    self.health, self.status, self.last_error = "NOT_CONNECTED", "Optional IMU is not connected", str(exc)
                    if self.active:
                        self.active = None
                        self.status = "IMU disconnected during calibration"
                if self._stop.wait(backoff):
                    break
                backoff = min(5.0, backoff * 2.0)

    def inject_simulated(self, sample: ImuSample) -> None:
        with self._lock:
            if not self.config["enabled"] or self.config["source"] != "simulated":
                raise ImuCalibrationError("Simulated IMU is not enabled")
            if self._samples and sample.timestamp <= self._samples[-1].timestamp:
                raise ImuCalibrationError("Simulated IMU timestamps must increase")
            self._samples.append(sample)
            self.health, self.status = "READY", "Simulated IMU ready"

    def calibrate_bias(self) -> dict[str, Any]:
        with self._lock:
            if self.health != "READY":
                raise ImuCalibrationError("IMU is not ready; manual calibration remains available")
            samples = list(self._samples)
        result = estimate_stationary_bias(samples[-300:])
        with self._lock:
            self.bias = result
            self.status = "Stationary bias measured — align Pan and Tilt"
        return deepcopy(result)

    def begin_measurement(self, kind: str, axis: str, raw_count: int) -> None:
        if kind not in {"alignment", "movement"} or axis not in {"pan", "tilt"}:
            raise ImuCalibrationError("Unknown IMU calibration measurement")
        with self._lock:
            if self.health != "READY":
                raise ImuCalibrationError("IMU is not ready; manual calibration remains available")
            if self.bias is None:
                raise ImuCalibrationError("Measure stationary IMU bias first")
            if kind == "movement" and (self.alignment is None or f"{axis}_axis" not in self.alignment):
                raise ImuCalibrationError("Complete Pan-right and Tilt-up mounting alignment first")
            if self.active:
                raise ImuCalibrationError("Another IMU measurement is already active")
            self.active = {
                "kind": kind,
                "axis": axis,
                "start_count": int(raw_count),
                "started_mono": self._samples[-1].timestamp if self._samples else time.monotonic(),
            }
            self.status = "Learning movement"

    def finish_measurement(self, raw_count: int, ppr: float, multiplier: int) -> dict[str, Any]:
        with self._lock:
            if self.health != "READY" or not self.active:
                raise ImuCalibrationError("No active IMU measurement, or the sensor disconnected")
            active = self.active
            self.active = None
            samples = [sample for sample in self._samples if sample.timestamp > active["started_mono"]]
            bias = deepcopy(self.bias["gyro_bias_dps"] if self.bias else None)
            alignment = deepcopy(self.alignment)
        if active["kind"] == "alignment":
            integrate_gyro(samples, bias)
            with self._lock:
                self.alignment_samples[active["axis"]] = samples
                if {"pan", "tilt"} <= self.alignment_samples.keys():
                    self.alignment = mounting_alignment(self.alignment_samples["pan"], self.alignment_samples["tilt"], bias)
                    self.status = "Mounting alignment ready — collect repeated Pan and Tilt movements"
                else:
                    self.status = "One mounting axis measured — measure the other axis"
            return self.snapshot()
        observation = movement_observation(samples, bias, alignment[f"{active['axis']}_axis"], active["start_count"], int(raw_count))
        with self._lock:
            self.observations[active["axis"]].append(observation)
            try:
                self.proposals[active["axis"]] = calibration_proposal(self.observations[active["axis"]], ppr, multiplier)
                self.status = "Calibration proposal ready — review before applying"
            except ImuCalibrationError as exc:
                self.proposals[active["axis"]] = None
                self.status = str(exc)
        return self.snapshot()

    def reset_calibration(self) -> None:
        with self._lock:
            self.bias = None
            self.alignment_samples = {}
            self.alignment = None
            self.observations = {"pan": [], "tilt": []}
            self.proposals = {"pan": None, "tilt": None}
            self.active = None
            self.status = "Ready to calibrate" if self.health == "READY" else self.status

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "optional": True,
                "enabled": self.config["enabled"],
                "source": self.config["source"],
                "health": self.health,
                "status": self.status,
                "last_error": self.last_error,
                "sample_count": len(self._samples),
                "last_sample_mono": self._samples[-1].timestamp if self._samples else None,
                "bias": deepcopy(self.bias),
                "alignment": deepcopy(self.alignment),
                "observations": deepcopy(self.observations),
                "proposals": deepcopy(self.proposals),
                "active": deepcopy(self.active),
                "config": deepcopy(self.config),
            }
