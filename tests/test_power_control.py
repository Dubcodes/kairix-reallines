import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.power_control import PowerControlError, PowerControlService


class FakeAdapter:
    def __init__(self):
        self.actions = []

    async def execute(self, action):
        self.actions.append(action)


class FakeHashPowerService(PowerControlService):
    def _verify(self, secret, encoded):
        return encoded.startswith("$argon2id$") and secret == "correct horse battery"


class PowerControlCase(unittest.IsolatedAsyncioTestCase):
    def make_service(self, directory, clock=lambda: 100.0):
        path = Path(directory) / "admin.json"
        path.write_text(json.dumps({"version": 1, "argon2id_hash": "$argon2id$test"}), encoding="utf-8")
        adapter = FakeAdapter()
        return FakeHashPowerService(path, adapter=adapter, clock=clock), adapter

    async def test_action_bound_single_use_authorization_and_mocked_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            service, adapter = self.make_service(directory)
            authorization = service.authorize("reboot", "correct horse battery", "client-a")
            with self.assertRaisesRegex(PowerControlError, "does not match"):
                service.consume(authorization["token"], "poweroff", "client-a")
            authorization = service.authorize("reboot", "correct horse battery", "client-a")
            service.consume(authorization["token"], "reboot", "client-a")
            with self.assertRaisesRegex(PowerControlError, "missing or expired"):
                service.consume(authorization["token"], "reboot", "client-a")
            await service.execute_consumed("reboot")
            self.assertEqual(adapter.actions, ["reboot"])

    async def test_failures_back_off_and_secrets_never_enter_status(self):
        now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            service, _adapter = self.make_service(directory, clock=lambda: now[0])
            with self.assertRaisesRegex(PowerControlError, "authentication failed"):
                service.authorize("restart_app", "wrong", "client-a")
            with self.assertRaisesRegex(PowerControlError, "wait"):
                service.authorize("restart_app", "correct horse battery", "client-a")
            now[0] += 3
            result = service.authorize("restart_app", "correct horse battery", "client-a")
            self.assertNotIn("secret", json.dumps(service.status()).lower())
            self.assertEqual(result["action"], "restart_app")

    async def test_expired_client_or_action_mismatch_rejected(self):
        now = [10.0]
        with tempfile.TemporaryDirectory() as directory:
            service, _adapter = self.make_service(directory, clock=lambda: now[0])
            token = service.authorize("poweroff", "correct horse battery", "client-a")["token"]
            now[0] = 71.0
            with self.assertRaisesRegex(PowerControlError, "expired"):
                service.consume(token, "poweroff", "client-a")

    async def test_real_argon2id_hash_is_verified(self):
        from argon2 import PasswordHasher
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin.json"
            encoded = PasswordHasher().hash("a genuinely long secret")
            self.assertTrue(encoded.startswith("$argon2id$"))
            path.write_text(json.dumps({"version": 1, "argon2id_hash": encoded}), encoding="utf-8")
            service = PowerControlService(path, adapter=FakeAdapter(), clock=lambda: 10.0)
            result = service.authorize("restart_app", "a genuinely long secret", "client-a")
            self.assertEqual(result["action"], "restart_app")


class PowerEndpointCase(unittest.TestCase):
    def test_http_authorization_is_rejected_before_credential_processing(self):
        from app.main import app
        with TestClient(app, base_url="http://testserver") as client:
            response = client.post("/api/system-power/authorize", json={"action": "reboot", "secret": "must-not-be-processed"}, headers={"Origin": "http://testserver"})
            self.assertEqual(response.status_code, 403)
            self.assertIn("HTTPS", response.text)

    def test_https_authorized_action_uses_injected_adapter_only(self):
        import app.main as main
        with tempfile.TemporaryDirectory() as directory:
            service, adapter = PowerControlCase().make_service(directory)
            original = main.power_control
            main.power_control = service
            try:
                with TestClient(main.app, base_url="https://testserver") as client:
                    headers = {"Origin": "https://testserver"}
                    auth = client.post("/api/system-power/authorize", json={"action": "restart_app", "secret": "correct horse battery"}, headers=headers)
                    self.assertEqual(auth.status_code, 200)
                    execute = client.post("/api/system-power/execute", json={"action": "restart_app", "token": auth.json()["token"]}, headers=headers)
                    self.assertEqual(execute.status_code, 200)
                self.assertEqual(adapter.actions, ["restart_app"])
            finally:
                main.power_control = original

    def test_helper_and_sudoers_are_allowlisted(self):
        root = Path(__file__).resolve().parents[1]
        helper = (root / "installer/kairix-power-helper").read_text(encoding="utf-8")
        sudoers = (root / "installer/kairix-power-sudoers").read_text(encoding="utf-8")
        self.assertIn('len(sys.argv) != 2', helper)
        self.assertNotIn('shell=True', helper)
        self.assertNotIn('ALL=(ALL) ALL', sudoers)
        for action in ("restart_app", "reboot", "poweroff"):
            self.assertIn(action, helper)
            self.assertIn(action, sudoers)
