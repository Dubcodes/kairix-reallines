# Kairix RealLines

Early development skeleton for a headless Raspberry Pi camera-tracking graphics system.

## v0.0.4-dev goals

- `/engineering` is the only navigation/system hub.
- `/calibration` provides separate axis-direction and world/camera calibration workflows.
- `/control` can add/edit/remove live world-coordinate lines and text.
- `/gfx` is a clean graphics-only output.
- Simulator drives pan and tilt through the canonical tracking pipeline; FOV and camera height remain persisted camera/profile settings.
- A bounded monotonic pose history supplies an independently delayed render camera for video/graphics synchronization.
- All pages share state over WebSockets.
- Scene, setup-profile, direction-calibration and world-calibration state persists under the data directory.
- Heavy debugging is enabled by default and a diagnostic ZIP can be downloaded from Engineering.

## Quick development run

Requires Python 3.10+.

```bash
./run-dev.sh
```

Then open:

- http://PI-IP:8080/engineering
- http://PI-IP:8080/calibration
- http://PI-IP:8080/control
- http://PI-IP:8080/gfx


## Recommended Pi development install

For the build phase, keep the repository in your normal Pi user's home directory, then run:

```bash
sudo ./installer/install-dev.sh
```

This creates a `kairix-reallines-dev.service` that runs the repository directly with Uvicorn reload enabled, and adds a writable Samba share named `reallines-dev`. Editing the files over SMB therefore changes the same checkout the Pi is running.

After the script runs, set the Samba password once:

```bash
sudo smbpasswd -a YOUR_PI_USERNAME
```

The eventual clean-build/release path is deliberately separate (`installer/install.sh`) and installs immutable releases under `/opt/kairix-reallines`.

## Pi install

On Raspberry Pi OS Lite:

```bash
sudo ./installer/install.sh
```

The installer creates a `kairix` system user, a Python venv, `/opt/kairix-reallines`, `/var/lib/kairix-reallines`, and a systemd service listening on port 8080.

## Coordinate model

- +X = world right
- +Y = world forward
- +Z = up
- Camera position is `camera.x`, `camera.y`, `camera.z`; legacy `camera.height` mirrors Z
- Pan 0 looks along +Y
- Tilt 0 is level
- Positive pan turns toward +X/right; positive tilt turns upward
- World orientation is `tracking angle + world-calibration offset`

The initial Canvas renderer is intentionally lightweight for the Pi 3B. It is a proof of the world-coordinate/state architecture rather than the final textured/3D renderer.

## Control editor

The Control page keeps the live camera-projected view and adds an independent orthographic **Top Down** editor. Lines, text and labelled marker objects can be selected from either viewport or the Layers tree. Drag markers, text, or a line body to move them in X/Y without changing Z; selected lines also expose fixed-size endpoint handles that can be dragged independently.

In Top Down, the mouse wheel zooms and middle-button drag or Space + left drag pans. **Fit All** frames the scene and camera origin. The metric grid can be hidden, and drag snapping can be set to Off, 0.1 m, 0.5 m, or 1.0 m. These viewport preferences are local to the browser and do not change broadcast camera state.

When focus is outside an input, arrow keys nudge the selected object by 0.1 m, Shift + arrows by 1 m, and Alt + arrows by 0.01 m. Delete removes it; Escape cancels an active drag or otherwise clears selection. Numeric Properties remain available for exact values and commit on change, blur, or Enter.

### Groups and duplication

Groups provide one organisational level in the Layers tree. An object can belong to one group or remain under Ungrouped. Group visibility is a parent mask: hiding a group does not alter its children’s individual visibility, so individually hidden children stay hidden when the group is shown again. Selecting a group in Top Down shows its bounds and allows all children to be translated together while preserving their Z values.

Objects and groups can be duplicated from Properties or with Ctrl+D. Duplicates receive new IDs, retain group membership and other properties, and are offset by 0.5 m in X/Y. Group deletion can either keep children as ungrouped objects or remove the group and its contents.

### Named layouts and persistence

A named layout contains the complete graphics scene: background, groups, lines, text and markers. Layout controls support New, Save, Save As, Rename, Duplicate, Delete and live switching. The backend is authoritative for the active layout, so connected Control and GFX clients switch together.

Layouts are stored under the configured data directory in `layouts/<stable-id>.json`, with metadata and the active ID in `layouts/index.json`. Scene edits automatically persist to the active layout. Drag samples update memory and WebSocket clients without writing to disk; pointer-up performs one atomic layout save. The Control toolbar shows UNSAVED during transient/in-flight changes and SAVE ERROR if persistence fails.

On first launch after upgrading, an existing `scene.json` is imported into a Default Layout without deleting or modifying the legacy file. A clean installation instead receives a Home Run demo containing Finish, 50M, 100M and 200M lines and markers.

Current group support is intentionally limited to a single grouping level and X/Y translation. Nested groups, group rotation/scaling, PNG assets, custom planes and undo/redo are not implemented yet.

## Tracking inputs and setup profiles

The renderer still consumes the stable `camera.pan`, `camera.tilt`, `camera.fov`, `camera.height` and `camera.valid` contract. Behind that contract, runtime `tracking.axes` records source, raw value, mapped value, health, validity, timestamp, age and sample-rate information independently for Pan, Tilt, Zoom and Focus.

Source selection is per axis. `simulator` and `disabled` are functional; `quadrature_gpio`, `imu` and `external` are recognised configuration choices which deliberately report **NOT CONFIGURED** until their real drivers exist. Pan and Tilt are required for overall tracking validity. Zoom and Focus may be disabled.

Hardware/tracking setup profiles are separate from graphics layouts and persist under `data/profiles/<stable-id>.json`, with `data/profiles/index.json` holding metadata and the active ID. Profiles support create, select, rename, duplicate, delete and per-axis updates. A clean install starts with **Development Simulator**. Existing `engineering.json` source selections and direction calibration are imported once without deleting either legacy file.

For a quadrature axis, the authoritative gearing field is `encoder_revs_per_camera_rev`:

```text
counts_per_camera_revolution = PPR × quadrature_multiplier × encoder_revs_per_camera_rev
degrees_per_count = 360 / counts_per_camera_revolution
mapped_angle = offset + direction × raw_count_delta × degrees_per_count
```

Direction is always `+1` or `-1`. The Calibration LEFT/RIGHT and DOWN/UP marks learn only orientation and store it in the active setup profile. They never establish mechanical travel limits. Simulator samples use the same source → raw → mapping → tracking → camera path that future drivers will use, without stale expiry while sitting at a valid static position.

Important profile and layout JSON writes use atomic temporary-file replacement. Diagnostic bundles include profile configuration alongside existing state and debug logs.

## World/camera calibration

World calibration is deliberately separate from setup profiles and graphics layouts. Named calibrations are stored atomically under `data/world_calibrations/<stable-id>.json`, with the active ID and metadata in `data/world_calibrations/index.json`. Each calibration references the setup-profile ID with which its observations were captured. Selecting a different hardware profile exposes a mismatch and prevents that calibration from being treated as valid world tracking.

V1 uses four known corners of a vertical rectangular target: top-left, top-right, bottom-right and bottom-left. The target stores its world centre, width, height and yaw. Each operator mark captures raw and mapped Pan/Tilt values, monotonic time, target identity and setup-profile ID. Direction-learning LEFT/RIGHT/DOWN/UP marks remain a separate process.

The project-owned deterministic least-squares solver estimates five values from eight angular measurements: camera X/Y/Z plus Pan and Tilt world-zero offsets. Horizontal FOV and fixed roll are supplied configuration, not solver unknowns. For any world point:

```text
world_pan  = atan2(point_x - camera_x, point_y - camera_y)
world_tilt = atan2(point_z - camera_z, horizontal_distance)

world_pan  = tracking_pan  + pan_offset
world_tilt = tracking_tilt + tilt_offset
```

Four exact, distinct corner observations overdetermine the five-unknown solve. Because bearing-only observations of one coplanar target become ill-conditioned under measurement noise—particularly camera height versus Tilt offset—the operator's approximate camera pose and offset estimates are used as weak regularising constraints only when observations are not mathematically exact. Solutions report per-point residuals, RMS angular error and maximum angular error; RMS above 1 degree is rejected. A failed solve preserves the previous solution but marks the active calibration invalid until a good solve succeeds.

Engineering and Calibration expose a synthetic scenario generator that creates observations from known geometry and feeds them through the same solver. No SciPy or other numerical dependency is required.

## Tracking history and graphics synchronization

The compatibility `camera` object remains the instantaneous **live** calibrated pose. State also exposes `live_camera` explicitly and a separate `render_camera` used by `/gfx` and the Control Camera preview. Calibration marking always reads live per-axis tracking values and is therefore unaffected by graphics delay.

The backend records compact pose samples in a RAM-only deque using Python's monotonic high-resolution performance counter. Each sample contains tracking and world Pan/Tilt, camera X/Y/Z, fixed roll, FOV, validity, and the active setup-profile/world-calibration IDs. Tracking changes insert samples immediately; a lightweight 20 Hz backend heartbeat keeps a stationary valid pose represented without depending on browser requests. The default history depth is five seconds.

`data/sync.json` atomically persists only:

```json
{
  "graphics_delay_ms": 0,
  "history_duration_seconds": 5.0
}
```

Delay is constrained to 0–2000 ms. Rendering requests the pose at `current_monotonic_time - graphics_delay`. X/Y/Z, Tilt, roll and FOV use timestamp-weighted linear interpolation. Pan uses shortest-arc circular interpolation, so `179° → -179°` passes through ±180°, while `359° → 1°` passes through 0°.

Invalid samples are hard boundaries: the system never interpolates through tracking loss. A request older than the retained history reports `HISTORY_UNDERRUN`; an empty or newly reset buffer reports `BUFFERING`. The render pose is invalid in either case, causing world-tracked GFX objects to be hidden until a safe pose is available. A stationary camera remains valid, and zero delay uses the newest pose.

History is cleared whenever coordinate meaning changes, including setup-profile switches, world-calibration switches or solves, source/direction/offset changes, and relevant camera configuration changes. This deliberately produces a short refill period instead of blending incompatible reference frames. No prediction or future-time extrapolation is implemented.
