import os
import tempfile
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
