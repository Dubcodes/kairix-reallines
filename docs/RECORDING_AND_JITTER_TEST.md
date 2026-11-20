# RealLines V1.6F1 — video-aligned jitter evidence

## Why the previous exports covered only 5 seconds

The legacy detailed stability recorder is bounded to 10–60 seconds for manual recordings, and automatic scheduling/HDMI triggers create a **5-second** capture. `Download JSONL/CSV` previously exported only that capture. It did **not** retain everything since OBS started. In dedicated mode it also lacked 200 Hz raw acquisition events because that work executes in a separate process.

## New semantics

- **Recent 10 min (video)** is a continuously collected, compact, 50 Hz **publication trace**, retained in RAM. It records web-side camera/render pose, live axis counts available at that boundary, tracking integrity, UTC estimate, Pi monotonic timestamp, WebSocket queue metrics, sparse browser telemetry and significant scheduler/HDMI events. It does not capture every 500 Hz Pico packet, or the true physical HDMI presentation time.
- **Short diagnostic capture** is the old manual/automatic, detailed 10–60 second/5-second capture. It is still useful for a narrow problem. These are independent datasets.
- Data is held in memory, not written to the microSD until export. A service reboot, restart, or time beyond 10 minutes clears/ages out recent samples. The oldest recent samples are overwritten intentionally and bounded.
- UTC timestamps come from the Raspberry Pi clock and are **approximate** for aligning an OBS/Windows video; the clocks must agree to within the precision needed. Pi `mono` is monotonic but has a different origin from OBS and is not directly comparable.
- JSONL rolling exports start with an `export_metadata` record. CSV retains the existing columns and includes this as a first data row with metadata in `payload_json`. Older capture exports remain unchanged.
- Export JSON/CSV encoding runs in a separate worker thread to avoid freezing Uvicorn's 50 Hz event loop.

## Test procedure (before submitting another diagnostic video)

1. Leave RealLines in dedicated mode and re-reference Pan, Tilt, Zoom. Do not restart once filming starts.
2. Open the GFX output and Engineering; check the **Recent history (RAM)** counter is increasing.
3. Record a **moving** camera test in OBS for 60–120 seconds. Include slow, steady Pan/Tilt motion and, if possible, a visible time or synchronized slate at the beginning. The rolling trace records continuously; there is no need to press Start recording.
4. Stop OBS, promptly choose **Recent 10 min (video)** in Engineering, and download both JSONL and CSV. The longer the delay, the more early video frames age out.
5. Confirm the exported JSONL metadata coverage spans the video. Confirm exported `publication` records show angle/count movement throughout the same interval.
6. Export a diagnostics bundle and record owner health counters using `python -m app.tracking_service_check --configured --wait 10`.
7. Compare OBS and direct GFX. If the OBS clip catches but the 50 Hz pose stream continues smoothly, investigate OBS capture settings, frame duplication, preview/render bottlenecks or capture-device timing. If both catch simultaneously, inspect shared-memory age, publication gaps and browser-delivery timing.

## Current milestone

Tag baseline commit `850fc18` with **`checkpoint/v16e-major-stutter-eliminated-20261120`** before applying changes. This represents the **observed elimination of the major repeating two-second stutter**, not final broadcast qualification. The remaining short jitter is unresolved. Avoid mixing unrelated UI/old-file cleanup or computer-assisted camera calibration with the current timing experiment.
