# Vision-assisted calibration architecture (future)

Status: design only. V1.3 implements the measured-target editor and persistence, not image capture, marker detection or automatic calibration.

## Proposed worker boundary

- Use an optional Linux UVC/V4L2 capture device. The operator manually aligns and identifies markers in the first image.
- A capture request must atomically retain the image, immutable Pico cumulative-count snapshot, active reference UUIDs, camera model identity and monotonic timestamps.
- Validate that camera movement has settled and measure/limit video latency before accepting a capture. Pi and browser clocks must not be subtracted without synchronization.
- Enqueue bounded analysis jobs. Prefer freshness and explicit overflow reporting; never block tracking or UART reception.
- Run analysis at low priority after capture, even if the operator moves to another view. Additional camera positions create independent viewpoints.
- Detected/refined points are proposals. Only validated, explicitly accepted observations become solver inputs. Preserve raw captures, operator measurements, accepted observations and the previous good calibration so rollback is always possible.
- Keep the manual Dot Calibration data and solver as an independent fallback. The measured-target schema must migrate by version and never be embedded in legacy target dictionaries.

## Deferred work

OpenCV integration, ChArUco/reusable lens calibration, UVC device selection, video-latency characterization, automatic marker refinement and an automatic vision solver remain unimplemented. USB HID operator controls should eventually call the same validated HTTP/application actions; no Pico firmware or physical-button assignment is introduced here.
