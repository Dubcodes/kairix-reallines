import json
import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.native_output import NativeOutputError, NativeOutputService
from unittest.mock import patch


class NativeOutputCase(unittest.TestCase):
    def test_status_distinguishes_process_from_measured_renderer(self):
        calls = []
        def run(command, **_kwargs):
            calls.append(command)
            if command[1:3] == ["show", "kairix-reallines-native.service"]:
                return subprocess.CompletedProcess(command, 0, "LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=disabled\nResult=success\n", "")
            return subprocess.CompletedProcess(command, 3, "inactive\n", "")
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = Path(directory) / "timing.json"
            diagnostics.write_text(json.dumps({"frames": 50, "display_mode": "1920x1080@50"}), encoding="utf-8")
            service = NativeOutputService(runner=run, diagnostics=diagnostics)
            status = service.status()
            self.assertTrue(status["active"])
            self.assertEqual(status["diagnostics"]["frames"], 50)
            self.assertTrue(status["exclusive"])

    def test_action_is_allowlisted(self):
        service = NativeOutputService(helper="missing-helper")
        with self.assertRaisesRegex(NativeOutputError, "unsupported"):
            service.action("shell")

    def test_service_units_are_mutually_exclusive_and_not_enabled_by_installer(self):
        root = Path(__file__).resolve().parents[1]
        native = (root / "installer/kairix-reallines-native.service").read_text(encoding="utf-8")
        browser = (root / "installer/kairix-reallines-hdmi.service").read_text(encoding="utf-8")
        installer = (root / "installer/install-native-renderer.sh").read_text(encoding="utf-8")
        self.assertIn("Conflicts=kairix-reallines-hdmi.service", native)
        self.assertIn("Conflicts=kairix-reallines-native.service", browser)
        self.assertIn("systemctl disable kairix-reallines-native.service", installer)
        self.assertNotIn("systemctl enable kairix-reallines-native.service", installer)
        self.assertIn("exact 1920x1080@50", (root / "native/src/main.cpp").read_text(encoding="utf-8"))


class NativeOutputAsyncCase(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_engineering_status_requests_are_singleflight(self):
        from app import main
        calls = 0
        def slow_status():
            nonlocal calls
            calls += 1
            import time
            time.sleep(.05)
            return {"installed": True}
        main.native_status_inflight = None
        with patch.object(main.native_output, "status", side_effect=slow_status):
            results = await asyncio.gather(*(main.native_output_status() for _ in range(6)))
        self.assertEqual(calls, 1)
        self.assertTrue(all(result["installed"] for result in results))


if __name__ == "__main__":
    unittest.main()
