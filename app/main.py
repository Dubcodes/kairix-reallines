from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import socket
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .debug import DebugRecorder
from .state import StateStore
from .tracking import TrackingConfigError
from .world_calibration import CalibrationError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
DATA_DIR = Path(os.getenv("KAIRIX_DATA_DIR", ROOT / "data")).resolve()
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

DATA_DIR.mkdir(parents=True, exist_ok=True)
debug = DebugRecorder(DATA_DIR)
store = StateStore(DATA_DIR, debug)

app = FastAPI(title="Kairix RealLines", version=VERSION)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
history_task: asyncio.Task | None = None


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


class GroupTranslate(BaseModel):
    dx: float
    dy: float
    persist: bool = True


class LayoutCreate(BaseModel):
    name: str
    copy_current: bool = False


class CalibrationMark(BaseModel):
    mark: str


class FrontendLog(BaseModel):
    level: str = "info"
    page: str = "unknown"
    event: str
    data: dict = {}


@app.middleware("http")
async def request_logging(request: Request, call_next):
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as exc:
        debug.error("http_exception", path=request.url.path, error=repr(exc))
        raise
    elapsed = (time.monotonic() - start) * 1000.0
    if not request.url.path.startswith("/static/"):
        debug.app("http_request", method=request.method, path=request.url.path, status=response.status_code, ms=round(elapsed, 3))
    return response


@app.on_event("startup")
async def start_history_heartbeat():
    global history_task
    history_task = asyncio.create_task(store.history_heartbeat())


@app.on_event("shutdown")
async def stop_history_heartbeat():
    global history_task
    if history_task:
        history_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await history_task
        history_task = None


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


@app.get("/api/state")
async def get_state():
    return store.snapshot()


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
        "debug_session": debug.session_dir.name,
        "websocket_clients": len(store.clients),
    }


@app.post("/api/camera")
async def update_camera(body: CameraUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    return await store.set_camera(updates)


@app.get("/api/tracking")
async def get_tracking():
    snapshot = store.snapshot()
    return {"tracking": snapshot["tracking"], "profiles": snapshot["profiles"], "camera": snapshot["camera"], "render_camera": snapshot["render_camera"], "sync": snapshot["sync"]}


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


@app.post("/api/profiles")
async def create_profile(body: NamedCreate):
    return await store.create_profile(body.name)


@app.post("/api/profiles/{profile_id}/select")
async def select_profile(profile_id: str):
    profile = await store.select_profile(profile_id)
    if profile is None:
        raise HTTPException(404, "Setup profile not found")
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
    return profile


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str):
    if not await store.delete_profile(profile_id):
        raise HTTPException(400, "Setup profile not found or at least one profile must remain")
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
    return config


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
    if body.type not in {"line", "text", "marker"}:
        raise HTTPException(400, "Only line, text and marker are supported")
    return await store.add_item(body.type)


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
    debug.enabled = enabled
    store.engineering["debug_enabled"] = enabled
    store._save("engineering.json", store.engineering)
    debug.app("debug_toggle", enabled=enabled)
    await store.broadcast_state()
    return {"enabled": enabled}


@app.get("/api/diagnostics/bundle")
async def diagnostic_bundle():
    files = [DATA_DIR / "scene.json", DATA_DIR / "calibration.json", DATA_DIR / "engineering.json", DATA_DIR / "sync.json", DATA_DIR / "layouts", DATA_DIR / "profiles", DATA_DIR / "world_calibrations"]
    bundle = debug.create_bundle(files)
    return FileResponse(bundle, filename=bundle.name, media_type="application/zip")


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    store.clients.add(websocket)
    debug.websocket("connected", clients=len(store.clients), client=str(websocket.client))
    await websocket.send_json({"type": "state", "data": store.snapshot()})
    try:
        while True:
            msg = await websocket.receive_text()
            debug.websocket("client_message", message=msg[:500])
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        debug.error("websocket_error", error=repr(exc))
    finally:
        store.clients.discard(websocket)
        debug.websocket("disconnected", clients=len(store.clients))
