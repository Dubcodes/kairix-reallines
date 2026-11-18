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
    def run_launcher(self, *, connected=True, modetest=MODETEST_50, process_exit=1, seat_accessible=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "hdmi_output.json"
            config.write_text(json.dumps({"connector": "HDMI-A-2", "mode": "1920x1080@50.00"}), encoding="utf-8")
            connector = root / "card1-HDMI-A-2"
            connector.mkdir()
            (connector / "status").write_text("connected" if connected else "disconnected", encoding="utf-8")
            seat_socket = root / "seatd.sock"
            seat_socket.write_text("seat", encoding="utf-8")
            completed = SimpleNamespace(returncode=0, stdout=modetest, stderr="")
            process = SimpleNamespace(poll=lambda: process_exit, returncode=process_exit,
                                      terminate=lambda: None, wait=lambda: process_exit)
            stderr = io.StringIO()
            with patch.dict(os.environ, {"KAIRIX_HDMI_CONFIG": str(config),
                                         "KAIRIX_HDMI_STATUS": str(root / "runtime.json"),
                                         "KAIRIX_HDMI_RECONNECT_WAIT_SECONDS": "0",
                                         "SEATD_SOCK": str(seat_socket)}, clear=False), \
                 patch("shutil.which", return_value="/usr/bin/tool"), \
                 patch("pathlib.Path.glob", return_value=[connector]), \
                 patch("os.access", return_value=seat_accessible), \
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

    def test_seat_socket_permission_failure_is_actionable(self):
        code, error = self.run_launcher(seat_accessible=False)
        self.assertEqual(code, 1)
        self.assertIn("seatd socket is not accessible", error)

    def test_service_declares_automatic_failure_recovery(self):
        service = (ROOT / "installer" / "kairix-reallines-hdmi.service").read_text(encoding="utf-8")
        self.assertIn("Restart=on-failure", service)
        self.assertIn("RestartSec=5", service)
        self.assertIn("StartLimitBurst=3", service)
        self.assertIn("Requires=seatd.service", service)
        self.assertIn("LIBSEAT_BACKEND=seatd", service)
        self.assertIn("SupplementaryGroups=kairix-seat render", service)

    def test_installer_creates_default_only_when_absent_and_leaves_service_off(self):
        installer = (ROOT / "installer" / "install-hdmi-output.sh").read_text(encoding="utf-8")
        self.assertIn('if [[ ! -e "$REPO/data/hdmi_output.json" ]]', installer)
        self.assertIn("apt-get install -y cage chromium wlr-randr libdrm-tests seatd", installer)
        self.assertIn("systemctl enable --now seatd.service", installer)
        self.assertIn("kairix-seat", installer)
        self.assertIn("systemctl disable kairix-reallines-hdmi.service", installer)
        self.assertNotIn("systemctl enable kairix-reallines-hdmi.service", installer)
        self.assertNotIn("systemctl start", installer)


if __name__ == "__main__":
    unittest.main()
