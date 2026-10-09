from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DebugRecorder:
    """Heavy, append-only debugging intended for development and field diagnostics."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.debug_root = data_dir / "debug"
        self.debug_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.session_dir = self.debug_root / stamp
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.enabled = True
        self.started_monotonic = time.monotonic()
        self.write_system_snapshot()

    def _record(self, filename: str, event: str, payload: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        row = {
            "wall_time": datetime.now(timezone.utc).isoformat(),
            "mono": round(time.monotonic(), 9),
            "event": event,
            "payload": payload or {},
        }
        path = self.session_dir / filename
        with self._lock, path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")

    def app(self, event: str, **payload: Any) -> None:
        self._record("application.jsonl", event, payload)

    def input(self, event: str, **payload: Any) -> None:
        self._record("inputs.jsonl", event, payload)

    def tracking(self, event: str, **payload: Any) -> None:
        self._record("tracking.jsonl", event, payload)

    def websocket(self, event: str, **payload: Any) -> None:
        self._record("websocket.jsonl", event, payload)

    def frontend(self, event: str, **payload: Any) -> None:
        self._record("frontend.jsonl", event, payload)

    def error(self, event: str, **payload: Any) -> None:
        self._record("errors.jsonl", event, payload)

    def write_system_snapshot(self) -> None:
        def command(args: list[str]) -> str | None:
            try:
                return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=2).strip()
            except Exception:
                return None

        snapshot = {
            "created": datetime.now(timezone.utc).isoformat(),
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "kernel": platform.release(),
            "os_release": command(["cat", "/etc/os-release"]),
            "ip_addresses": command(["hostname", "-I"]),
            "uptime": command(["uptime", "-p"]),
            "memory": command(["free", "-h"]),
            "disk": shutil.disk_usage(self.data_dir)._asdict(),
            "env_data_dir": os.getenv("KAIRIX_DATA_DIR"),
        }
        (self.session_dir / "system.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    def create_bundle(self, extra_files: list[Path]) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bundle = self.data_dir / f"Kairix-RealLines-Diagnostics-{stamp}.zip"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
            if self.session_dir.exists():
                for path in self.session_dir.rglob("*"):
                    if path.is_file():
                        zf.write(path, arcname=f"debug/{path.relative_to(self.session_dir)}")
            for path in extra_files:
                if path.exists() and path.is_dir():
                    for child in path.rglob("*"):
                        if child.is_file():
                            zf.write(child, arcname=f"state/{path.name}/{child.relative_to(path)}")
                elif path.exists() and path.is_file():
                    zf.write(path, arcname=f"state/{path.name}")
        self.app("diagnostic_bundle_created", path=str(bundle))
        return bundle
