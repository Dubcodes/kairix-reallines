# RealLines Pico integration — decision and test record (13 Nov 2026 NZDT)

## Versions and provenance

- Windows project working tree `J:\projects\kairix-reallines\kairix-reallines-v0.0.1-dev`.
- Pi project `/home/reallines/kairix-reallines`; service `kairix-reallines-dev`.
- Observed deployed branch `wip/live-tuning-dot-lock`, commit `a4469df` (210 Python tests passed on Windows and Pi; JavaScript suite passed).
- Actual Pico firmware originally compiled from Windows `Pico Programming\reallines-pico-migration\firmware\pico`, based on `Kairix-RealLines-Pico-Migration-V1.zip`. Pico SDK build succeeded in VS Code; original UF2 was flashed over BOOTSEL.
- Wiring: GP0–5 Pan/Tilt/Zoom A/B, GP8 TX→Pi RX GPIO15, GP9 RX←Pi GPIO14, buttons GP10–12; internal and external 4.7kΩ pull-ups to 3V3, regulated encoder 5V supply, shared ground.
- Pi verified `/dev/serial0 → /dev/ttyAMA0`, baud 1,000,000; `dialout` permission, serial console disabled, Bluetooth disabled for PL011; raw bytes captured successfully.

## Root cause

RealLines at `a4469df` expects 58-byte ASCII `KRP1` packets but the Pico actually sends 63-byte binary `A5 5A` packets. Host reported `OPEN — WAITING`, zero received packets. Hardware transmission was proven good by manual raw UART capture and CRC validation.

Valid captured frame: seq 163559; Pico timestamp 327117850 microseconds; session 980923745; Pan -72, Tilt 0, Zoom 10, Focus 0; flags 0x0102; CRC B5F1. Flag interpretation: firmware v1.0 and Focus reserved. No dedicated encoder signal-integrity fault bit is implemented.

## Chosen solution

Keep flashed firmware, update RealLines host to its original 63-byte protocol, reconcile repository firmware source from that same archived original. Do not change physical wiring or flash firmware for this protocol fix. Preserve existing camera configuration, world calibration and all six saved dot observations; new tracking source/reference identity may still require reference restoration and new-world verification.

## Patch V1.2 verification

- The uploaded original Pico firmware and original host protocol were inspected, and current `a4469df` receiver compared using repository Git blob hashes.
- Exact real 63-byte UART fixture round-trips byte-for-byte with new decoder and passes CRC; tests cover fragmentation, noise, wrong version/type, CRC recovery, extreme int64 counts, cumulative packet gaps, duplicates, 3-button rollover, metadata flag interpretation, Pico restart and stale-reference behavior.
- Isolated Python protocol tests passed in the patch-build environment; the full RealLines checkout tests, Pi hardware receive test and Pico UF2 rebuild **remain pending** until the user's next test run.
- The full project cleanup remains a separate operator-controlled operation; default is preview, applying merely **moves** selected older bundles/backups into `_archive`, with optional cache deletion only when requested.

## Acceptance checks after deployment

Pico controller display: `Link RECEIVING`, ~500 packets/s, increasing packet count/sequence, stable session and valid uptime, CRC and protocol errors 0, normal firmware flags 0x0102 and no false fault. Pan/Tilt/Zoom counts move independently, hold still, and return to original counts after out-and-back physical movement. UART sequence gaps do not imply physical missed encoder pulses. A Pico reboot or extended link outage invalidates position references; the operator explicitly re-references before trusting world graphics. Capture a diagnostic ZIP and compare with this record.

## Deferred work

Physical quadrature signal error detection in firmware; held-out camera calibration across Pan/Tilt/Zoom ranges; reference recovery without deleting historical measurements; optional button actions; firmware update UX with trusted access; broadcast-grade signal hardware (PCB rather than breadboard). No untested changes here.
