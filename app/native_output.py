from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


UNIT = "kairix-reallines-native.service"
ACTIONS = {"start", "stop", "restart", "enable", "disable", "reset-failed"}


class NativeOutputError(RuntimeError):
    pass


class NativeOutputService:
    def __init__(self, *, helper: str | None = None,
                 diagnostics: Path = Path("/run/kairix-native/renderer-diagnostics.json"),
                 runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
                 clock=time.monotonic) -> None:
        self.helper = helper or os.getenv("KAIRIX_NATIVE_HELPER", "/usr/local/sbin/kairix-native-helper")
        self.diagnostics = Path(diagnostics)
        self.runner = runner
        self.clock = clock
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    def _systemctl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        command = ["systemctl", *arguments]
        try:
            return self.runner(command, text=True, capture_output=True, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(command, 127, "", str(exc))

    def status(self, refresh: bool = False) -> dict[str, Any]:
        now = self.clock()
        if not refresh and self._cached is not None and now - self._cached_at < 2.0:
            return dict(self._cached)
        shown = self._systemctl("show", UNIT, "--property=LoadState,ActiveState,SubState,UnitFileState,Result")
        properties = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line) if shown.returncode == 0 else {}
        load, active, sub = properties.get("LoadState", ""), properties.get("ActiveState", ""), properties.get("SubState", "")
        enabled, result = properties.get("UnitFileState", ""), properties.get("Result", "")
        diagnostics = None
        if self.diagnostics.exists() and self.diagnostics.stat().st_size <= 1_000_000:
            try:
                diagnostics = json.loads(self.diagnostics.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                diagnostics = {"error": "renderer diagnostics are unreadable"}
        browser = self._systemctl("is-active", "kairix-reallines-hdmi.service")
        status = {
            "installed": load == "loaded", "active": active == "active", "state": sub or active or "unknown",
            "enabled": enabled == "enabled", "result": result or None,
            "helper_available": Path(self.helper).exists(), "diagnostics": diagnostics,
            "browser_hdmi_active": browser.returncode == 0 and browser.stdout.strip() == "active",
            "exclusive": not (active == "active" and browser.returncode == 0 and browser.stdout.strip() == "active"),
        }
        self._cached, self._cached_at = status, now
        return dict(status)

    def action(self, action: str) -> dict[str, Any]:
        if action not in ACTIONS:
            raise NativeOutputError("unsupported native output action")
        if not Path(self.helper).exists():
            raise NativeOutputError("native output helper is not installed")
        result = self.runner(["sudo", "-n", self.helper, action], text=True,
                             capture_output=True, timeout=15, check=False)
        if result.returncode:
            raise NativeOutputError((result.stderr or result.stdout or "native output action failed").strip())
        self._cached = None
        return self.status(refresh=True)
