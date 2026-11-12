# Pico UART integration — authoritative firmware protocol V1 (63-byte A5 5A)

**Status: Pi receiver patch V1.2; protocol-level tested against a real captured packet. End-to-end hardware receive after installing this patch remains to be tested.**

## Authoritative implementation and provenance

The RP2040 was flashed from `Kairix-RealLines-Pico-Migration-V1.zip`, with source at `reallines-pico-migration/firmware/pico`. That firmware transmits 63-byte frames beginning `a5 5a`. It has already transmitted a packet captured and independently CRC-validated on the Pi. The RealLines branch `a4469df` mistakenly introduced a second `KRP1`/58-byte firmware/receiver contract; the host could open `/dev/serial0` but could not decode its frames. The 63-byte source is now the **only authoritative format** in `firmware/pico/` and `app/pico_protocol.py`. Do not reflash the Pico to fix the original protocol mismatch.

Firmware source, PIO table, and license text were restored from the uploaded original flashed-firmware **source package**. The UF2 binary flashed on the real device was built from a local Windows checkout with an SDK-import adjustment; no bit-identical firmware rebuild comparison has yet been performed.

## Electrical and firmware decisions

- Board: RP2040 USB-C development board, use printed GPIO labels, not original Pico physical-pin numbering.
- Pan A/B GP0/1, Tilt GP2/3, Zoom GP4/5, reserved Focus GP6/7.
- Active-low momentary buttons GP10/11/12, firmware debounce 20 ms, cumulative modulo-65536 press counters; no automatic action bound to buttons.
- UART1 TX GP8 → Raspberry Pi 4 GPIO15 RX (physical pin 10); Pico UART RX GP9 ← Pi GPIO14 TX (pin 8; reserved). Shared ground, Pico powered over USB-C from Pi USB-A.
- 600 PPR NPN open-collector incremental encoders, full ×4 PIO decoding. A/B: RP2040 internal pull-ups **plus** six external 4.7 kΩ resistors to **Pico 3.3 V**; encoder supply 5 V regulated with common ground. Never connect 5 V directly to an RP2040 GPIO. Use short/ground-paired signal runs and replace breadboard before production.
- PIO counts continuously. Firmware sends cumulative **signed 64-bit** counts at nominal 500 Hz over 1,000,000-baud UART 8N1. Lost UART snapshots do not lose physical counts, but packet CRC does not prove physical encoder signal integrity.
- `pico_us` uses the Pico local clock, never a Pi clock. The Pi's acquisition/pose timestamps remain host-monotonic.

## Exact packet contract

`struct.Struct('<2sBBIQI4qB3HHH')` — 63 bytes, little-endian.

| Bytes | Field |
|---:|---|
| 0..1 | Magic A5 5A |
| 2 | Protocol version = 1 |
| 3 | Frame type = 1 (tracking) |
| 4..7 | uint32 sequence |
| 8..15 | uint64 microseconds since Pico boot |
| 16..19 | uint32 random boot/session ID |
| 20..51 | four signed int64 cumulative counts: Pan/Tilt/Zoom/Focus (Focus reserved=0) |
| 52 | active-low-debounced button held-state mask (3 bits) |
| 53..58 | three uint16 cumulative button-press totals |
| 59..60 | uint16 firmware metadata flags |
| 61..62 | uint16 CRC16/CCITT-FALSE (init FFFF, polynomial 1021) of first 61 bytes, LE |

Flags **are not** hardware encoder fault reports:
- `0x0001` watchdog-caused reboot (sampled on Pico boot; does not mean active count loss).
- `0x0002` Focus reserved/inactive.
- `0xFF00` contains firmware major/minor version; `0x0100` = version 1.0.
- `0x0102` is a normal healthy/boot metadata value. V1 has **no physical encoder error-counter or signal-integrity flag**. Other unknown flag bits are surfaced as metadata, not interpreted as a count failure.

### Real Pi capture — canonical golden vector

The terminal capture contained 64 bytes, where the **last** `a5` begins the next packet. The first 63 bytes give CRC `0xB5F1`, sequence `163559`, Pico timestamp `327117850 us`, session ID `980923745`, Pan `-72`, Tilt `0`, Zoom `10`, Focus `0`, zero button events, flags `0x0102`. `tests/test_pico_uart.py` contains the exact bytes and tests that serialise back byte-for-byte.

## Raspberry Pi 4 UART — verified preconditions

The operating setup uses `/dev/serial0` → `/dev/ttyAMA0` (hardware PL011), user `reallines` has access via `dialout` and serial console is **disabled**. `/boot/firmware/config.txt` has `enable_uart=1` and `dtoverlay=disable-bt`, with Bluetooth disabled to free PL011. The UART has already been verified with `stty -F /dev/serial0 1000000 raw -echo -ixon -ixoff -crtscts` and a raw `head | od` capture. The host receiver uses Python's `os` / `termios` / `select` — **no pyserial dependency**. Do not run two readers simultaneously; temporarily stop RealLines if making a manual raw UART capture.

## What changes in this patch

- Accept fixed 63-byte `A5 5A` frames; reject wrong type/version, CRC failures, and out-of-order sequence; resynchronise and bound memory in noise.
- Correct `pico_source` mapping, timestamp/uptime, version/reset/Focus informational flags; preserve three-axis separate reference IDs and cumulative packet-loss diagnostics.
- Preserve 64-bit count values after a missing UART packet. A Pico boot-session change or substantial UART silence forces fresh camera-position references, **without deleting any saved dot observations**.
- Report valid packet receive rate, CRC/protocol errors, byte resync, Pico uptime, metadata flags, connection state.
- Keep all Direct GPIO, Linux Rotary, simulator and current RealLines graphics/Zoom calibration pathways intact.

## Deploy without firmware reflashing

1. Apply patch only to a **clean** Windows checkout of `wip/live-tuning-dot-lock` at `a4469df`.
2. Run `python -m pytest tests -q`, `node tests/editor_math.test.mjs`, `node --test tests/frontend_contract.test.mjs`, `git diff --check`; review changes; commit and push **only if tests pass**.
3. On Pi, confirm a clean checkout, `git pull --ff-only origin wip/live-tuning-dot-lock`, run `.venv/bin/python -m unittest discover -s tests -q`, restart `kairix-reallines-dev`.
4. Keep Pico flashed as-is. In Engineering, select Pico UART for Pan/Tilt/Zoom. Verify **RECEIVING** (not OPEN—WAITING), valid packet counter increasing and packet rate around 500 Hz. Confirm CRC and protocol errors remain zero, session ID and firmware `1.0` visible, flags `0x0102` informational.
5. Rotate Pan/Tilt/Zoom individually, confirm cumulative counts respond and return to baseline; then reference each position and capture Zoom endpoints as needed. Test a long idle, rapid movement and forced Pico restart for reference invalidation. **Do not clear old world measurements.** New physical source/reference IDs may invalidate an accepted world; a guided alignment recovery mechanism remains future work.
6. Record diagnostic bundle and assess packet rate, CRC errors, gaps, count drift and visual tracking before trusting for broadcast use. Invalid PIO A/B transitions currently do not get a dedicated hardware error count: a future firmware enhancement is still needed.

## Future firmware builds

For the RP2040 board, with official Pico SDK installed (and `PICO_SDK_PATH` set or local `pico_sdk_import.cmake` supplied):

```sh
cmake -S firmware/pico -B firmware/pico/build -DPICO_BOARD=pico
cmake --build firmware/pico/build -j4
```

`firmware/pico/main.c` and `quadrature_encoder.pio` now match the original firmware source, but an actual UF2 rebuild and binary equivalence check **has not been performed**. Keep the existing Pico firmware until testing justifies an update. The repository's `tools/flash_pico.py` is a separate local-only flashing utility; no public web endpoint should flash hardware.

## Workflow and folder management

The repository is the source of truth for application and firmware source; use Git branch `wip/live-tuning-dot-lock` until merged deliberately. `data/` and Pi diagnostic logs must never be deleted by a source-code cleanup. The supplied Windows `cleanup-project.ps1` **previews** moving old installer bundles and backup folders into a dated archive outside Git. The original firmware build and tripod imagery remain untouched until hardware tests have passed.
