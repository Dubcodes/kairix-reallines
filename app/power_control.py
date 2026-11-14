from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

ACTIONS = {"restart_app", "reboot", "poweroff"}
DEFAULT_CONFIG = Path("/etc/kairix-reallines/admin.json")
DEFAULT_HELPER = "/usr/local/libexec/kairix-power-helper"


class PowerControlError(ValueError):
    pass


class CommandAdapter(Protocol):
    async def execute(self, action: str) -> None: ...


class RestrictedHelperAdapter:
    async def execute(self, action: str) -> None:
        if action not in ACTIONS:
            raise PowerControlError("Unsupported system-power action")
        helper = os.getenv("KAIRIX_POWER_HELPER", DEFAULT_HELPER)
        process = await asyncio.create_subprocess_exec(
            "sudo", "-n", helper, action,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(f"System-power helper failed ({process.returncode}): {stderr.decode('utf-8', 'replace')[:200]}")


@dataclass
class Authorization:
    action: str
    client_id: str
    expires_at: float


class PowerControlService:
    def __init__(self, config_path: Path | None = None, adapter: CommandAdapter | None = None,
                 clock=time.monotonic) -> None:
        configured = config_path or Path(os.getenv("KAIRIX_ADMIN_CONFIG", DEFAULT_CONFIG))
        self.config_path = configured
        self.adapter = adapter or RestrictedHelperAdapter()
        self.clock = clock
        self._tokens: dict[str, Authorization] = {}
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}
        self._operation_pending = False

    def status(self) -> dict[str, Any]:
        return {"configured": self.config_path.is_file(), "https_required": True,
                "actions": sorted(ACTIONS), "operation_pending": self._operation_pending}

    def _hash(self) -> str:
        if not self.config_path.is_file():
            raise PowerControlError("Secure administration is not configured on this Pi")
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
            encoded = str(value["argon2id_hash"])
        except Exception as exc:
            raise PowerControlError("Secure administration configuration is invalid") from exc
        if not encoded.startswith("$argon2id$"):
            raise PowerControlError("Administrator credential is not an Argon2id hash")
        return encoded

    def _verify(self, secret: str, encoded: str) -> bool:
        try:
            from argon2 import PasswordHasher
            from argon2.exceptions import VerifyMismatchError
        except ImportError as exc:
            raise PowerControlError("Argon2 support is not installed") from exc
        try:
            return PasswordHasher().verify(encoded, secret)
        except VerifyMismatchError:
            return False

    def authorize(self, action: str, secret: str, client_id: str) -> dict[str, Any]:
        if action not in ACTIONS:
            raise PowerControlError("Unsupported system-power action")
        if self._operation_pending:
            raise PowerControlError("Another system-power operation is already pending")
        now = self.clock()
        if now < self._locked_until.get(client_id, 0):
            wait = max(1, int(self._locked_until[client_id] - now + .999))
            raise PowerControlError(f"Too many failed attempts; wait {wait} seconds")
        failures = self._failures[client_id]
        while failures and failures[0] < now - 900:
            failures.popleft()
        if not isinstance(secret, str) or not secret or not self._verify(secret, self._hash()):
            failures.append(now)
            delay = min(300, 2 ** min(len(failures), 8))
            self._locked_until[client_id] = now + delay
            raise PowerControlError("Administrator authentication failed")
        failures.clear();self._locked_until.pop(client_id, None)
        token = secrets.token_urlsafe(32)
        self._tokens[token] = Authorization(action=action, client_id=client_id, expires_at=now + 60)
        return {"token": token, "action": action, "expires_in_seconds": 60}

    def consume(self, token: str, action: str, client_id: str) -> None:
        authorization = self._tokens.pop(str(token), None)
        now = self.clock()
        if authorization is None or authorization.expires_at < now:
            raise PowerControlError("System-power authorization is missing or expired")
        if authorization.action != action or authorization.client_id != client_id:
            raise PowerControlError("System-power authorization does not match this action or client")
        if self._operation_pending:
            raise PowerControlError("Another system-power operation is already pending")
        self._operation_pending = True

    async def execute_consumed(self, action: str) -> None:
        try:
            await self.adapter.execute(action)
        except Exception:
            self._operation_pending = False
            raise


def provision_admin_config(path: Path | None = None) -> Path:
    import getpass
    try:
        from argon2 import PasswordHasher
    except ImportError as exc:
        raise SystemExit("Install requirements first; argon2-cffi is required") from exc
    destination = path or Path(os.getenv("KAIRIX_ADMIN_CONFIG", DEFAULT_CONFIG))
    first = getpass.getpass("New RealLines administrator secret: ")
    second = getpass.getpass("Confirm administrator secret: ")
    if first != second:
        raise SystemExit("Secrets did not match")
    if len(first) < 12:
        raise SystemExit("Administrator secret must contain at least 12 characters")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "argon2id_hash": PasswordHasher().hash(first)}, indent=2), encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(destination)
    return destination


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Provision RealLines secure administration")
    parser.add_argument("command", choices=["provision"])
    parser.add_argument("--config", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "provision":
        print(f"Administrator configuration written to {provision_admin_config(arguments.config)}")
