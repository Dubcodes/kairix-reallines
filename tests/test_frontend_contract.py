import ast
import re
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from app.state import StateStore


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
LEGACY_ASSET_TOKENS = ("tracking1", "world1", "sync1", "layouts5")


class SilentDebug:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, _tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])


def html_ids(path: Path) -> set[str]:
    parser = AnchorParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser.ids


def module_target(owner: Path, reference: str) -> Path:
    clean = reference.split("?", 1)[0]
    if clean.startswith("/static/"):
        return STATIC / clean.removeprefix("/static/")
    return owner.parent / clean


class FrontendContractCase(unittest.TestCase):
    def test_engineering_static_anchors_cover_module_references_and_boot_status(self):
        html_path = STATIC / "engineering.html"
        js_path = STATIC / "js" / "engineering.js"
        html = html_path.read_text(encoding="utf-8")
        js = js_path.read_text(encoding="utf-8")
        anchors = html_ids(html_path)
        required = {
            "uiStatus", "trackingStatus", "system", "profileSelect", "profileName",
            "newProfile", "duplicateProfile", "renameProfile", "deleteProfile",
            "profileError", "trackingAxes", "pan", "panOut", "tilt", "tiltOut",
            "fov", "fovOut", "height", "heightOut", "live", "syncDiagnostics",
            "allowProvisionalGfx", "gfxProjectionStatus", "debugOn", "debugOff", "debugState", "engineeringDialog", "dialogTitle",
            "dialogMessage", "dialogInput", "dialogCancel", "dialogAccept",
        }
        self.assertTrue(required <= anchors)
        literal_references = set(re.findall(r"byId\(['\"]([^'\"]+)['\"]\)", js))
        self.assertEqual(set(), literal_references - anchors)
        self.assertIn("UI LOADING", html)
        self.assertIn("__kairixEngineeringBoot", html)
        self.assertIn("UI ERROR", html)
        for field in ("filename", "lineno", "colno"):
            self.assertIn(field, html)

    def test_page_module_graph_resolves_and_uses_one_repair_revision(self):
        references = []
        owners = list(STATIC.glob("*.html")) + list((STATIC / "js").glob("*.js"))
        for owner in owners:
            content = owner.read_text(encoding="utf-8")
            references.extend((owner, value) for value in re.findall(r"(?:src=|href=|from\s+|import\s*)['\"]([^'\"]+\.(?:js|css)(?:\?[^'\"]*)?)", content))
            for token in LEGACY_ASSET_TOKENS:
                self.assertNotIn(f"?v={token}", content, owner.name)
        versioned = {value.split("?v=", 1)[1] for _, value in references if "?v=" in value}
        self.assertEqual({"world-media1"}, versioned)
        for owner, reference in references:
            self.assertTrue(module_target(owner, reference).is_file(), f"{owner.name}: {reference}")

    def test_visible_control_ids_on_operator_pages_are_wired(self):
        for page in ("engineering", "calibration", "control"):
            html = (STATIC / f"{page}.html").read_text(encoding="utf-8")
            js = (STATIC / "js" / f"{page}.js").read_text(encoding="utf-8")
            controls = set(re.findall(r"<(?:button|input|select)[^>]+\bid=['\"]([^'\"]+)", html))
            for control_id in controls:
                self.assertRegex(js, rf"['\"]{re.escape(control_id)}['\"]", f"{page}: {control_id}")

    def test_operator_page_api_targets_resolve_to_backend_routes(self):
        tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
        routes = set()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and decorator.args and isinstance(decorator.args[0], ast.Constant):
                    value = decorator.args[0].value
                    if isinstance(value, str) and value.startswith("/api/"):
                        routes.add(re.sub(r"\{[^}]+\}", "{}", value))
        for page in ("engineering", "calibration", "control"):
            js = (STATIC / "js" / f"{page}.js").read_text(encoding="utf-8")
            targets = re.findall(r"api\((?:`|'|\")(/api/.*?)(?:`|'|\")", js)
            for target in targets:
                shape = re.sub(r"\$\{[^}]+\}", "{}", target)
                matches = any(re.fullmatch(re.escape(route).replace(r"\{\}", "[^/]+"), shape) for route in routes)
                self.assertTrue(matches, f"{page}: {target}")

    def test_development_cache_policy_covers_pages_and_static_assets(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('request.url.path.startswith("/static/")', source)
        self.assertIn('"Cache-Control"] = "no-store, max-age=0"', source)
        for page in ("engineering", "calibration", "control", "gfx"):
            self.assertIn(f'"/{page}"', source)

    def test_engineering_api_targets_have_matching_routes(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        expected = (
            '@app.get("/api/system")', '@app.post("/api/camera")',
            '@app.post("/api/profiles")', '@app.post("/api/profiles/{profile_id}/select")',
            '@app.patch("/api/profiles/{profile_id}")',
            '@app.post("/api/profiles/{profile_id}/duplicate")',
            '@app.delete("/api/profiles/{profile_id}")',
            '@app.patch("/api/profiles/{profile_id}/axes/{axis}")',
            '@app.patch("/api/profiles/{profile_id}/camera-rig")',
            '@app.post("/api/tracking/{axis}/reference")',
            '@app.delete("/api/tracking/{axis}/reference")',
            '@app.patch("/api/engineering")',
            '@app.post("/api/debug/{enabled}")',
        )
        for route in expected:
            self.assertIn(route, source)

    def test_engineering_source_choices_cover_backend_enum(self):
        tracking_tree = ast.parse((ROOT / "app" / "tracking.py").read_text(encoding="utf-8"))
        assignment = next(node for node in tracking_tree.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "SOURCE_TYPES" for target in node.targets))
        backend = set(ast.literal_eval(assignment.value))
        contract = (STATIC / "js" / "engineering_contract.js").read_text(encoding="utf-8")
        frontend = set(re.findall(r"value:'([^']+)'", contract))
        self.assertEqual(backend, frontend)

    def test_gpio_editor_applies_one_complete_source_config_draft(self):
        source = (STATIC / "js" / "engineering.js").read_text(encoding="utf-8")
        contract = (STATIC / "js" / "engineering_contract.js").read_text(encoding="utf-8")
        self.assertIn("Apply GPIO settings", source)
        self.assertIn("buildEncoderSourceConfig(draft)", source)
        self.assertIn("JSON.stringify({values})", source)
        self.assertNotRegex(source, r"updateAxis\(axis,\{source_config:\{\[key\]")
        for field in ("chip", "line_a", "line_b", "bias"):
            self.assertIn(field, contract)

    def test_websocket_runtime_dependency_and_fallback_are_explicit(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        self.assertTrue(any(re.fullmatch(r"websockets==\d+\.\d+(?:\.\d+)?", line) for line in requirements))
        common = (STATIC / "js" / "common.js").read_text(encoding="utf-8")
        engineering = (STATIC / "js" / "engineering.js").read_text(encoding="utf-8")
        self.assertIn("ws.onerror", common)
        self.assertIn("startPolling", common)
        self.assertIn("WEBSOCKET ERROR / RETRYING", engineering)
        self.assertIn("POLLING FALLBACK", engineering)

    def test_ci_syntax_checks_every_operator_javascript_file(self):
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        for name in ("common", "engineering", "engineering_contract", "calibration", "control", "editor", "render"):
            self.assertIn(f"node --check app/static/js/{name}.js", workflow)
        for name in ("calibration_contract", "preview", "preview_media"):
            self.assertIn(f"node --check app/static/js/{name}.js", workflow)

    def test_preview_and_clean_gfx_contracts_are_separate(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        preview = (STATIC / "js" / "preview.js").read_text(encoding="utf-8")
        preview_html = (STATIC / "preview.html").read_text(encoding="utf-8")
        gfx = (STATIC / "gfx.html").read_text(encoding="utf-8")
        self.assertIn('@app.get("/preview")', main)
        self.assertIn("renderScene(canvas,state", preview)
        self.assertIn("allowProvisional:true", preview)
        self.assertIn("state.render_camera", (STATIC / "js" / "render.js").read_text(encoding="utf-8"))
        self.assertIn("NO CAMERA PREVIEW SOURCE", preview_html)
        self.assertNotIn("preview-media", gfx)
        self.assertNotRegex(gfx, r"<(?:img|video)\b")

    def test_png_upload_and_asset_routes_are_safely_scoped(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        state = (ROOT / "app" / "state.py").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn('@app.post("/api/assets/png")', main)
        self.assertIn('file.content_type != "image/png"', main)
        self.assertIn('media_type="image/png"', main)
        self.assertIn("10 * 1024 * 1024", main)
        self.assertIn("ASSET_ID_RE.fullmatch", state)
        self.assertIn('self.assets_dir / f"{asset_id}.png"', state)
        self.assertRegex(requirements, r"python-multipart==\d+\.\d+\.\d+")

    def test_movement_calibration_uses_existing_profile_update_path(self):
        calibration = (STATIC / "js" / "calibration.js").read_text(encoding="utf-8")
        html = (STATIC / "calibration.html").read_text(encoding="utf-8")
        self.assertIn("calculateMovementCalibration", calibration)
        self.assertIn("movementCard('pan')+movementCard('tilt')", calibration)
        self.assertIn("movement[axis]", calibration)
        self.assertIn("/api/profiles/${state.profiles.active_id}/axes/${axis}", calibration)
        self.assertIn("direction_learned:true", calibration)
        self.assertIn("require a new encoder reference", html)
        self.assertIn("invalidate", html)

    def test_rig_geometry_ui_uses_profile_path_and_nine_marks(self):
        calibration = (STATIC / "js" / "calibration.js").read_text(encoding="utf-8")
        html = (STATIC / "calibration.html").read_text(encoding="utf-8")
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        for target in ("top_center", "middle_left", "center", "middle_right", "bottom_center"):
            self.assertIn(target, calibration)
        for field in ("right_m", "forward_m", "up_m"):
            self.assertIn(field, calibration)
            self.assertIn(f'data-rig-offset="{field}"', html)
        self.assertIn("Apply solved rig geometry", html)
        self.assertIn("/camera-rig", calibration)
        endpoint = main.split('@app.patch("/api/profiles/{profile_id}/camera-rig")', 1)[1].split("@app.", 1)[0]
        self.assertNotIn("source_manager.configure", endpoint)

    def test_projection_policy_is_shared_and_gfx_override_is_explicit(self):
        render = (STATIC / "js" / "render.js").read_text(encoding="utf-8")
        control = (STATIC / "js" / "control.js").read_text(encoding="utf-8")
        gfx = (STATIC / "gfx.html").read_text(encoding="utf-8")
        state = (ROOT / "app" / "state.py").read_text(encoding="utf-8")
        self.assertIn("export function projectionMode", render)
        self.assertIn("projectItemGeometry", control)
        self.assertIn("allowProvisional:true", control)
        self.assertIn("state?.render_camera||state?.camera", control)
        self.assertNotIn("!camera?.valid||!camera?.world_valid", control)
        self.assertIn("allowProvisional:state.engineering?.allow_provisional_gfx===true", gfx)
        self.assertIn('"allow_provisional_gfx": False', state)

    def test_provisional_workflow_labels_are_clear(self):
        engineering = (STATIC / "js" / "engineering.js").read_text(encoding="utf-8")
        engineering_html = (STATIC / "engineering.html").read_text(encoding="utf-8")
        calibration = (STATIC / "js" / "calibration.js").read_text(encoding="utf-8")
        self.assertIn("Encoder shaft revs per 1 camera revolution", engineering)
        self.assertIn("format(degreesPerCount,6)", engineering)
        self.assertIn("° / count", engineering)
        self.assertIn("Simulator inputs", engineering_html)
        self.assertIn("Simulator inactive", engineering)
        self.assertIn("Camera Preview is available using a provisional pose", calibration)

    def test_engineering_exposes_high_rate_encoder_diagnostics(self):
        engineering = (STATIC / "js" / "engineering.js").read_text(encoding="utf-8")
        for label in (
            "Requested kernel event buffer", "Read batch size", "Edges/sec", "Peak edges/sec",
            "Largest read batch", "Total events", "Global sequence gaps", "Line sequence gaps",
            "Integrity-loss events", "Integrity", "Counts/sec", "Degrees/sec", "Last edge age",
        ):
            self.assertIn(label, engineering)

    def test_state_and_system_payloads_cover_engineering_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = StateStore(Path(directory), SilentDebug()).snapshot()
        expected_state = {
            "profiles", "tracking", "camera", "render_camera", "sync",
            "world_calibrations", "engineering", "clients",
        }
        self.assertTrue(expected_state <= snapshot.keys())

        tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "system_info")
        returned = next(node.value for node in ast.walk(function) if isinstance(node, ast.Return))
        keys = {key.value for key in returned.keys if isinstance(key, ast.Constant)}
        self.assertEqual({
            "product", "version", "hostname", "platform", "python", "machine",
            "uptime_seconds", "data_dir", "debug_enabled", "debug_session",
            "websocket_clients",
        }, keys)

    def test_synthetic_calibration_side_effect_is_explicit(self):
        html = (STATIC / "calibration.html").read_text(encoding="utf-8")
        self.assertIn("DEVELOPMENT TOOL", html)
        self.assertIn("ACTIVE calibration", html)
        self.assertIn("changes the active camera/world solution", html)
        self.assertIn("separate from Simulator Input", html)

    def test_installers_are_executable_and_restart_rewritten_services(self):
        listing = subprocess.check_output(
            ["git", "ls-files", "-s", "installer/install-dev.sh", "installer/install.sh"],
            cwd=ROOT,
            text=True,
        )
        modes = {line.split()[-1]: line.split()[0] for line in listing.splitlines()}
        self.assertEqual("100755", modes["installer/install-dev.sh"])
        self.assertEqual("100755", modes["installer/install.sh"])
        for name, service in (
            ("install-dev.sh", "kairix-reallines-dev.service"),
            ("install.sh", "kairix-reallines.service"),
        ):
            source = (ROOT / "installer" / name).read_text(encoding="utf-8")
            self.assertIn(f"systemctl restart {service}", source)
            self.assertIn(f"systemctl is-active --quiet {service}", source)


if __name__ == "__main__":
    unittest.main()
