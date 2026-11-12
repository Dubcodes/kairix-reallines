# Kairix RealLines — RP2040 Pico UART acquisition (V1.1)

**Base:** V9.1 commit `a344800` on `wip/live-tuning-dot-lock`. This is a hardware-testing candidate. Preserve your existing `data/` directory and the current V9.1 calibration profile. The first hardware bring-up must be done while graphics output is not on air.

## What changes

- A new **Pico UART** tracking source for Pan, Tilt and Zoom. Existing Direct GPIO, Linux Rotary and Simulator choices remain available.
- A **single shared UART receiver**, independent of the 200 Hz application acquisition loop, decodes 500 frames/s. The Pico's PIO counts every ×4 quadrature transition continuously.
- Full signed 64-bit **cumulative** counts. Losing one UART packet does not lose encoder counts; the next valid frame replaces the previous snapshot.
- CRC-16, sequence, boot-session ID, age/staleness, packet gaps, errors, button press counters and basic health on Engineering.
- Pi-side reference IDs/positions remain separate for each axis. Restarting the Pico clears all references; no silent zeroing or reusing an old camera pose.
- Physical Zoom with Wide/Tight FOV capture accepts Pico source with the same reference rules as Direct GPIO.
- Firmware C source and the matching binary wire contract, plus offline tests and a **local CLI** firmware updater. Firmware flashing is **not** exposed through an unauthenticated HTTP endpoint.

## Wiring — board GPIO labels (not standard Pico header pin numbers)

| Function | RP2040 pad | Other end |
|---|---|---|
| Pan A / B | GP0 / GP1 | Green / white encoder wires |
| Tilt A / B | GP2 / GP3 | Green / white encoder wires |
| Zoom A / B | GP4 / GP5 | Green / white encoder wires |
| Future Focus | GP6 / GP7 | Reserved; no decoding yet |
| UART1 TX | GP8 | Raspberry Pi **GPIO15 RX**, physical pin **10** |
| UART1 RX | GP9 | Raspberry Pi **GPIO14 TX**, physical pin **8** (reserved) |
| Button 1/2/3 | GP10/GP11/GP12 | Momentary button to GND |
| Ground | GND | Pi GND, physical pin 6, and encoder ground |
| Power | Pico USB-C | USB-A port on Pi; don't bridge two 3V3 supplies |
| Encoder red (5V) | Regulated 5 V | Not connected to GPIO |

The firmware enables **internal pull-ups** on all A/B and button inputs. Install the **external 4.7 kΩ pull-up from each encoder A/B to Pico 3V3**, near the Pico. Internal and external pull-ups coexist. NEVER pull-up an RP2040 GPIO to 5 V. Verify the encoder is genuinely NPN open collector, not push-pull 5 V, before connecting. Keep common grounds, strain relief, and signal wiring short or twisted with GND. For production, replace the breadboard.

## Pi UART prerequisites (Raspberry Pi 4)

Enable UART hardware, disable the login shell on serial, and select stable **PL011** hardware UART on GPIO14/15. On Raspberry Pi OS, use `sudo raspi-config` -> Interface Options -> Serial Port: **login shell No, serial hardware Yes**. If Bluetooth owns PL011, configure `dtoverlay=disable-bt` and `enable_uart=1` in `/boot/firmware/config.txt` (where supported), then reboot. Check actual device mapping rather than assuming it:

```bash
ls -l /dev/serial0 /dev/ttyAMA0
stty -F /dev/serial0 1000000 raw -echo
id -nG reallines
```

Add the service user to `dialout` if required (`sudo usermod -aG dialout reallines`), then reboot or restart login/session so the membership takes effect. RealLines defaults to `/dev/serial0` **1,000,000 baud, 8N1**, no flow control, no Python pyserial dependency. To override the path on the Pi, add `Environment=KAIRIX_PICO_UART_DEVICE=/dev/ttyAMA0` to a `systemctl edit kairix-reallines-dev.service` drop-in, `daemon-reload`, and restart. Do not start two serial consumers at once.

## Build the included firmware (requires installed official Pico SDK)

The **matched** wire protocol is `KRP1` 58 bytes, documented below. A firmware built using a different earlier Pico package may use a different protocol. Install the firmware included in **this** patch before enabling `Pico UART` in Engineering.

```bash
cd /home/reallines/kairix-reallines/firmware/pico
export PICO_SDK_PATH=/path/to/installed/pico-sdk
cmake -S . -B build -DPICO_BOARD=pico
cmake --build build -j2
```

Copy `build/reallines_pico.uf2` to the Pico using its USB bootloader. On the Pi, if `picotool` is available and the Pico runs firmware with compatible USB stdio support, an operator can do a verified local update:

```bash
sha256sum build/reallines_pico.uf2
python3 tools/flash_pico.py firmware/pico/build/reallines_pico.uf2 --sha256 <exact-output-from-sha256sum>
```

The command above runs from the RealLines repo root. It requires a human to type `FLASH` and invokes `picotool load -f -v -x`. Physical BOOTSEL remains the fallback if software-triggered updating is unavailable. There is no automatic flashing. **Firmware compilation and flashing have not been validated on your USB-C board yet.** Board-specific PICO_BOARD options may need adjusting.

## Install RealLines patch on Windows

From the extracted bundle beside your existing checkout:

```powershell
cd "J:\projects\kairix-reallines"
.\reallines-pico-v1_1\install.ps1
```

The installer requires clean commit `a344800`, runs all replacement preflight checks, and creates a timestamped copy of changed files before applying. It never accesses `data/`.

Then:

```powershell
cd "J:\projects\kairix-reallines\kairix-reallines-v0.0.1-dev"
python -m compileall -q app tests
python -m pytest tests -q
node tests/editor_math.test.mjs
node --test tests/frontend_contract.test.mjs
git diff --check
git diff --stat
```

Commit only after reviewing the diff and passing tests. Push to `wip/live-tuning-dot-lock`, then pull on Pi. Run the Pi unit tests before restarting its service.

## Bring-up

1. Confirm UART wires and board's GPIO pad labels. Confirm firmware matches `KRP1`.
2. Open Engineering: **Pico encoder controller** should show `RECEIVING`, last packet under about 20 ms, increasing sequence, stable boot session, zero CRC errors.
3. Select **Pico UART** for Pan, then Tilt, then Zoom. Reference each at a known physical position. Existing gear ratios/directions/PPR remain on RealLines; Pico is fixed ×4.
4. Slowly move each axis and check counts change smoothly with correct direction. Stop and confirm held counts don't drift. Test fast movement and long idle; watch packet gaps, CRC and controller restarts.
5. Reference Zoom and capture physical Wide/Tight points again (saved FOV numbers remain available).
6. Observe that old world calibration is *invalidated* by hardware-source/encoder-reference change. **Old six Dot observations and world solution are preserved on disk**, but old raw counts cannot be relabelled as a new Pico counting session. Establish new physical references and collect new matched observations for world solve. Do not clear old data without a deliberate backup.

## Protocol KRP1, V1

Binary, little endian, 58-byte fixed frame at 500 Hz (2 ms nominal). 1 Mbit/s UART 8N1 => roughly 29% wire usage. Fields by byte offset:

| Offset | Bytes | Value |
|---:|---:|---|
| 0 | 4 | ASCII `KRP1` |
| 4 | 4 | sequence, uint32, wrapping |
| 8 | 4 | random count-session ID, uint32, changes on reboot |
| 12 | 8 | Pico monotonic time, uint64 µs |
| 20 | 8 | Pan cumulative count, int64 |
| 28 | 8 | Tilt cumulative count, int64 |
| 36 | 8 | Zoom cumulative count, int64 |
| 44 | 1 | Three debounced button states, bitmask |
| 45 | 6 | Three uint16 cumulative press counters |
| 51 | 1 | Fault flags, initially 0 |
| 52 | 4 | Device uptime, uint32 ms |
| 56 | 2 | CRC16-CCITT-FALSE (poly 0x1021, init 0xffff) over first 56 bytes |

The host discards out-of-order/duplicate packets, checks CRC, and accepts fresh cumulative counts even when sequence gaps are detected. It does not infer encoder transitions from packet deltas. An unchanged count is a valid stationary camera; an unchanged packet timestamp indicates a stale UART link. USB firmware version isn't communicated in this minimal protocol; `KRP1` reports wire protocol version 1, not a claim of verified firmware release.

## Future improvements

- Compare 50 Hz vs 100 Hz WebSocket pose publication independently from Pico reporting, with timing instrumentation and no slow-client queue buildup.
- Add measured end-to-end latency and local Pi HDMI 1080p50 experiment.
- Add signed, revisioned web-based .UF2 firmware update once Engineering authentication and hardware verification exist. Until then the CLI avoids an unsafe remote flashing endpoint.
- Validate the firmware's PIO counts against a known pulse generator/oscilloscope and confirm the actual USB-C RP2040 board's PIO/USB/PIN details.
