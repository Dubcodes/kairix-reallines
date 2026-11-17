import os
import tempfile
import time
import asyncio
from unittest.mock import patch
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.hdmi_output import DEFAULT_CONFIG, HdmiOutputError, HdmiOutputService, discover_connectors, parse_modetest_modes


MODETEST = """
Connectors:
id encoder status name size (mm) modes encoders
32 31 connected HDMI-A-2 600x340 3 31
  modes:
    #0 1920x1080 50.00 1920 2448 2492 2640 1080 1084 1089 1125 148500 flags: phsync, pvsync; type: preferred, driver
    #1 1920x1080 60.00 1920 2008 2052 2200 1080 1084 1089 1125 148500 flags: phsync, pvsync; type: driver
40 39 disconnected HDMI-A-1 0x0 0 39
  modes:
"""


class HdmiOutputCase(unittest.TestCase):
    def test_slow_hdmi_commands_do_not_stall_200hz_or_50hz_event_loop_work(self):
        from app import main

        async def scenario():
            stamps = {200: [], 50: []}
            async def ticker(rate, duration=.10):
                interval = 1 / rate
                end = asyncio.get_running_loop().time() + duration
                while asyncio.get_running_loop().time() < end:
                    stamps[rate].append(asyncio.get_running_loop().time())
                    await asyncio.sleep(interval)
            status = {"installed": False, "active": False, "state": "unavailable",
                      "enabled": False, "exit_status": None, "helper_available": False,
                      "config": DEFAULT_CONFIG, "connectors": [], "selected_connected": False,
                      "selected_mode_verified": False, "actual_mode": None, "displayed_fps": None,
                      "application_connected": False}
            def slow_status(*, refresh=False):
                time.sleep(.30)
                return dict(status)
            with patch.object(main.hdmi_output, "status", side_effect=slow_status):
                hdmi = asyncio.create_task(main._hdmi_status())
                await asyncio.gather(ticker(200), ticker(50))
                # Both simulated loops made progress while the blocking command
                # was still sleeping in the dedicated worker.
                self.assertFalse(hdmi.done())
                await hdmi
            return stamps

        stamps = asyncio.run(scenario())
        self.assertGreaterEqual(len(stamps[200]), 3)
        self.assertGreaterEqual(len(stamps[50]), 2)

    def test_concurrent_status_requests_share_one_blocking_operation(self):
        from app import main
        calls = 0
        status = {"installed": False, "active": False, "state": "unavailable",
                  "enabled": False, "exit_status": None, "helper_available": False,
                  "config": DEFAULT_CONFIG, "connectors": [], "selected_connected": False,
                  "selected_mode_verified": False, "actual_mode": None, "displayed_fps": None,
                  "application_connected": False}
        def slow_status(*, refresh=False):
            nonlocal calls
            calls += 1
            time.sleep(.05)
            return dict(status)
        async def scenario():
            with patch.object(main.hdmi_output, "status", side_effect=slow_status):
                await asyncio.gather(*(main._hdmi_status() for _ in range(8)))
        asyncio.run(scenario())
        self.assertEqual(calls, 1)
    def test_discovery_and_system_status_are_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            def runner(args, **_kwargs):
                calls.append(args[0])
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            service = HdmiOutputService(Path(directory), runner=runner)
            service._modetest = lambda: calls.append("modetest") or MODETEST
            service.status()
            service.status()
            self.assertEqual(calls.count("modetest"), 1)
            self.assertEqual(calls.count("systemctl"), 1)

    def test_explicit_refresh_repeats_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            service = HdmiOutputService(Path(directory))
            calls = []
            service._modetest = lambda: calls.append(True) or MODETEST
            service.connectors()
            service.connectors()
            service.refresh_connectors()
            self.assertEqual(len(calls), 2)
    def test_modetest_parser_preserves_exact_refresh_modes(self):
        modes = parse_modetest_modes(MODETEST)
        self.assertEqual(modes["HDMI-A-2"], ["1920x1080@50.00", "1920x1080@60.00"])
        self.assertEqual(modes["HDMI-A-1"], [])

    def test_sysfs_discovery_reports_connection_card_and_verified_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connector = root / "card1-HDMI-A-2"
            connector.mkdir()
            (connector / "status").write_text("connected\n")
            (connector / "modes").write_text("1920x1080\n1280x720\n")
            result = discover_connectors(root, MODETEST)
            self.assertEqual(result[0]["name"], "HDMI-A-2")
            self.assertEqual(result[0]["card"], "card1")
            self.assertTrue(result[0]["connected"])
            self.assertIn(DEFAULT_CONFIG["mode"], result[0]["modes"])

    def test_settings_accept_only_discovered_exact_modes_and_persist_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            service = HdmiOutputService(Path(directory))
            service.connectors = lambda: [{"name": "HDMI-A-2", "connected": True,
                                            "modes": ["1920x1080@50.00"]}]
            self.assertEqual(service.update(DEFAULT_CONFIG), DEFAULT_CONFIG)
            self.assertTrue((Path(directory) / "hdmi_output.json").exists())
            with self.assertRaisesRegex(HdmiOutputError, "not verified"):
                service.update({"mode": "1920x1080@59.94"})
            with self.assertRaisesRegex(HdmiOutputError, "not discovered"):
                service.update({"connector": "HDMI-A-1"})
            self.assertEqual(service.config, DEFAULT_CONFIG)

    def test_service_actions_are_narrowly_allowlisted(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "helper"
            helper.write_text("helper")
            helper.chmod(0o755)
            calls = []

            def runner(args, **_kwargs):
                calls.append(args)
                if args[:2] == ["systemctl", "show"]:
                    return SimpleNamespace(returncode=0, stdout="LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\nExecMainStatus=0\n", stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            service = HdmiOutputService(Path(directory), runner=runner, helper=str(helper))
            service.connectors = lambda: []
            result = service.action("restart")
            self.assertTrue(result["active"])
            self.assertIn(["sudo", "-n", str(helper), "restart"], calls)
            with self.assertRaises(HdmiOutputError):
                service.action("status kairix-reallines.service")


if __name__ == "__main__":
    unittest.main()
