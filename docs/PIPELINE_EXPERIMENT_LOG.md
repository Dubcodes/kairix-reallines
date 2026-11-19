# RealLines tracking/graphics pipeline — experiment history

**Last reviewed:** 2026-11-20.  **Purpose:** stop repeating low-value tests; keep measured findings, assumptions, rollback and next checks together. Update after every controlled benchmark. All numbers are from distinct test sessions unless explicitly identified as a paired comparison.

## Scope and non-negotiable requirements

- Pi 4B dedicated appliance; RP2040 Pico PIO quadrature counts over UART `/dev/serial0` at 1 Mbaud, 500 nominal packets/second.
- Each KRP1 packet is 63 bytes, with cumulative 64-bit pan/tilt/zoom counts, reserved focus count, session/sequence/timestamps, button states/press counters, firmware flags and CRC16.
- Processing target 200 Hz; graphics delivery 50 Hz; output must ultimately meet actual broadcast 1080i50 requirements. Output is not automatically genlocked.
- Zero **intentional** additional pose delay; no video feed delay as a workaround. Hold last complete pose briefly if needed; don't replay a backlog of counts.
- Tracking correctness, reference identity and calibration integrity must not be sacrificed for smooth-looking graphics. Retain native binary shared-memory protocol and `StateStore` mapping semantics.
- Engineering and other pages must continue to operate. They should not impose deadlines on native HDMI output.

## Timeline / attempts

| Date | Change/test | Measured observation | Conclusion / repeat? |
|---|---|---|---|
| Before Nov 19 | Current one-process Uvicorn: 500 Hz Pico, 200 Hz `asyncio` acquisition, 50 Hz publication, WebSocket/Canvas GFX | Large recurring visually evident stutter, ~2 s cadence | **Baseline failure.** A single Python event loop contains unrelated HTTP, WebSocket, tracking and diagnostics. |
| Nov 19 | GFX-only versus Engineering open, fewer browser clients | User consistently reports no material improvement GFX-only | **Do not repeat** as primary diagnosis. Browser count alone does not resolve stutter. |
| Nov 19 | Disable WebSocket compression (A/B), then revert | No visible improvement | Do **not** assume permessage-deflate is sole cause or repeat unaltered experiment. Watchdog stacks can still fall within WS sends. |
| Nov 19 | `3a30fca` diagnostic watchdog checkpoint; tag `checkpoint/pre-dedicated-pipeline-20261119` | Stalls commonly 40–200 ms; some snapshots in performance calculations, stability copying, WS/HTTP | Sampling stacks indicate *where the main thread was captured*, not complete operation duration or causation. |
| Nov 20 | `fcde04c`: move profiler and stability HTTP summaries to worker threads; avoid deep-copying pre-event evidence | User reports **no visual change**; measured live Pi 184.0–185.8 / 200 Hz, publication ~47.7–48.3 / 50 Hz; worst acquisition 119–158 ms; publication 168–188 ms | Genuine overhead reduction but not sufficient; threads still contend for GIL. |
| Nov 20 | Patch 2 commit `1c5f7a6` (inferred from subsequent push baseline): avoid routine deep copies in 50 Hz stability-publication path | User reports **no visual change** | Stop pursuing isolated micro-optimisation as primary solution. |
| Nov 20 | Diagnostics after Patch 2: Pico input ~500.1 Hz, zero missed UART packets/CRC errors, low typical pose computation; GFX canvas render ~1 ms | Server misses deadlines while Pico input and browser drawing appear healthy | Avoid slowing Pico to 200/100 Hz to hide a scheduler issue; **physical encoder pulse loss remains separately unproven**. |
| Nov 20 | `e364284` shadow process, simulated 500 Hz absolute counts, 200 Hz existing StateStore mapping, own `/dev/shm` pose file | **60.0 s**; 30,000 synthetic input intervals; **11,997 pose writes**, **199.95 Hz**, **zero wake-up deadline misses**; wake p95 1.070 ms, p99 1.129 ms, max 3.690 ms; processing p95 0.617 ms, p99 0.738 ms, max **15.474 ms**; final valid true | **Positive isolation signal**, not proof of real UART, GFX or native HDMI. 15.474 ms execution outlier needs investigation. Existing baseline is from a different workload, not controlled A/B. |
| Nov 20 | Initial isolated probe Windows tests | `ModuleNotFoundError: fcntl`; Pi four tests pass; five native pose tests on Windows pass | Environment portability defect, not tracking fault. Fix `fcntl` import location. |
| Nov 20 | V1.6A Phase 2 real UART shadow mirror (this patch) | **Not measured yet.** | One live UART reader forwards a **nonblocking, lossy** copy to separate independent shadow process. Safe evaluation before UART/control cutover. |

## Verified current design properties / caveats

- `app/pico_source.py` is the **only** live Pico reader. It validates CRC, tracks sequence gaps and retains latest cumulative count. `latest` mode drops obsolete samples rather than generating a processing backlog. 350 ms gap invalidates references (safety policy, not an intended buffer).
- `app/native_pose.py` writes latest pose to a versioned memory-mapped ring in `/dev/shm`, with sequence/generation and integrity checks; no per-frame `fsync`.
- Browser `/gfx` runs a `requestAnimationFrame` Canvas 2D renderer; display cadence being ~50 Hz in reports does not prove true HDMI scanout/field timing. Native SDL2 prototype is not feature-complete for all RealLines layouts.
- Existing native test chooses 1920×1080@50 progressive-style timing; **broadcast 1080i50 physical acceptance is outstanding**.
- Power status on recent Nov 20 tests indicated `0x0` (no undervoltage at time of capture); historical earlier `0x50000` reflects past events, not proof of ongoing power trouble.
- GC captured pauses up to ~84–94 ms, but a GC overlap is not necessarily a complete explanation for all stalls.
- User already tested GFX-only and unchanged stutter. Do not request the same simple test again absent a new variable.

## V1.6A Phase 2 shadow-mirror acceptance criteria

1. No second `/dev/serial0` reader; sending serial chunks is **disabled unless explicitly enabled** with `KAIRIX_PICO_MIRROR_SOCKET`.
2. Live `/gfx`, operator reference controls and live native pose shared memory remain unchanged. Mirror output uses *different* `/dev/shm/kairix-reallines-shadow-pose-v1` and synthetic shadow-local reference IDs; **it is not production calibration truth**.
3. Mirror is Unix datagram, nonblocking. Socket backpressure **drops mirror chunks** instead of delaying the live reader; report source-side `shadow_mirror_datagrams_dropped` as well as shadow-side sequence/CRC counts. Datagram loss may cause increased mirror sequence gaps even if live UART CRC/gaps remain zero.
4. Compare measured shadow timing under typical user motion and Engineering activity with the 60 s synthetic benchmark and old live system. Evaluate p95/p99/max, execution outliers, stale ticks, packet gaps and actual count changes. Zero observed misses on one run does not guarantee determinism.
5. No auto enable at boot, no automatic service installation, no changes to firmware or hardware source, and no risky release cutover in this patch.

## Next milestone after this experiment (NOT supplied by this patch)

- Isolated tracker becomes **exclusive** Pico owner only after web application can be switched to an IPC consumer without opening a second UART port.
- Build authenticated/bounded operator command IPC: set/clear encoder references, profile/calibration and lens state changes, operator Show/Hide/lock semantics, stable state/ACK revisioning, safe retry and restart behaviour.
- Transfer *authoritative* configuration and reference identity transactionally. Avoid reading stale disk configuration every tracking tick. Re-reference on producer restart; do not silently preserve invalid references.
- GFX must stop relying on Uvicorn's 50 Hz publications for **production HDMI**; native renderer reads shared memory with deterministic deadline handling and no artificial smoothing. Web GFX may be secondary best-effort unless specifically validated.
- Run physical mode, latency, field cadence, stale-data, failover and logging/CPU stress acceptance; compare against baseline. PREEMPT_RT/CPU affinity only after process isolation and evidence.

## Source anchors and rollback

Repository `Dubcodes/kairix-reallines`, branch `wip/live-tuning-dot-lock`; working system rollback tag `checkpoint/pre-dedicated-pipeline-20261119` (`3a30fca`). New shadow baseline commit `e364284`. Preserve untracked `tools/`, any local patch scripts and user data. Prefer `git revert` for committed code; **never use `git reset --hard` indiscriminately**. Keep `kairix-reallines-native.service` disabled until explicitly accepted. To undo mirror test, first remove service environment override, restart and re-reference axes; the new shadow runner exits after its configured duration and does not own live UART.

