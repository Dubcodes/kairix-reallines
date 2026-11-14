from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
    PlaywrightError = Exception


ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipIf(sync_playwright is None, "Playwright is not installed; install requirements-dev.txt")
class RealBrowserSmokeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        environment = os.environ.copy()
        environment["KAIRIX_DATA_DIR"] = cls.temporary.name
        environment["KAIRIX_ADMIN_CONFIG"] = str(Path(cls.temporary.name) / "outside-admin.json")
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(cls.port), "--log-level", "warning"],
            cwd=ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if urlopen(f"{cls.base}/api/state", timeout=.5).status == 200:
                    break
            except Exception:
                time.sleep(.1)
        else:
            cls.server.terminate();cls.server.wait(timeout=5)
            cls.temporary.cleanup()
            raise RuntimeError("Temporary RealLines browser-test server did not start")
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            cls.playwright.stop();cls.server.terminate();cls.server.wait(timeout=5);cls.temporary.cleanup()
            raise unittest.SkipTest(f"Playwright Chromium is unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "playwright"):
            cls.playwright.stop()
        if hasattr(cls, "server"):
            cls.server.terminate()
            try: cls.server.wait(timeout=5)
            except subprocess.TimeoutExpired: cls.server.kill();cls.server.wait(timeout=5)
        if hasattr(cls, "temporary"):
            cls.temporary.cleanup()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000})

    def tearDown(self):
        self.page.close()

    def test_engineering_pico_settings_persist_and_power_is_locked_on_http(self):
        page = self.page
        page.goto(f"{self.base}/engineering")
        page.wait_for_function("document.querySelector('#uiStatus').textContent !== 'Loading'")
        page.select_option("#picoProcessingMode", "every")
        page.fill("#picoEveryRate", "450")
        page.fill("#picoBufferCapacity", "128")
        page.click("#savePicoProcessing")
        page.wait_for_function("document.querySelector('#picoProcessingFeedback').textContent === 'Confirmed'")
        page.reload();page.wait_for_function("document.querySelector('#uiStatus').textContent !== 'Loading'")
        self.assertEqual(page.input_value("#picoProcessingMode"), "every", page.text_content("#uiStatus") + " " + (page.get_attribute("#uiStatus", "title") or ""))
        self.assertEqual(page.input_value("#picoEveryRate"), "450")
        self.assertTrue(page.is_disabled('[data-power-action="reboot"]'))
        self.assertIn(page.text_content("#powerSecurityStatus"), {"Secure administration is not configured on this Pi.", "HTTPS REQUIRED — power controls are disabled on this HTTP page."})

    def test_engineering_profile_creation_and_failed_action_feedback(self):
        page = self.page
        page.goto(f"{self.base}/engineering");page.wait_for_selector("#newProfile:not([disabled])")
        page.click("#newProfile");page.fill("#dialogInput", "Browser Rig");page.click("#dialogAccept")
        page.wait_for_function("document.querySelector('#profileName').value === 'Browser Rig'")
        page.route("**/api/pico/processing", lambda route: route.abort())
        page.click("#savePicoProcessing")
        page.wait_for_function("document.querySelector('#picoProcessingFeedback').textContent === 'Failed'")
        self.assertFalse(page.is_disabled("#savePicoProcessing"))

    def test_manual_dot_observation_selection_survives_camera_movement(self):
        page = self.page
        page.goto(f"{self.base}/calibration");page.wait_for_selector("#newRectangleDotTarget")
        page.click("#newRectangleDotTarget")
        page.wait_for_function("document.querySelector('#saveDotObservation').disabled === false")
        page.click("#saveDotObservation")
        page.wait_for_selector("#dotObservationList .observation-view")
        page.locator("#dotObservationList .observation-view").first.click()
        page.wait_for_selector("#dotObservationList .observation-chip.selected")
        page.evaluate("document.querySelector('#pan').value='12';document.querySelector('#pan').dispatchEvent(new Event('input',{bubbles:true}))")
        page.wait_for_timeout(200)
        self.assertEqual(page.locator("#dotObservationList .observation-chip.selected").count(), 1)

    def test_calibration_draft_survives_pose_updates_and_measured_target_round_trips(self):
        page = self.page
        page.goto(f"{self.base}/calibration")
        page.wait_for_selector("#newMeasuredTarget:not([disabled])")
        page.click("#newMeasuredTarget")
        self.assertEqual(page.locator("#measuredPairRows .measured-pair-row").count(), 6)
        page.fill("#measuredTargetName", "Survey frame draft")
        page.locator("#measuredPairRows input").first.fill("1.234")
        page.locator("#measuredMarkerRows input[type=number]").first.fill("0")
        page.locator("#measuredTargetName").focus()
        time.sleep(.25)  # Several 50 Hz pose messages arrive while the input is focused.
        self.assertEqual(page.evaluate("document.activeElement.id"), "measuredTargetName")
        self.assertEqual(page.input_value("#measuredTargetName"), "Survey frame draft")
        page.click("#saveMeasuredTarget")
        page.wait_for_function("document.querySelector('#measuredTargetFeedback').textContent === 'Confirmed'")
        page.reload();page.wait_for_selector("#measuredTargetName")
        self.assertEqual(page.input_value("#measuredTargetName"), "Survey frame draft")
        self.assertEqual(page.locator("#measuredPairRows input").first.input_value(), "1.234")

    def test_graphics_marker_distance_and_delay_controls(self):
        page = self.page
        page.goto(f"{self.base}/control")
        page.wait_for_selector("#addMarker:not([disabled])")
        initial = page.locator("#items .tree-row").count()
        page.click("#addMarker")
        page.wait_for_function(f"document.querySelectorAll('#items .tree-row').length > {initial}")
        page.locator("#items .tree-row").last.click()
        page.wait_for_selector("#distanceDiagnostics")
        self.assertIn("3D distance", page.text_content("#distanceDiagnostics"))
        page.fill("#graphicsDelay", "37");page.locator("#graphicsDelay").press("Enter")
        page.wait_for_timeout(150)
        self.assertEqual(page.input_value("#graphicsDelay"), "37")

    def test_group_visibility_reports_parent_mask_without_changing_child_visibility(self):
        page = self.page
        page.goto(f"{self.base}/control");page.wait_for_selector("#addGroup:not([disabled])")
        page.click("#addGroup");page.fill("#dialogInput", "Browser Group");page.click("#dialogAccept")
        page.wait_for_function("document.querySelector('#props input[data-key=\"name\"]')?.value === 'Browser Group'")
        page.click("#addMarker");page.wait_for_selector('#props select[data-key="group_id"]')
        page.select_option('#props select[data-key="group_id"]', label="Browser Group")
        page.locator("#items .group-row", has_text="Browser Group").click()
        visible = page.locator('#props input[data-key="visible"]')
        visible.uncheck();visible.blur()
        page.wait_for_function("[...document.querySelectorAll('#items .tree-name')].some(node=>node.textContent.includes('PARENT HIDDEN'))")
        page.locator("#items .tree-row.child.effectively-hidden").click()
        self.assertIn("PARENT GROUP", page.text_content("#props"))
        self.assertTrue(page.locator('#props input[data-key="visible"]').is_checked())
