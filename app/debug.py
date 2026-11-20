from __future__ import annotations

import json
import os
import platform
import atexit
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections import deque


class DebugRecorder:
    """Heavy, append-only debugging intended for development and field diagnostics."""

    def __init__(self, data_dir: Path, *, queue_capacity: int = 4096,
                 flush_interval: float = 1.0, max_file_bytes: int = 2_000_000) -> None:
        self.data_dir = data_dir
        self.debug_root = data_dir / "debug"
        self.debug_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.session_dir = self.debug_root / stamp
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._file_lock = threading.Lock()
        self._pending: deque[tuple[str, str]] = deque(maxlen=max(64, int(queue_capacity)))
        self._flush_interval = max(0.1, float(flush_interval))
        self._max_file_bytes = max(64_000, int(max_file_bytes))
        self._dropped_events = 0
        self._written_events = 0
        self._stopping = False
        self.enabled = True
        self.started_monotonic = time.monotonic()
        self.write_system_snapshot()
        self._thread = threading.Thread(target=self._flush_loop, name="reallines-debug-flush", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def _record(self, filename: str, event: str, payload: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        row = {
            "wall_time": datetime.now(timezone.utc).isoformat(),
            "mono": round(time.monotonic(), 9),
            "event": event,
            "payload": payload or {},
        }
        encoded = json.dumps(row, separators=(",", ":"), default=str) + "\n"
        if filename == "errors.jsonl":
            self._write_rows([(filename, encoded)])
            return
        with self._condition:
            if len(self._pending) == self._pending.maxlen:
                self._pending.popleft()
                self._dropped_events += 1
            self._pending.append((filename, encoded))
            if len(self._pending) >= 256:
                self._condition.notify()

    def _rotate_if_needed(self, path: Path, incoming_bytes: int) -> None:
        if not path.exists() or path.stat().st_size + incoming_bytes <= self._max_file_bytes:
            return
        oldest = path.with_name(path.name + ".3")
        oldest.unlink(missing_ok=True)
        for number in (2, 1):
            source = path.with_name(path.name + f".{number}")
            if source.exists():
                source.replace(path.with_name(path.name + f".{number + 1}"))
        path.replace(path.with_name(path.name + ".1"))

    def _write_rows(self, rows: list[tuple[str, str]]) -> None:
        if not rows:
            return
        grouped: dict[str, list[str]] = {}
        for filename, encoded in rows:
            grouped.setdefault(filename, []).append(encoded)
        with self._file_lock:
            for filename, encoded_rows in grouped.items():
                path = self.session_dir / filename
                payload = "".join(encoded_rows)
                self._rotate_if_needed(path, len(payload.encode("utf-8")))
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(payload)
                self._written_events += len(encoded_rows)

    def flush(self) -> None:
        with self._condition:
            rows = list(self._pending)
            self._pending.clear()
        self._write_rows(rows)

    def _flush_loop(self) -> None:
        while True:
            with self._condition:
                if not self._stopping and len(self._pending) < 256:
                    self._condition.wait(self._flush_interval)
                stopping = self._stopping
            self.flush()
            if stopping:
                return

    def close(self) -> None:
        thread = getattr(self, "_thread", None)
        if thread is None:
            return
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self.flush()
        self._thread = None

    def status(self) -> dict[str, Any]:
        with self._condition:
            pending = len(self._pending)
            dropped = self._dropped_events
        return {
            "enabled": self.enabled, "pending_events": pending,
            "dropped_events": dropped, "written_events": self._written_events,
            "flush_interval_seconds": self._flush_interval,
            "max_file_bytes": self._max_file_bytes,
            "sudden_power_loss_may_lose_pending": pending > 0,
        }

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
        self.flush()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
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
