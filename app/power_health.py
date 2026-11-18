from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from copy import deepcopy
from typing import Any, Callable


THROTTLE_FLAGS = {
    0: "under_voltage_now",
    1: "frequency_capped_now",
    2: "throttled_now",
    3: "soft_temperature_limit_now",
    16: "under_voltage_since_boot",
    17: "frequency_capped_since_boot",
    18: "throttled_since_boot",
    19: "soft_temperature_limit_since_boot",
}


def parse_throttled(output: str) -> dict[str, Any]:
    match = re.fullmatch(r"\s*throttled=(0x[0-9a-fA-F]+|\d+)\s*", str(output))
    if not match:
        raise ValueError("Unexpected vcgencmd get_throttled response")
    value = int(match.group(1), 0)
    flags = {name: bool(value & (1 << bit)) for bit, name in THROTTLE_FLAGS.items()}
    return {"raw": f"0x{value:x}", **flags}


class PowerHealthService:
    """Cached, read-only Pi power status. It never clears flags or reboots."""

    def __init__(self, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
                 clock: Callable[[], float] = time.monotonic, ttl: float = 10.0) -> None:
        self.runner = runner
        self.clock = clock
        self.ttl = max(1.0, float(ttl))
        self._lock = threading.Lock()
        self._cache: tuple[float, dict[str, Any]] | None = None

    def status(self, *, refresh: bool = False) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            if not refresh and self._cache and now - self._cache[0] < self.ttl:
                return deepcopy(self._cache[1])
            result: dict[str, Any] = {
                "available": False,
                "interpretation": "*_now flags describe the current condition; *_since_boot flags are historical and are not cleared by RealLines.",
            }
            if shutil.which("vcgencmd") is not None:
                try:
                    command = self.runner(["vcgencmd", "get_throttled"], capture_output=True,
                                          text=True, timeout=2, check=False)
                    if command.returncode == 0:
                        result.update(parse_throttled(command.stdout))
                        result["available"] = True
                    else:
                        result["error"] = (command.stderr or command.stdout or "vcgencmd failed").strip()[:200]
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    result["error"] = str(exc)[:200]
            else:
                result["error"] = "vcgencmd is unavailable on this host"
            self._cache = (self.clock(), result)
            return deepcopy(result)
