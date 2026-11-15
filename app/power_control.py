from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

ACTIONS = {"restart_app", "reboot", "poweroff"}
DEFAULT_CONFIG = Path("/etc/kairix-reallines/admin.json")
DEFAULT_HELPER = "/usr/local/libexec/kairix-power-helper"
CREDENTIAL_NAME = "kairix-admin.json"


class PowerControlError(ValueError):
    pass


class CommandAdapter(Protocol):
    async def execute(self, action: str) -> None: ...
    def available(self) -> bool: ...


class RestrictedHelperAdapter:
    def available(self) -> bool:
        helper = os.getenv("KAIRIX_POWER_HELPER", DEFAULT_HELPER)
        return os.path.isfile(helper) and os.access(helper, os.X_OK)

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
        configured = self._resolve_config_path(config_path)
        self.config_path = configured
        self.adapter = adapter or RestrictedHelperAdapter()
        self.clock = clock
        self._tokens: dict[str, Authorization] = {}
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}
        self._operation_pending = False
        self._last_operation: dict[str, Any] | None = None

    @staticmethod
    def _resolve_config_path(config_path: Path | None) -> Path:
        if config_path is not None:
            return Path(config_path)
        explicit = os.getenv("KAIRIX_ADMIN_CONFIG")
        if explicit:
            return Path(explicit)
        credential_directory = os.getenv("CREDENTIALS_DIRECTORY")
        if credential_directory:
            return Path(credential_directory) / CREDENTIAL_NAME
        return DEFAULT_CONFIG

    def _credential_status(self) -> tuple[str, str]:
        try:
            exists = self.config_path.exists()
        except PermissionError:
            return "unreadable", "Administrator credential is configured but unreadable by the service"
        if not exists:
            return "not_configured", "Administrator credential is not configured"
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
        except PermissionError:
            return "unreadable", "Administrator credential is configured but unreadable by the service"
        except Exception:
            return "malformed", "Administrator credential configuration is malformed"
        encoded = value.get("argon2id_hash") if isinstance(value, dict) else None
        if not isinstance(encoded, str) or not encoded.startswith("$argon2id$"):
            return "malformed", "Administrator credential configuration is malformed"
        return "usable", "Administrator credential is usable"

    @property
    def operation_pending(self) -> bool:
        return self._operation_pending

    def status(self) -> dict[str, Any]:
        credential_status, credential_detail = self._credential_status()
        available = bool(getattr(self.adapter, "available", lambda: True)())
        return {"configured": credential_status == "usable", "credential_status": credential_status,
                "credential_detail": credential_detail, "helper_available": available,
                "https_required": True, "actions": sorted(ACTIONS),
                "operation_pending": self._operation_pending,
                "last_operation": deepcopy(self._last_operation)}

    def _hash(self) -> str:
        credential_status, detail = self._credential_status()
        if credential_status == "not_configured":
            raise PowerControlError("Secure administration is not configured on this Pi")
        if credential_status != "usable":
            raise PowerControlError(detail)
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
        self._last_operation = {"action": action, "status": "accepted", "detail": "System action accepted"}

    async def execute_consumed(self, action: str) -> None:
        try:
            await self.adapter.execute(action)
            self._last_operation = {"action": action, "status": "dispatched", "detail": "Privileged helper accepted the action"}
        except Exception as exc:
            self._operation_pending = False
            self._last_operation = {"action": action, "status": "failed", "detail": str(exc)[:200]}
            raise


class MutationGate:
    """Drain in-flight writes and reject new ones after a power action is accepted."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active = 0
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    @asynccontextmanager
    async def mutation(self):
        async with self._condition:
            if self._closing:
                raise PowerControlError("System transition is pending; changes are temporarily disabled")
            self._active += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active -= 1
                self._condition.notify_all()

    async def begin_transition(self) -> None:
        async with self._condition:
            if self._closing:
                raise PowerControlError("Another system transition is already pending")
            self._closing = True
            while self._active:
                await self._condition.wait()

    async def reopen(self) -> None:
        async with self._condition:
            self._closing = False
            self._condition.notify_all()


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
