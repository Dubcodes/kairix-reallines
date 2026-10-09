# Kairix RealLines

Early development skeleton for a headless Raspberry Pi camera-tracking graphics system.

## v0.0.5-dev goals

- `/engineering` is the only navigation/system hub.
- `/calibration` provides separate axis-direction and world/camera calibration workflows.
- `/control` can add/edit/remove live world-coordinate lines and text.
- `/gfx` is a clean graphics-only output.
- Simulator drives pan and tilt through the canonical tracking pipeline; FOV and camera height remain persisted camera/profile settings.
- A bounded monotonic pose history supplies an independently delayed render camera for video/graphics synchronization.
- Configuration uses infrequent full-state WebSocket messages; compact pose messages stream at about 50 Hz.
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

Source selection is per axis. `simulator`, `disabled`, and `quadrature_gpio` are functional; `imu` and `external` remain recognised but unconfigured. Pan and Tilt are required for overall tracking validity. Zoom and Focus may be disabled. Each GPIO axis has a dedicated libgpiod v2 event thread; one source manager owns lifecycle, diagnostics, and retry after driver errors. GPIO code is imported only by the Linux adapter, so development and tests remain import-safe on Windows.

Hardware/tracking setup profiles are separate from graphics layouts and persist under `data/profiles/<stable-id>.json`, with `data/profiles/index.json` holding metadata and the active ID. Profiles support create, select, rename, duplicate, delete and per-axis updates. A clean install starts with **Development Simulator**. Existing `engineering.json` source selections and direction calibration are imported once without deleting either legacy file.

For a quadrature axis, PPR means **A-channel cycles per encoder revolution**. The authoritative gearing field is `encoder_revs_per_camera_rev`:

```text
counts_per_camera_revolution = PPR × quadrature_multiplier × encoder_revs_per_camera_rev
degrees_per_count = 360 / counts_per_camera_revolution
mapped_angle = reference_angle + offset + direction × (raw_count - reference_count) × degrees_per_count
```

At 600 PPR the decoder yields 600, 1200, or 2400 counts per encoder revolution at ×1, ×2, or ×4. With 1 encoder revolution per camera revolution, 2400 ×4 counts maps to 360°. Counts are unbounded signed integers. Illegal Gray-code transitions are diagnosed and do not move the count. Software debounce is off by default because it can discard legitimate high-rate edges.

Direction is always `+1` or `-1`. Direction marks learn only orientation; raw count motion is visible before referencing. A quadrature axis becomes valid only after the driver is alive and the operator uses **Set current angle** or **Zero** in Engineering. Each such action creates a new session-only UUID reference identity; clearing the reference, restarting the service, retrying the GPIO driver, detecting an illegal Gray-code transition, or detecting a kernel event sequence gap invalidates that identity. Raw counts and fault diagnostics remain available for troubleshooting, but a new reference must be established before the axis is valid again. A stationary referenced encoder remains valid because lack of edges is not stale.

GPIO edge processing uses the kernel's monotonic event timestamps rather than userspace read time. Engineering exposes the current A/B state, raw count, angle relative to the current reference, mapped angle, reference state and angle, edge/count/degree rates, separate global and per-line sequence-gap totals, illegal transitions, integrity-loss events, last-edge age, retry count, and driver status. Relative angle is unavailable until referenced and deliberately excludes direction, offset, and the operator-entered reference angle; mapped angle applies those settings.

The integrity policy is intentionally conservative for first-hardware bring-up: any observed illegal transition or sequence discontinuity may mean an unknown count was lost, so it latches integrity loss and requires re-referencing. Real bench data may justify a more nuanced policy later, but the system will not silently preserve absolute world calibration while count integrity is uncertain.

### Encoder wiring and bench bring-up

No GPIO pins are assumed. Engineering requires an explicit gpiochip device (for example `/dev/gpiochip0`) and distinct non-negative A/B **line offsets** for every quadrature axis. The same chip/line cannot be assigned to two axes. Inspect the Pi before configuring:

```bash
gpiodetect
gpioinfo /dev/gpiochip0
```

Raspberry Pi GPIO is **3.3 V only**. Never connect a 5–24 V encoder output directly. Identify whether outputs are push-pull, open-collector/open-drain, or differential and use appropriate isolation or level conversion. Open-collector outputs need pull-ups to 3.3 V; differential outputs need a suitable receiver. The UI bias (`as_is`, `pull_up`, or `pull_down`) is not a substitute for electrical level conversion.

A safe initial bench profile can use Pan=`quadrature_gpio`, Tilt=`simulator`, and Zoom/Focus=`disabled`. With power off, connect encoder ground through the chosen interface, connect A/B to the configured offsets, then power up. Confirm raw counts change, learn direction if needed, set a known current angle, and only then capture world-calibration marks. Diagnostics show event totals, illegal transitions, and sequence gaps.

Important profile and layout JSON writes use atomic temporary-file replacement. Diagnostic bundles include profile configuration alongside existing state and debug logs.

## World/camera calibration

World calibration is deliberately separate from setup profiles and graphics layouts. Named calibrations are stored atomically under `data/world_calibrations/<stable-id>.json`, with the active ID and metadata in `data/world_calibrations/index.json`. Each calibration references its setup-profile ID and a deterministic SHA-256 fingerprint covering source, direction, offset, PPR, decode multiplier, gearing, GPIO identity/bias, and reference semantics. A solved calibration also records the current session reference identity for every quadrature Pan/Tilt axis. A profile/fingerprint mismatch or a different, absent, or invalidated runtime reference prevents world tracking and is reported as requiring recalibration. Observations captured under mixed reference identities cannot be solved together. Simulator-only calibrations remain compatible without an encoder identity. Legacy calibrations without a fingerprint remain stored but invalid until solved again. `/gfx` and the Control Camera editor require both `camera.valid` and `camera.world_valid`; Top Down editing remains available.

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

The backend samples all sources through one canonical `time.monotonic()` path at approximately 200 Hz and records compact poses in a RAM-only deque. Sampling and pose publication run as independent tasks, so a slow or blocked WebSocket broadcast cannot reduce acquisition cadence. Each sample contains tracking and world Pan/Tilt, camera X/Y/Z, fixed roll, FOV, validity, and the active setup-profile/world-calibration IDs. Runtime reference-identity changes reset the history before a new sample is recorded. Compact pose transport is broadcast at approximately 50 Hz; full scene/configuration state is sent only when it changes. If WebSocket transport is unavailable, clients use a slow full-state poll plus a faster `/api/pose` poll. The default history depth is five seconds.

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
