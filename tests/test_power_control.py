import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.power_control import MutationGate, PowerControlError, PowerControlService, provision_admin_config


class FakeAdapter:
    def __init__(self):
        self.actions = []

    async def execute(self, action):
        self.actions.append(action)

    def available(self):
        return True


class FailingAdapter(FakeAdapter):
    async def execute(self, action):
        raise RuntimeError("allowlisted helper unavailable")


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

    async def test_configuration_states_and_failed_helper_recover_pending_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin.json"
            service = PowerControlService(path, adapter=FailingAdapter())
            self.assertEqual(service.status()["credential_status"], "not_configured")
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(service.status()["credential_status"], "malformed")
            path.write_text(json.dumps({"argon2id_hash": "$argon2id$test"}), encoding="utf-8")
            self.assertEqual(service.status()["credential_status"], "usable")
            failed = FakeHashPowerService(path, adapter=FailingAdapter())
            token = failed.authorize("restart_app", "correct horse battery", "client-a")["token"]
            failed.consume(token, "restart_app", "client-a")
            with self.assertRaisesRegex(RuntimeError, "helper unavailable"):
                await failed.execute_consumed("restart_app")
            status = failed.status()
            self.assertFalse(status["operation_pending"])
            self.assertEqual(status["last_operation"]["status"], "failed")
            self.assertNotIn("correct horse", json.dumps(status))

    async def test_mutation_gate_drains_and_reopens_after_failure(self):
        gate = MutationGate()
        entered = asyncio.Event()
        release = asyncio.Event()
        async def mutation():
            async with gate.mutation():
                entered.set()
                await release.wait()
        active = asyncio.create_task(mutation())
        await entered.wait()
        closing = asyncio.create_task(gate.begin_transition())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await active
        await closing
        with self.assertRaisesRegex(PowerControlError, "temporarily disabled"):
            async with gate.mutation():
                pass
        await gate.reopen()
        async with gate.mutation():
            pass

    def test_systemd_credential_resolution_and_provisioned_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            credential_directory = Path(directory) / "credentials"
            credential_directory.mkdir()
            with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(credential_directory)}, clear=False):
                service = PowerControlService(adapter=FakeAdapter())
            self.assertEqual(service.config_path, credential_directory / "kairix-admin.json")
            destination = Path(directory) / "admin.json"
            with patch("getpass.getpass", side_effect=["a sufficiently long secret", "a sufficiently long secret"]):
                provision_admin_config(destination)
            if os.name != "nt":
                self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_unreadable_credential_is_distinct_from_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin.json"
            path.write_text(json.dumps({"argon2id_hash": "$argon2id$test"}), encoding="utf-8")
            service = PowerControlService(path, adapter=FakeAdapter())
            with patch.object(Path, "read_text", side_effect=PermissionError):
                status = service.status()
            self.assertEqual(status["credential_status"], "unreadable")
            self.assertFalse(status["configured"])

    def test_usable_credential_does_not_imply_helper_availability(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin.json"
            path.write_text(json.dumps({"argon2id_hash": "$argon2id$test"}), encoding="utf-8")
            with patch.dict(os.environ, {"KAIRIX_POWER_HELPER": str(Path(directory) / "missing-helper")}, clear=False):
                status = PowerControlService(path).status()
            self.assertTrue(status["configured"])
            self.assertFalse(status["helper_available"])


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
            original_gate = main.mutation_gate
            main.power_control = service
            main.mutation_gate = MutationGate()
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
                main.mutation_gate = original_gate

    def test_forwarded_transport_and_mismatched_origin_are_rejected(self):
        from app.main import app
        with TestClient(app, base_url="http://testserver") as client:
            response = client.post("/api/system-power/authorize", json={"action": "reboot", "secret": "ignored"},
                                   headers={"Origin": "https://testserver", "X-Forwarded-Proto": "https"})
            self.assertEqual(response.status_code, 403)
        with TestClient(app, base_url="https://testserver") as client:
            for headers in (
                {"Origin": "https://testserver", "Forwarded": "proto=https"},
                {"Origin": "https://testserver", "X-Forwarded-Proto": "https"},
                {"Origin": "https://evil.example", "Host": "testserver"},
                {"Origin": "https://testserver", "Host": "evil.example"},
            ):
                response = client.post("/api/system-power/authorize", json={"action": "reboot", "secret": "ignored"}, headers=headers)
                self.assertEqual(response.status_code, 403)

    def test_failed_helper_reopens_mutations_and_surfaces_status(self):
        import app.main as main
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin.json"
            path.write_text(json.dumps({"argon2id_hash": "$argon2id$test"}), encoding="utf-8")
            service = FakeHashPowerService(path, adapter=FailingAdapter())
            original_service, original_gate = main.power_control, main.mutation_gate
            main.power_control, main.mutation_gate = service, MutationGate()
            try:
                with TestClient(main.app, base_url="https://testserver") as client:
                    headers = {"Origin": "https://testserver"}
                    auth = client.post("/api/system-power/authorize", json={"action": "restart_app", "secret": "correct horse battery"}, headers=headers)
                    response = client.post("/api/system-power/execute", json={"action": "restart_app", "token": auth.json()["token"]}, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    status = client.get("/api/system-power/status").json()
                    self.assertFalse(status["operation_pending"])
                    self.assertFalse(status["mutations_blocked"])
                    self.assertEqual(status["last_operation"]["status"], "failed")
                    self.assertNotEqual(client.post("/api/camera", json={"pan": 1}).status_code, 503)
            finally:
                main.power_control, main.mutation_gate = original_service, original_gate

    def test_helper_and_sudoers_are_allowlisted(self):
        root = Path(__file__).resolve().parents[1]
        helper = (root / "installer/kairix-power-helper").read_text(encoding="utf-8")
        sudoers = (root / "installer/kairix-power-sudoers").read_text(encoding="utf-8")
        credential_dropin = (root / "installer/kairix-power-credentials.conf").read_text(encoding="utf-8")
        dev_service_name = (root / "installer/kairix-power-service-dev").read_text(encoding="utf-8").strip()
        production_service_name = (root / "installer/kairix-power-service-production").read_text(encoding="utf-8").strip()
        production_service = (root / "installer/kairix-reallines.service").read_text(encoding="utf-8")
        development_installer = (root / "installer/install-dev.sh").read_text(encoding="utf-8")
        self.assertIn('len(sys.argv) != 2', helper)
        self.assertNotIn('shell=True', helper)
        self.assertNotIn('ALL=(ALL) ALL', sudoers)
        self.assertIn('ALLOWED_SERVICES', helper)
        self.assertIn('service-name', helper)
        self.assertEqual({dev_service_name, production_service_name}, {"kairix-reallines-dev.service", "kairix-reallines.service"})
        self.assertIn('LoadCredential=kairix-admin.json:', credential_dropin)
        self.assertIn('--no-proxy-headers', production_service)
        self.assertIn('--no-proxy-headers', development_installer)
        self.assertIn('kairix ALL=(root)', sudoers)
        for action in ("restart_app", "reboot", "poweroff"):
            self.assertIn(action, helper)
            self.assertIn(action, sudoers)
