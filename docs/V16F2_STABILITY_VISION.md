# V1.6F2 — Web delivery jitter / first optical encoder-resolution measurement

**Locked successful baseline:** `850fc18` (`wip/live-tuning-dot-lock`). The physical Pico, 1 Mbps UART, 200 Hz StateStore pose producer, systemd boot configuration and current operator references are **unchanged** by this patch.

## Scope / findings

1. Existing automatic stability capture lasts five seconds; exports were not aligned with longer OBS clips. V1.6F1 rolling buffer is incorporated automatically by this patch when not already committed, and exports the previous ten minutes at ~50 Hz using compact RAM-only samples. Recent export is distinct from the short triggered/manual capture. No old event data can be retroactively recovered.
2. Diagnostics bundle compression previously ran inside the Uvicorn event loop. Its `create_bundle` call now runs in a worker thread. Downloads still cause Pi CPU and SD contention; avoid while measuring and only export once capture is complete. Filenames use microsecond resolution to avoid collisions.
3. Browser telemetry now records consecutive `server_mono` pose intervals in **server clock**, as well as browser reception intervals. Comparing **intervals**, not absolute timestamps, is valid when server and browser clocks are not synchronized. Server publication gaps or browser-delivery gaps distinguish upstream handoff from network/OBS. It remains impossible to infer actual KMS scanout/genlock from this alone.
4. Existing OpenCV/OBS camera-only calibration capability remains. New `POST /api/vision-calibration/capture-motion` triggers two OBS source screenshots 3 seconds apart. During that interval an operator turns one **physical** camera axis while leaving Zoom fixed. Shi-Tomasi corners and forward/backward Lucas-Kanade optical flow yield median image motion and robust feature agreement. It compares the source image displacement to **mapped Pico counts × degrees/count × focal length**, producing a **review-only** diagnostic. No calibration data is automatically applied.

## Vision calibration constraints and limitations

- Use **live camera-only OBS footage**. The previously recorded race footage used behind graphics is unrelated to the current physical tripod movement. It cannot calibrate the Pan/Tilt encoders. Do not use an OBS scene containing RealLines graphics for computer vision: it can lock onto its own overlay.
- OBS WebSocket v5 must be reachable **from the Raspberry Pi** (e.g. OBS host = Windows PC's LAN address, firewall allows OBS WebSocket TCP port 4455). Saving OBS password is supported by existing code, stored locally under `data/vision_obs_secret.json` with private permissions. Prefer trusted LAN only.
- Reference Pan, Tilt and Zoom. Open `/calibration` > Vision Calibration, start the session, configure OBS, choose **Measure Pan/Tilt movement (3 seconds)**, and rotate **one axis**, moderately, pointing at **stationary distant scenery**. Avoid horses/cars/people moving across the picture. Zoom must stay fixed. Do **not** use this as an on-air calibration operation.
- Horizontal FOV is sourced from RealLines' current camera model. Camera movements are approximated by 2-D scene translation. Rotation, parallax, lens distortion, moving subjects, changing crop/resolution, autofocus changes and source scaling can produce discrepancies. The ratio is **diagnostic evidence**, not a verified gear correction or proof of missed encoder counts.
- **Encoder angular step** is independent of video output resolution. With example values Pan 600 PPR ×4 and ratio 4.559715223, step ≈0.03290°/count. Tilt ratio 1.2 gives ≈0.125°/count. At a *true horizontal* FOV of 56° and 1920-pixel image width the central shift is about 1.04 px per Pan count and 3.94 px per Tilt count. At higher video resolution those same physical steps span *more*, not fewer, pixels. Increasing output resolution alone does not fix coarse physical angular steps. Confirm observed precision with image test and real lens/camera before deciding to replace gears/encoders.
- Optical-flow analysis is limited to one still pair; a complete auto-calibration solution ultimately needs multiple independent stills, survey targets, lens distortion, mechanical backlash/direction reversals, and a constrained global optimization with operator acceptance.

## Test protocol for next small jitter

- Keep V1.6E dedicated owner running (`systemctl is-active` for both units). Re-reference only if a service was restarted or the Pico reset.
- Record a 60–120 s continuous slow Pan while simultaneously recording GFX in OBS. Do not run ZIP export during motion.
- Immediately after stopping OBS, open Engineering > Tracking Stability > **Recent 10 min (video)** and download JSONL. Export the diagnostic ZIP separately if needed.
- Compare `publication.server_timestamp` intervals (server publish), `browser_metrics.server_pose_gap_max_ms_5s` (server intervals delivered), `browser_metrics.browser_pose_gap_max_ms_5s` (client receive intervals), browser animation/frame gaps, and owner health. A stationary capture cannot prove motion smoothness.
- If OBS output catches while browser pose/frame delivery remains even, investigate OBS render, capture, frame rate conversion and compositing separately. For physical 1080i50, test actual HDMI/SDI scanout; a 50 fps browser count is not scanout measurement.

## Rollback

Use `git revert <V1.6F2 commit>` rather than hard reset. Revert code only; systemd dedicated tracking mode, profile files and reference data remain untouched. This patch is review-only for camera model and does not perform a firmware update.
