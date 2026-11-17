from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "installer" / "kairix-hdmi-launcher"
MODETEST_50 = """
Connectors:
32 31 connected HDMI-A-2 600x340 3 31
  modes:
    #0 1920x1080 50.00 1920 2448 2492 2640 1080 1084 1089 1125 148500 flags: phsync, pvsync; type: preferred, driver
"""


class HdmiLauncherCase(unittest.TestCase):
    def run_launcher(self, *, connected=True, modetest=MODETEST_50, process_exit=1):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "hdmi_output.json"
            config.write_text(json.dumps({"connector": "HDMI-A-2", "mode": "1920x1080@50.00"}), encoding="utf-8")
            connector = root / "card1-HDMI-A-2"
            connector.mkdir()
            (connector / "status").write_text("connected" if connected else "disconnected", encoding="utf-8")
            completed = SimpleNamespace(returncode=0, stdout=modetest, stderr="")
            process = SimpleNamespace(poll=lambda: process_exit, returncode=process_exit,
                                      terminate=lambda: None, wait=lambda: process_exit)
            stderr = io.StringIO()
            with patch.dict(os.environ, {"KAIRIX_HDMI_CONFIG": str(config)}, clear=False), \
                 patch("shutil.which", return_value="/usr/bin/tool"), \
                 patch("pathlib.Path.glob", return_value=[connector]), \
                 patch("subprocess.run", return_value=completed), \
                 patch("subprocess.Popen", return_value=process), \
                 contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    runpy.run_path(str(LAUNCHER), run_name="__main__")
            return caught.exception.code, stderr.getvalue()

    def test_no_display_is_actionable(self):
        code, error = self.run_launcher(connected=False)
        self.assertEqual(code, 1)
        self.assertIn("HDMI-A-2 is disconnected", error)

    def test_unsupported_mode_is_rejected_before_chromium(self):
        code, error = self.run_launcher(modetest=MODETEST_50.replace("50.00", "60.00"))
        self.assertEqual(code, 1)
        self.assertIn("requested mode 1920x1080@50.00 was not verified", error)

    def test_chromium_or_cage_early_exit_is_actionable(self):
        code, error = self.run_launcher(process_exit=23)
        self.assertEqual(code, 1)
        self.assertIn("Cage/Chromium exited with 23", error)

    def test_service_declares_automatic_failure_recovery(self):
        service = (ROOT / "installer" / "kairix-reallines-hdmi.service").read_text(encoding="utf-8")
        self.assertIn("Restart=on-failure", service)
        self.assertIn("RestartSec=3", service)

    def test_installer_creates_default_only_when_absent_and_leaves_service_off(self):
        installer = (ROOT / "installer" / "install-hdmi-output.sh").read_text(encoding="utf-8")
        self.assertIn('if [[ ! -e "$REPO/data/hdmi_output.json" ]]', installer)
        self.assertIn("systemctl disable kairix-reallines-hdmi.service", installer)
        self.assertNotIn("systemctl enable", installer)
        self.assertNotIn("systemctl start", installer)


if __name__ == "__main__":
    unittest.main()
