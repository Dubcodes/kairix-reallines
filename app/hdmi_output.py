from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable


CONNECTOR_RE = re.compile(r"^card\d+-(HDMI-A-\d+)$")
MODE_RE = re.compile(r"^(\d{3,5})x(\d{3,5})@([1-9]\d{0,2}(?:\.\d{1,3})?)$")
ACTIONS = {"start", "stop", "restart", "enable", "disable"}
UNIT = "kairix-reallines-hdmi.service"
DEFAULT_CONFIG = {"connector": "HDMI-A-2", "mode": "1920x1080@50.00"}


def parse_modetest_modes(output: str) -> dict[str, list[str]]:
    """Extract exact connector refresh modes from libdrm modetest output."""
    result: dict[str, list[str]] = {}
    connector: str | None = None
    header = re.compile(r"^\s*\d+\s+\d+\s+(?:connected|disconnected)\s+(HDMI-A-\d+)\b")
    mode = re.compile(r"^\s*#\d+\s+(\d+x\d+)\s+([0-9]+(?:\.[0-9]+)?)\b")
    for line in output.splitlines():
        match = header.match(line)
        if match:
            connector = match.group(1)
            result.setdefault(connector, [])
            continue
        match = mode.match(line)
        if connector and match:
            value = f"{match.group(1)}@{float(match.group(2)):.2f}"
            if value not in result[connector]:
                result[connector].append(value)
    return result


def discover_connectors(sysfs_root: Path = Path("/sys/class/drm"),
                        modetest_output: str = "") -> list[dict[str, Any]]:
    exact_modes = parse_modetest_modes(modetest_output)
    connectors: list[dict[str, Any]] = []
    if not sysfs_root.exists():
        return connectors
    for path in sorted(sysfs_root.glob("card*-HDMI-A-*")):
        match = CONNECTOR_RE.fullmatch(path.name)
        if not match:
            continue
        name = match.group(1)
        try:
            connected = (path / "status").read_text(encoding="utf-8").strip() == "connected"
        except OSError:
            connected = False
        resolutions: list[str] = []
        try:
            resolutions = [line.strip() for line in (path / "modes").read_text(encoding="utf-8").splitlines()
                           if re.fullmatch(r"\d+x\d+", line.strip())]
        except OSError:
            pass
        connectors.append({
            "name": name, "card": path.name.split("-", 1)[0], "connected": connected,
            "modes": exact_modes.get(name, []), "resolutions": list(dict.fromkeys(resolutions)),
            "exact_refresh_available": bool(exact_modes.get(name)),
        })
    return connectors


class HdmiOutputError(ValueError):
    pass


class HdmiOutputService:
    def __init__(self, data_dir: Path, *, sysfs_root: Path = Path("/sys/class/drm"),
                 runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
                 helper: str | None = None) -> None:
        self.path = Path(data_dir) / "hdmi_output.json"
        self.sysfs_root = sysfs_root
        self.runner = runner
        self.helper = helper or os.getenv("KAIRIX_HDMI_HELPER", "/usr/local/sbin/kairix-hdmi-helper")
        self.config = self._load()

    def _load(self) -> dict[str, str]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return self._normalise(value)
        except FileNotFoundError:
            return deepcopy(DEFAULT_CONFIG)
        except (OSError, json.JSONDecodeError, HdmiOutputError):
            return deepcopy(DEFAULT_CONFIG)

    @staticmethod
    def _normalise(value: dict[str, Any]) -> dict[str, str]:
        connector = str(value.get("connector") or DEFAULT_CONFIG["connector"])
        mode = str(value.get("mode") or DEFAULT_CONFIG["mode"])
        if not re.fullmatch(r"HDMI-A-\d+", connector):
            raise HdmiOutputError("HDMI connector is invalid")
        if not MODE_RE.fullmatch(mode):
            raise HdmiOutputError("HDMI mode must use WIDTHxHEIGHT@HZ")
        return {"connector": connector, "mode": mode}

    def _modetest(self) -> str:
        if shutil.which("modetest") is None:
            return ""
        try:
            result = self.runner(["modetest", "-M", "vc4", "-c"], capture_output=True,
                                 text=True, timeout=3, check=False)
            return result.stdout if result.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def connectors(self) -> list[dict[str, Any]]:
        return discover_connectors(self.sysfs_root, self._modetest())

    def update(self, values: dict[str, Any]) -> dict[str, str]:
        unknown = set(values) - {"connector", "mode"}
        if unknown:
            raise HdmiOutputError(f"Unknown HDMI output setting: {sorted(unknown)[0]}")
        candidate = self._normalise({**self.config, **values})
        connectors = self.connectors()
        connector = next((item for item in connectors if item["name"] == candidate["connector"]), None)
        if connector is None:
            raise HdmiOutputError("Selected HDMI connector was not discovered")
        if candidate["mode"] not in connector["modes"]:
            raise HdmiOutputError("Selected HDMI mode was not verified by DRM/KMS")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(candidate, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self.config = candidate
        return deepcopy(candidate)

    def reset(self) -> dict[str, str]:
        return self.update(DEFAULT_CONFIG)

    def _systemd_status(self) -> dict[str, Any]:
        try:
            result = self.runner(
                ["systemctl", "show", UNIT, "--property=LoadState,ActiveState,SubState,UnitFileState,ExecMainStatus"],
                capture_output=True, text=True, timeout=3, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {"installed": False, "active": False, "state": "unavailable", "enabled": False, "exit_status": None}
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        return {
            "installed": values.get("LoadState") == "loaded",
            "active": values.get("ActiveState") == "active",
            "state": values.get("SubState") or values.get("ActiveState") or "unknown",
            "enabled": values.get("UnitFileState") == "enabled",
            "exit_status": int(values["ExecMainStatus"]) if values.get("ExecMainStatus", "").isdigit() else None,
        }

    def status(self) -> dict[str, Any]:
        connectors = self.connectors()
        selected = next((item for item in connectors if item["name"] == self.config["connector"]), None)
        helper_available = Path(self.helper).is_file() and os.access(self.helper, os.X_OK)
        return {
            **self._systemd_status(), "helper_available": helper_available,
            "config": deepcopy(self.config), "connectors": connectors,
            "selected_connected": bool(selected and selected["connected"]),
            "selected_mode_verified": bool(selected and self.config["mode"] in selected["modes"]),
            "actual_mode": None, "displayed_fps": None,
            "application_connected": False,
            "limitations": "Actual presentation FPS and display latency require compositor/browser telemetry and physical capture validation.",
        }

    def action(self, action: str) -> dict[str, Any]:
        if action not in ACTIONS:
            raise HdmiOutputError("Unsupported HDMI output action")
        if not (Path(self.helper).is_file() and os.access(self.helper, os.X_OK)):
            raise HdmiOutputError("Privileged HDMI output helper is unavailable")
        try:
            result = self.runner(["sudo", "-n", self.helper, action], capture_output=True,
                                 text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HdmiOutputError("HDMI output helper could not be executed") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "helper failed").strip()[:240]
            raise HdmiOutputError(f"HDMI output action failed: {detail}")
        return self.status()
