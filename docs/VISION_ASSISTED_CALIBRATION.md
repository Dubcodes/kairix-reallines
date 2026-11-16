# Vision-assisted calibration architecture

Status: V1.4 implements operator-triggered manual still import, OBS WebSocket v5 camera-source capture, target teaching, constrained OpenCV template detection and review through the existing Dot Calibration pipeline. Manual calibration remains independent.

## Proposed worker boundary

- The current capture adapters are manual upload and OBS `GetSourceScreenshot`. A future Linux UVC/V4L2 adapter can implement the same still-image boundary without changing detection or solver integration. The operator manually aligns and identifies markers in the teaching image.
- A capture request must atomically retain the image, immutable Pico cumulative-count snapshot, active reference UUIDs, camera model identity and monotonic timestamps.
- Validate that camera movement has settled and measure/limit video latency before accepting a capture. Pi and browser clocks must not be subtracted without synchronization.
- Accept one bounded analysis job at a time and report busy state rather than buffering frames. Detection runs off the event loop and never touches tracking/UART reception.
- Complete analysis against its immutable image/count/reference snapshot even if the operator moves to another view. Additional camera positions create independent viewpoints.
- Detected/refined points are proposals. Only identity-confident, image-stable, encoder-stable results with valid tracking can be loaded as saveable vision drafts; the operator still reviews them in the manual editor. Unvalidated results are visible but cannot be saved. Accepted observations and the previous good calibration retain the existing rollback policy.
- Keep the manual Dot Calibration data and solver as an independent fallback. The measured-target schema must migrate by version and never be embedded in legacy target dictionaries.

## Credential and synchronization boundary

- OBS host, port, source name and password are explicitly configured. The password lives only in `data/vision_obs_secret.json`, is omitted from status/diagnostics and is never a browser/static asset.
- OBS requests name the camera input and never change scene/program state.
- Two screenshots 250 ms apart provide a stopped-feature check. Encoder counts are compared across the same request. This is a settling guard, not exposure-time synchronization: OBS buffering remains unknown and the response timestamp is never subtracted from the Pico clock.
- Taught image templates and marker identities persist separately in `data/vision_targets.json`.
- Stopping the session prevents new capture/analysis. An already-running bounded job finishes and remains available in the completed-result review list.

## Deferred work and physical validation

Subpixel corner-specific models, scale/rotation-invariant descriptors, ChArUco/reusable lens calibration, local UVC/V4L2 capture, calibrated video latency and an automatic multi-view session controller remain future work. The first detector intentionally uses constrained local template matching and rejects ambiguity. OBS source naming, authentication, image stability, real marker appearance, lighting/zoom range and CPU impact require bench validation before broadcast use. USB HID operator controls should eventually call the same validated HTTP/application actions; no Pico firmware or physical-button assignment is introduced here.
