from __future__ import annotations

import asyncio
import contextlib
import concurrent.futures
import json
import os
import platform
import socket
import time
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .debug import DebugRecorder
from .kinematics import RigGeometryError
from .imu import ImuAssistanceService, ImuCalibrationError
from .state import StateStore
from .source_manager import TrackingSourceManager
from .runtime import imu_status_publication_loop, pose_publication_loop, tracking_loop
from .tracking import TrackingConfigError
from .world_calibration import CalibrationError
from .dot_calibration import DotCalibrationError
from .power_control import MutationGate, PowerControlError, PowerControlService
from .browser_diagnostics import BrowserDiagnostics
from .stability import TrackingStabilityRecorder
from .performance import RuntimeProfiler
from .hdmi_output import ACTIONS as HDMI_ACTIONS, HdmiOutputError, HdmiOutputService
from .power_health import PowerHealthService
from .hang_watchdog import EventLoopWatchdog
from .native_pose import LatestPoseWriter
from .native_output import ACTIONS as NATIVE_ACTIONS, NativeOutputError, NativeOutputService
from .vision_calibration import MAX_IMAGE_BYTES, VisionCalibrationError, VisionCalibrationService

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
DATA_DIR = Path(os.getenv("KAIRIX_DATA_DIR", ROOT / "data")).resolve()
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

DATA_DIR.mkdir(parents=True, exist_ok=True)
debug = DebugRecorder(DATA_DIR)
store = StateStore(DATA_DIR, debug)
imu_service = ImuAssistanceService(DATA_DIR, debug)
store.imu_service = imu_service

app = FastAPI(title="Kairix RealLines", version=VERSION)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
acquisition_task: asyncio.Task | None = None
publication_task: asyncio.Task | None = None
imu_status_task: asyncio.Task | None = None
watchdog_heartbeat_task: asyncio.Task | None = None
stability_recorder = TrackingStabilityRecorder()
runtime_profiler = RuntimeProfiler()
source_manager = TrackingSourceManager(stability_recorder=stability_recorder)
store.pico_source_manager = source_manager
store.stability_recorder = stability_recorder
store.runtime_profiler = runtime_profiler
power_control = PowerControlService()
mutation_gate = MutationGate()
browser_diagnostics = BrowserDiagnostics()
hdmi_output = HdmiOutputService(DATA_DIR)
hdmi_operation_lock = asyncio.Lock()
hdmi_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="reallines-hdmi")
hdmi_status_inflight: asyncio.Task | None = None
diagnostic_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="reallines-diagnostics")
power_health = PowerHealthService()
power_status_inflight: asyncio.Task | None = None
vision_calibration = VisionCalibrationService(DATA_DIR)
event_loop_watchdog = EventLoopWatchdog(debug.session_dir)
native_output = NativeOutputService()
native_output_lock = asyncio.Lock()
native_output_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="reallines-native-output")
native_status_inflight: asyncio.Task | None = None
native_pose_default = "/dev/shm/kairix-reallines-pose-v1" if os.name == "posix" else str(DATA_DIR / "native_pose.bin")
native_pose_writer = LatestPoseWriter(Path(os.getenv("KAIRIX_NATIVE_POSE_PATH", native_pose_default)))
store.native_pose_writer = native_pose_writer


class CameraUpdate(BaseModel):
    pan: float | None = None
    tilt: float | None = None
    fov: float | None = None
    height: float | None = None
    valid: bool | None = None
    source: str | None = None
    raw_pan: int | None = None
    raw_tilt: int | None = None


class ItemCreate(BaseModel):
    type: str = "line"
    asset_id: str | None = None


class ItemUpdate(BaseModel):
    values: dict
    persist: bool = True


class BackgroundUpdate(BaseModel):
    color: str


class NamedCreate(BaseModel):
    name: str


class GroupUpdate(BaseModel):
    values: dict


class AxisConfigUpdate(BaseModel):
    values: dict


class WorldCalibrationUpdate(BaseModel):
    values: dict


class WorldTargetSelect(BaseModel):
    target: str


class SyncUpdate(BaseModel):
    values: dict


class EngineeringUpdate(BaseModel):
    values: dict


class PicoProcessingUpdate(BaseModel):
    values: dict


class MeasuredTargetsUpdate(BaseModel):
    values: dict


class PowerAuthorize(BaseModel):
    action: str
    secret: str


class PowerExecute(BaseModel):
    action: str
    token: str


class ImuUpdate(BaseModel):
    values: dict


class GroupTranslate(BaseModel):
    dx: float
    dy: float
    persist: bool = True


class LayoutCreate(BaseModel):
    name: str
    copy_current: bool = False


class CalibrationMark(BaseModel):
    mark: str


class EncoderReference(BaseModel):
    angle: float = 0.0


class ZoomFovCapture(BaseModel):
    fov: float


class CalibrationOverlayUpdate(BaseModel):
    values: dict


class DotCalibrationUpdate(BaseModel):
    values: dict


class FrontendLog(BaseModel):
    level: str = "info"
    page: str = "unknown"
    event: str
    data: dict = {}


class BrowserMetricsReport(BaseModel):
    client_id: str
    page: str
    label: str | None = None
    metrics: dict


class StabilityRecordingStart(BaseModel):
    duration_seconds: float = 10.0
    mode: str = "live"


class HdmiOutputUpdate(BaseModel):
    values: dict


class VisionCalibrationUpdate(BaseModel):
    values: dict


@app.middleware("http")
async def power_transition_gate(request: Request, call_next):
    guarded = request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path not in {
        "/api/system-power/authorize", "/api/system-power/execute",
    }
    if not guarded:
        return await call_next(request)
    try:
        async with mutation_gate.mutation():
            return await call_next(request)
    except PowerControlError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=503)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as exc:
        debug.error("http_exception", path=request.url.path, error=repr(exc))
        raise
    elapsed = (time.monotonic() - start) * 1000.0
    quiet_paths = {"/api/pose", "/api/browser-metrics", "/api/performance", "/api/tracking-stability", "/api/hdmi-output"}
    if not request.url.path.startswith("/static/") and request.url.path not in quiet_paths:
        debug.app("http_request", method=request.method, path=request.url.path, status=response.status_code, ms=round(elapsed, 3))
    if request.url.path.startswith("/static/") or request.url.path in {"/", "/engineering", "/calibration", "/control", "/gfx", "/preview"}:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.on_event("startup")
async def start_acquisition():
    global acquisition_task, publication_task, imu_status_task, watchdog_heartbeat_task
    source_manager.configure_pico_processing(store.pico_processing)
    source_manager.configure(store.profile_index["active_id"], store.profile)
    imu_service.start()
    runtime_profiler.install_gc_monitoring()
    event_loop_watchdog.start()
    native_pose_writer.open()
    acquisition_task = asyncio.create_task(tracking_loop(store, source_manager), name="tracking-acquisition-200hz")
    publication_task = asyncio.create_task(pose_publication_loop(store), name="pose-publication-50hz")
    imu_status_task = asyncio.create_task(imu_status_publication_loop(store, imu_service), name="optional-imu-status")
    watchdog_heartbeat_task = asyncio.create_task(event_loop_watchdog.heartbeat_loop(), name="event-loop-heartbeat")


@app.on_event("shutdown")
async def stop_acquisition():
    global acquisition_task, publication_task, imu_status_task, watchdog_heartbeat_task
    tasks = [task for task in (acquisition_task, publication_task, imu_status_task, watchdog_heartbeat_task) if task]
    for task in tasks:
        task.cancel()
    if tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks)
    acquisition_task = publication_task = imu_status_task = watchdog_heartbeat_task = None
    source_manager.stop()
    imu_service.stop()
    runtime_profiler.uninstall_gc_monitoring()
    event_loop_watchdog.stop()
    native_pose_writer.close()
    debug.flush()


@app.get("/")
async def root():
    return RedirectResponse("/engineering")


@app.get("/engineering")
async def engineering():
    return FileResponse(STATIC / "engineering.html")


@app.get("/calibration")
async def calibration():
    return FileResponse(STATIC / "calibration.html")


@app.get("/control")
async def control():
    return FileResponse(STATIC / "control.html")


@app.get("/gfx")
async def gfx():
    return FileResponse(STATIC / "gfx.html")


@app.get("/preview")
async def preview():
    return FileResponse(STATIC / "preview.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon")


@app.get("/api/state")
async def get_state():
    return store.snapshot()


@app.get("/api/pose")
async def get_pose():
    return store.pose_snapshot()


@app.get("/api/browser-metrics")
async def get_browser_metrics():
    return {"clients": browser_diagnostics.snapshot()}


@app.post("/api/browser-metrics")
async def update_browser_metrics(body: BrowserMetricsReport):
    try:
        accepted = browser_diagnostics.report(body.client_id, body.page, body.label, body.metrics)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    stability_recorder.record_browser_metrics(body.model_dump(), time.monotonic())
    return {"accepted": accepted}


def _hdmi_status_data(status: dict) -> dict:
    reports = browser_diagnostics.snapshot()
    client = next((report for report in reports
                   if report["page"] == "gfx" and report["label"].strip().lower() == "hdmi"), None)
    if client:
        metrics = client.get("metrics") or {}
        status["application_connected"] = True
        status["displayed_fps"] = metrics.get("animation_frame_hz_5s")
        status["frame_interval_p95_ms"] = metrics.get("frame_interval_p95_ms_5s")
        status["missed_frame_deadlines_5s"] = metrics.get("missed_frame_deadlines_5s")
        status["operational_state"] = ("rendering" if metrics.get("animation_frame_hz_5s")
                                       else "browser_connected")
    return status


async def _run_hdmi_blocking(name: str, function, *args):
    async with hdmi_operation_lock:
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(hdmi_executor, function, *args)
        elapsed = (time.monotonic() - started) * 1000.0
        runtime_profiler.observe(f"{name}_ms", elapsed)
        stability_recorder.record_blocking_operation(name, elapsed, time.monotonic())
        return result


async def _compute_hdmi_status(*, refresh: bool = False) -> dict:
    # Serialize bounded blocking discovery/service operations and keep them off
    # the acquisition/publication event loop.
    status = await _run_hdmi_blocking("hdmi_status", lambda: hdmi_output.status(refresh=refresh))
    return _hdmi_status_data(status)


async def _hdmi_status(*, refresh: bool = False) -> dict:
    global hdmi_status_inflight
    if refresh:
        return await _compute_hdmi_status(refresh=True)
    task = hdmi_status_inflight
    if task is None or task.done():
        task = asyncio.create_task(_compute_hdmi_status(), name="hdmi-status-singleflight")
        hdmi_status_inflight = task
    try:
        return await asyncio.shield(task)
    finally:
        if hdmi_status_inflight is task and task.done():
            hdmi_status_inflight = None


@app.get("/api/hdmi-output")
async def hdmi_output_status():
    return await _hdmi_status()


@app.post("/api/hdmi-output/refresh")
async def refresh_hdmi_output():
    return await _hdmi_status(refresh=True)


@app.patch("/api/hdmi-output")
async def update_hdmi_output(body: HdmiOutputUpdate):
    try:
        await _run_hdmi_blocking("hdmi_update", hdmi_output.update, body.values)
        return await _hdmi_status()
    except HdmiOutputError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/hdmi-output/reset")
async def reset_hdmi_output():
    try:
        await _run_hdmi_blocking("hdmi_reset", hdmi_output.reset)
        return await _hdmi_status()
    except HdmiOutputError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/hdmi-output/{action}")
async def control_hdmi_output(action: str):
    if action not in HDMI_ACTIONS:
        raise HTTPException(404, "Unknown HDMI output action")
    try:
        result = await _run_hdmi_blocking(f"hdmi_{action}", hdmi_output.action, action)
        return _hdmi_status_data(result)
    except HdmiOutputError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/system")
async def system_info():
    return {
        "product": "Kairix RealLines",
        "version": VERSION,
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "uptime_seconds": round(time.monotonic(), 1),
        "data_dir": str(DATA_DIR),
        "debug_enabled": debug.enabled,
        "debug_queue": debug.status(),
        "debug_session": debug.session_dir.name,
        "websocket_clients": len(store.clients),
    }


def _secure_admin_request(request: Request) -> str:
    # Deliberately use the transport URL seen by Uvicorn. Forwarded headers are
    # not accepted as proof of HTTPS.
    if request.url.scheme != "https":
        raise HTTPException(403, "Secure administration requires direct HTTPS")
    if request.headers.get("forwarded") or request.headers.get("x-forwarded-proto"):
        raise HTTPException(403, "Forwarded transport headers are not accepted for secure administration")
    host = request.headers.get("host", "")
    origin = request.headers.get("origin", "")
    if not host or origin != f"https://{host}":
        raise HTTPException(403, "Secure administration origin check failed")
    return request.client.host if request.client else "unknown"


@app.get("/api/system-power/status")
async def system_power_status(request: Request):
    return {**power_control.status(), "request_secure": request.url.scheme == "https",
            "mutations_blocked": mutation_gate.closing}


@app.post("/api/system-power/authorize")
async def system_power_authorize(body: PowerAuthorize, request: Request):
    client_id = _secure_admin_request(request)
    try:
        result = power_control.authorize(body.action, body.secret, client_id)
    except PowerControlError as exc:
        raise HTTPException(403, str(exc)) from exc
    debug.app("system_power_authorized", action=body.action, client=client_id)
    return result


@app.post("/api/system-power/execute")
async def system_power_execute(body: PowerExecute, request: Request, background_tasks: BackgroundTasks):
    client_id = _secure_admin_request(request)
    try:
        await mutation_gate.begin_transition()
        async with store.lock:
            pass  # All admitted writes have drained; finish any final atomic persistence.
        power_control.consume(body.token, body.action, client_id)
    except Exception as exc:
        await mutation_gate.reopen()
        if not isinstance(exc, PowerControlError):
            debug.error("system_power_transition_failed", action=body.action, error=repr(exc))
            raise HTTPException(500, "Could not prepare the system transition") from exc
        raise HTTPException(403, str(exc)) from exc
    debug.app("system_power_requested", action=body.action, client=client_id)
    async def execute_action() -> None:
        try:
            await power_control.execute_consumed(body.action)
        except Exception as exc:
            await mutation_gate.reopen()
            debug.error("system_power_helper_failed", action=body.action, error=repr(exc))
    background_tasks.add_task(execute_action)
    return {"accepted": True, "action": body.action, "message": "System action acknowledged; the connection may close"}


@app.post("/api/camera")
async def update_camera(body: CameraUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    return await store.set_camera(updates)


@app.get("/api/tracking")
async def get_tracking():
    snapshot = store.snapshot()
    return {"tracking": snapshot["tracking"], "profiles": snapshot["profiles"], "camera": snapshot["camera"], "render_camera": snapshot["render_camera"], "sync": snapshot["sync"]}


@app.get("/api/performance")
async def performance_status():
    global power_status_inflight
    if power_status_inflight is None or power_status_inflight.done():
        loop = asyncio.get_running_loop()
        power_status_inflight = asyncio.ensure_future(
            loop.run_in_executor(diagnostic_executor, power_health.status)
        )
    result = runtime_profiler.snapshot()
    result["pico"] = source_manager.pico_status()
    result["event_loop_watchdog"] = event_loop_watchdog.status()
    result["power"] = await asyncio.shield(power_status_inflight)
    if power_status_inflight.done():
        power_status_inflight = None
    return result


@app.get("/api/tracking-stability")
async def tracking_stability_status():
    return stability_recorder.status()


@app.post("/api/tracking-stability/start")
async def start_tracking_stability(body: StabilityRecordingStart):
    try:
        return stability_recorder.start(
            body.duration_seconds, mode=body.mode,
            fixed_pose=store.camera if body.mode in {"fixed_pose", "fixed_fov"} else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


async def _run_native_output(function, *args):
    async with native_output_lock:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(native_output_executor, function, *args)


@app.get("/api/native-output")
async def native_output_status():
    global native_status_inflight
    task = native_status_inflight
    if task is None or task.done():
        task = asyncio.create_task(_run_native_output(native_output.status), name="native-status-singleflight")
        native_status_inflight = task
    try:
        return await asyncio.shield(task)
    finally:
        if native_status_inflight is task and task.done():
            native_status_inflight = None


@app.post("/api/native-output/{action}")
async def control_native_output(action: str):
    if action not in NATIVE_ACTIONS:
        raise HTTPException(404, "Unknown native output action")
    try:
        return await _run_native_output(native_output.action, action)
    except NativeOutputError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/tracking-stability/stop")
async def stop_tracking_stability():
    return stability_recorder.stop()


@app.get("/api/tracking-stability/export")
async def export_tracking_stability(format: str = "jsonl"):
    if format == "jsonl":
        return Response(stability_recorder.export_jsonl(), media_type="application/x-ndjson",
                        headers={"Content-Disposition": "attachment; filename=tracking-stability.jsonl"})
    if format == "csv":
        return Response(stability_recorder.export_csv(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=tracking-stability.csv"})
    raise HTTPException(400, "Tracking stability export format must be jsonl or csv")


@app.get("/api/sync")
async def get_sync_state():
    snapshot = store.snapshot()
    return {"sync": snapshot["sync"], "live_camera": snapshot["live_camera"], "render_camera": snapshot["render_camera"]}


@app.patch("/api/sync")
async def update_sync_state(body: SyncUpdate):
    try:
        return await store.update_sync_config(body.values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/sync/reset")
async def reset_sync_delay():
    return await store.update_sync_config({"graphics_delay_ms": 0})


@app.patch("/api/engineering")
async def update_engineering_settings(body: EngineeringUpdate):
    try:
        return await store.update_engineering_settings(body.values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/imu")
async def update_imu(body: ImuUpdate):
    try:
        result = imu_service.configure(body.values)
    except (ImuCalibrationError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    await store.broadcast_state()
    return result


@app.post("/api/imu/bias")
async def calibrate_imu_bias():
    try:
        result = imu_service.calibrate_bias()
    except ImuCalibrationError as exc:
        raise HTTPException(400, str(exc)) from exc
    await store.broadcast_state()
    return result


def _imu_axis_state(axis: str) -> tuple[dict, dict]:
    if axis not in {"pan", "tilt"}:
        raise HTTPException(404, "Unknown IMU calibration axis")
    runtime = store.tracking["axes"][axis]
    config = store.profile["axes"][axis]
    if config["source"] not in {"quadrature_gpio", "linux_rotary"} or not runtime.get("driver_alive"):
        raise HTTPException(400, f"{axis.title()} encoder must be connected for IMU-assisted calibration")
    return runtime, config


@app.post("/api/imu/{kind}/{axis}/start")
async def start_imu_measurement(kind: str, axis: str):
    runtime, _ = _imu_axis_state(axis)
    try:
        imu_service.begin_measurement(kind, axis, int(runtime["raw_count"]))
    except ImuCalibrationError as exc:
        raise HTTPException(400, str(exc)) from exc
    await store.broadcast_state()
    return imu_service.snapshot()


@app.post("/api/imu/{kind}/{axis}/stop")
async def stop_imu_measurement(kind: str, axis: str):
    runtime, config = _imu_axis_state(axis)
    active = imu_service.snapshot().get("active") or {}
    if active.get("kind") != kind or active.get("axis") != axis:
        raise HTTPException(400, "The requested IMU measurement is not active")
    mapping = config["mapping"]
    try:
        result = imu_service.finish_measurement(int(runtime["raw_count"]), float(mapping["ppr"]), int(mapping["quadrature_multiplier"]))
    except ImuCalibrationError as exc:
        raise HTTPException(400, str(exc)) from exc
    await store.broadcast_state()
    return result


@app.delete("/api/imu/calibration")
async def reset_imu_calibration():
    imu_service.reset_calibration()
    await store.broadcast_state()
    return {"ok": True}


@app.post("/api/profiles")
async def create_profile(body: NamedCreate):
    result = await store.create_profile(body.name)
    source_manager.configure(store.profile_index["active_id"], store.profile)
    return result


@app.post("/api/profiles/{profile_id}/select")
async def select_profile(profile_id: str):
    profile = await store.select_profile(profile_id)
    if profile is None:
        raise HTTPException(404, "Setup profile not found")
    source_manager.configure(store.profile_index["active_id"], store.profile)
    return profile


@app.patch("/api/profiles/{profile_id}")
async def rename_profile(profile_id: str, body: NamedCreate):
    profile = await store.rename_profile(profile_id, body.name)
    if profile is None:
        raise HTTPException(404, "Setup profile not found")
    return profile


@app.post("/api/profiles/{profile_id}/duplicate")
async def duplicate_profile(profile_id: str, body: NamedCreate | None = None):
    profile = await store.duplicate_profile(profile_id, body.name if body else None)
    if profile is None:
        raise HTTPException(404, "Setup profile not found")
    source_manager.configure(store.profile_index["active_id"], store.profile)
    return profile


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str):
    if not await store.delete_profile(profile_id):
        raise HTTPException(400, "Setup profile not found or at least one profile must remain")
    source_manager.configure(store.profile_index["active_id"], store.profile)
    return {"ok": True}


@app.patch("/api/profiles/{profile_id}/axes/{axis}")
async def update_axis_config(profile_id: str, axis: str, body: AxisConfigUpdate):
    try:
        config = await store.update_axis_config(profile_id, axis, body.values)
    except TrackingConfigError as exc:
        debug.error("tracking_config_invalid", profile_id=profile_id, axis=axis, error=str(exc))
        raise HTTPException(400, str(exc)) from exc
    if config is None:
        raise HTTPException(404, "Active setup profile or axis not found")
    source_manager.configure(store.profile_index["active_id"], store.profile)
    return config


@app.patch("/api/profiles/{profile_id}/camera-rig")
async def update_camera_rig(profile_id: str, body: AxisConfigUpdate):
    try:
        rig_offset = await store.update_rig_geometry(profile_id, body.values)
    except RigGeometryError as exc:
        raise HTTPException(400, str(exc)) from exc
    if rig_offset is None:
        raise HTTPException(404, "Active setup profile not found")
    # Rig geometry does not alter encoder acquisition or reference semantics,
    # so the GPIO source manager is deliberately not reconfigured here.
    return rig_offset


@app.get("/api/pico/status")
async def pico_controller_status():
    return source_manager.pico_status()


@app.patch("/api/pico/processing")
async def update_pico_processing(body: PicoProcessingUpdate):
    try:
        return await store.update_pico_processing(body.values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/measured-targets")
async def update_measured_targets(body: MeasuredTargetsUpdate):
    try:
        return await store.update_measured_targets(body.values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/tracking/{axis}/reference")
async def set_encoder_reference(axis: str, body: EncoderReference):
    if axis not in {"pan", "tilt", "zoom", "focus"}:
        raise HTTPException(404, "Unknown tracking axis")
    try:
        source_manager.set_reference(axis, body.angle)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    async with store.lock:
        store.sample_tick(source_manager.snapshots())
    await store.broadcast_state()
    return store.tracking["axes"][axis]


@app.delete("/api/tracking/{axis}/reference")
async def clear_encoder_reference(axis: str):
    try:
        source_manager.clear_reference(axis)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    async with store.lock:
        store.sample_tick(source_manager.snapshots())
    await store.broadcast_state()
    return {"ok": True}


@app.patch("/api/tracking/zoom/lens-fov/{point}")
async def save_zoom_lens_fov(point: str, body: ZoomFovCapture):
    try:
        return await store.save_zoom_lens_fov(point, body.fov)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/tracking/zoom/fov-points/{point}")
async def capture_zoom_fov_point(point: str, body: ZoomFovCapture):
    try:
        return await store.capture_zoom_fov_point(point, body.fov)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/tracking/zoom/fov-points/{point}")
async def update_zoom_fov_point(point: str, body: ZoomFovCapture):
    try:
        return await store.update_zoom_fov_point(point, body.fov)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/calibration-overlay")
async def update_calibration_overlay(body: CalibrationOverlayUpdate):
    try:
        return await store.update_calibration_overlay(body.values)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/world-calibration/dot-calibration")
async def update_dot_calibration(body: DotCalibrationUpdate):
    try:
        return await store.update_dot_calibration(body.values)
    except (DotCalibrationError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/world-calibration/dot-calibration/solve")
async def solve_dot_calibration():
    try:
        return await store.solve_dot_calibration()
    except (DotCalibrationError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/world-calibration/dot-calibration/apply")
async def apply_dot_calibration():
    try:
        return await store.apply_dot_calibration_solution()
    except (DotCalibrationError, TrackingConfigError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/world-calibration/dot-calibration/preview-world")
async def preview_dot_world_reference(body: DotCalibrationUpdate):
    try:
        return await store.preview_dot_world_reference((body.values or {}).get("target_id"))
    except (DotCalibrationError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/world-calibration/dot-calibration/solve-world")
async def solve_dot_world_reference(body: DotCalibrationUpdate):
    try:
        values = body.values or {}
        return await store.solve_dot_world_reference(
            values.get("target_id"), accept_review_only=values.get("accept_review_only") is True
        )
    except (DotCalibrationError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/world-calibration/dot-calibration/quality")
async def dot_calibration_quality(body: DotCalibrationUpdate):
    try:
        values = body.values or {}
        return await store.dot_calibration_quality(
            values.get("target_id"), observation_id=values.get("observation_id"),
            marker_id=values.get("marker_id"),
        )
    except (DotCalibrationError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


def _vision_capture_context() -> dict:
    snapshot = store.snapshot()
    tracking = snapshot.get("tracking") or {}
    axes = tracking.get("axes") or {}
    return {
        "captured_mono": snapshot.get("server_mono", time.monotonic()),
        "tracking_valid": bool(tracking.get("valid")),
        "references": {axis: (axes.get(axis) or {}).get("reference_id") for axis in ("pan", "tilt", "zoom")},
        "axes": {
            axis: {key: (axes.get(axis) or {}).get(key) for key in
                   ("raw_count", "reference_count", "reference_angle", "relative_count")}
            for axis in ("pan", "tilt", "zoom")
        },
        "camera": snapshot.get("render_camera") or {},
    }


async def _read_vision_upload(file: UploadFile) -> bytes:
    image = await file.read(MAX_IMAGE_BYTES + 1)
    if len(image) > MAX_IMAGE_BYTES:
        raise VisionCalibrationError("Calibration image exceeds 12 MiB")
    return image


@app.get("/api/vision-calibration")
async def vision_calibration_status():
    return vision_calibration.status()


@app.post("/api/vision-calibration/start")
async def start_vision_calibration():
    return vision_calibration.start()


@app.post("/api/vision-calibration/stop")
async def stop_vision_calibration():
    return vision_calibration.stop()


@app.patch("/api/vision-calibration/obs")
async def configure_vision_obs(body: VisionCalibrationUpdate):
    try:
        return vision_calibration.configure_obs(body.values)
    except (VisionCalibrationError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/vision-calibration/teach")
async def teach_vision_target(
    file: UploadFile = File(...), markers_json: str = Form(...), target_id: str = Form(...),
):
    try:
        markers = json.loads(markers_json)
        if not isinstance(markers, dict):
            raise VisionCalibrationError("Taught markers must be an object keyed by marker id")
        return await vision_calibration.teach(await _read_vision_upload(file), markers, target_id)
    except (json.JSONDecodeError, VisionCalibrationError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/vision-calibration/analyse")
async def analyse_vision_still(file: UploadFile = File(...)):
    try:
        return await vision_calibration.analyse(await _read_vision_upload(file), _vision_capture_context(), source="manual")
    except (VisionCalibrationError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/vision-calibration/capture-obs")
async def capture_vision_from_obs():
    try:
        return await vision_calibration.capture_obs_pair(_vision_capture_context)
    except (VisionCalibrationError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/world-calibration/dot-calibration/restore-world")
async def restore_previous_dot_world():
    try:
        return await store.restore_previous_dot_world()
    except (DotCalibrationError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/world-calibration")
async def get_world_calibration():
    snapshot = store.snapshot()
    return {"world_calibrations": snapshot["world_calibrations"], "camera": snapshot["camera"], "tracking": snapshot["tracking"]}


@app.post("/api/world-calibrations")
async def create_world_calibration(body: NamedCreate):
    return await store.create_world_calibration(body.name)


@app.post("/api/world-calibrations/{calibration_id}/select")
async def select_world_calibration(calibration_id: str):
    result = await store.select_world_calibration(calibration_id)
    if result is None:
        raise HTTPException(404, "World calibration not found")
    return result


@app.patch("/api/world-calibration")
async def update_world_calibration(body: WorldCalibrationUpdate):
    try:
        return await store.update_world_calibration(body.values)
    except (CalibrationError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/world-calibration/target")
async def select_world_target(body: WorldTargetSelect):
    try:
        return {"target": await store.select_world_target(body.target)}
    except CalibrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/world-calibration/next")
async def next_world_target():
    return {"target": await store.move_world_target(1)}


@app.post("/api/world-calibration/previous")
async def previous_world_target():
    return {"target": await store.move_world_target(-1)}


@app.post("/api/world-calibration/mark")
async def mark_world_target(body: WorldTargetSelect | None = None):
    try:
        return await store.mark_world_target(body.target if body else None)
    except CalibrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/world-calibration/marks/{target}")
async def clear_world_mark(target: str):
    try:
        return {"cleared": await store.clear_world_mark(target)}
    except CalibrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/world-calibration/solve")
async def solve_world_calibration():
    try:
        return await store.solve_active_world_calibration()
    except CalibrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/world-calibration/reset")
async def reset_world_calibration():
    await store.reset_world_calibration()
    return {"ok": True}


@app.post("/api/world-calibration/synthetic")
async def generate_synthetic_world_calibration(body: WorldCalibrationUpdate):
    try:
        return await store.generate_synthetic_world_calibration(body.values)
    except (CalibrationError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/scene/items")
async def add_item(body: ItemCreate):
    if body.type not in {"line", "text", "marker", "image"}:
        raise HTTPException(400, "Only line, text, marker and image are supported")
    if body.type == "image":
        asset = store.asset_info(body.asset_id or "")
        if asset is None:
            raise HTTPException(400, "A valid PNG asset_id is required")
        return await store.add_image_item(asset)
    return await store.add_item(body.type)


@app.post("/api/assets/png")
async def upload_png(file: UploadFile = File(...)):
    if file.content_type != "image/png":
        raise HTTPException(415, "Only image/png uploads are accepted")
    data = await file.read(10 * 1024 * 1024 + 1)
    try:
        return store.save_png_asset(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/assets/{asset_id}")
async def get_asset(asset_id: str):
    path = store.asset_path(asset_id)
    if path is None:
        raise HTTPException(404, "Asset not found")
    return FileResponse(path, media_type="image/png")


@app.patch("/api/scene/items/{item_id}")
async def update_item(item_id: str, body: ItemUpdate):
    item = await store.update_item(item_id, body.values, persist=body.persist)
    if item is None:
        raise HTTPException(404, "Scene item not found")
    return item


@app.delete("/api/scene/items/{item_id}")
async def delete_item(item_id: str):
    if not await store.delete_item(item_id):
        raise HTTPException(404, "Scene item not found")
    return JSONResponse({"ok": True})


@app.post("/api/scene/items/{item_id}/duplicate")
async def duplicate_item(item_id: str):
    item = await store.duplicate_item(item_id)
    if item is None:
        raise HTTPException(404, "Scene item not found")
    return item


@app.post("/api/scene/groups")
async def add_group(body: NamedCreate):
    return await store.add_group(body.name)


@app.patch("/api/scene/groups/{group_id}")
async def update_group(group_id: str, body: GroupUpdate):
    group = await store.update_group(group_id, body.values)
    if group is None:
        raise HTTPException(404, "Group not found")
    return group


@app.delete("/api/scene/groups/{group_id}")
async def delete_group(group_id: str, delete_contents: bool = False):
    if not await store.delete_group(group_id, delete_contents):
        raise HTTPException(404, "Group not found")
    return {"ok": True}


@app.post("/api/scene/groups/{group_id}/translate")
async def translate_group(group_id: str, body: GroupTranslate):
    result = await store.translate_group(group_id, body.dx, body.dy, body.persist)
    if result is None:
        raise HTTPException(404, "Group not found")
    return result


@app.post("/api/scene/groups/{group_id}/duplicate")
async def duplicate_group(group_id: str):
    group = await store.duplicate_group(group_id)
    if group is None:
        raise HTTPException(404, "Group not found")
    return group


@app.post("/api/scene/background")
async def set_background(body: BackgroundUpdate):
    await store.set_scene_background(body.color)
    return {"ok": True}


@app.post("/api/layouts")
async def create_layout(body: LayoutCreate):
    return await store.create_layout(body.name, body.copy_current)


@app.post("/api/layouts/save")
async def save_layout():
    return await store.save_layout()


@app.post("/api/layouts/{layout_id}/load")
async def load_layout(layout_id: str):
    layout = await store.load_layout(layout_id)
    if layout is None:
        raise HTTPException(404, "Layout not found")
    return layout


@app.patch("/api/layouts/{layout_id}")
async def rename_layout(layout_id: str, body: NamedCreate):
    layout = await store.rename_layout(layout_id, body.name)
    if layout is None:
        raise HTTPException(404, "Layout not found")
    return layout


@app.post("/api/layouts/{layout_id}/duplicate")
async def duplicate_layout(layout_id: str, body: NamedCreate):
    layout = await store.duplicate_layout(layout_id, body.name)
    if layout is None:
        raise HTTPException(404, "Layout not found")
    return layout


@app.delete("/api/layouts/{layout_id}")
async def delete_layout(layout_id: str):
    if not await store.delete_layout(layout_id):
        raise HTTPException(404, "Layout not found")
    return {"ok": True}


@app.post("/api/calibration/mark")
async def calibration_mark(body: CalibrationMark):
    if body.mark not in {"pan_left", "pan_right", "tilt_down", "tilt_up"}:
        raise HTTPException(400, "Unknown calibration mark")
    try:
        return await store.calibration_mark(body.mark)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/calibration/reset")
async def calibration_reset():
    await store.reset_calibration()
    return {"ok": True}


@app.post("/api/debug/frontend")
async def frontend_log(body: FrontendLog):
    debug.frontend(body.event, level=body.level, page=body.page, **body.data)
    return {"ok": True}


@app.post("/api/debug/{enabled}")
async def debug_toggle(enabled: bool):
    if not enabled:
        debug.flush()
    debug.enabled = enabled
    store.engineering["debug_enabled"] = enabled
    store._save("engineering.json", store.engineering)
    debug.app("debug_toggle", enabled=enabled)
    await store.broadcast_state()
    return {"enabled": enabled}


@app.get("/api/diagnostics/bundle")
async def diagnostic_bundle():
    files = [DATA_DIR / "scene.json", DATA_DIR / "calibration.json", DATA_DIR / "engineering.json", DATA_DIR / "pico_processing.json", DATA_DIR / "measured_targets.json", DATA_DIR / "zoom_lens_profiles.json", DATA_DIR / "sync.json", DATA_DIR / "imu.json", DATA_DIR / "layouts", DATA_DIR / "profiles", DATA_DIR / "world_calibrations"]
    bundle = debug.create_bundle(files)
    return FileResponse(bundle, filename=bundle.name, media_type="application/zip")


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    output = store.register_websocket(websocket)
    debug.websocket("connected", clients=len(store.clients), client=str(websocket.client))
    output.enqueue({"type": "state", "data": store.snapshot()})
    try:
        while True:
            msg = await websocket.receive_text()
            debug.websocket("client_message", message=msg[:500])
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        debug.error("websocket_error", error=repr(exc))
    finally:
        await store.unregister_websocket(websocket)
        debug.websocket("disconnected", clients=len(store.clients))
