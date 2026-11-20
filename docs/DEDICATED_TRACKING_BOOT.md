# Dedicated tracking at boot — V1.6D

This deploys the **existing V1.6C code** (commit `35273fe` or descendants) as the normal, enabled Pi boot mode. It does **not** rewrite tracking or GFX logic. Raspberry Pi Pico firmware is unchanged.

## Architecture

- `kairix-reallines-tracking.service` owns `/dev/serial0`, uses canonical StateStore mapping at 200 Hz, and publishes native 200 Hz pose and 50 Hz detailed state to two RAM files.
- `kairix-reallines-dev.service` remains the original Uvicorn application. A systemd drop-in directs it to the owner Unix socket and detailed tracking mmap, and orders it after the owner is accepting requests.
- `RuntimeDirectory` creates `/run/kairix-reallines/owner.sock` and removes stale socket paths on service shutdown. The owner remains the only UART reader. Avoid running `app.pico_owner` or `app.tracking_owner` manually after installation.
- systemd `BindsTo=` and `After=` stop the web process if the owner stops. The tracking service uses `Restart=on-failure` and `Upholds=` so a restarted owner can bring up a web process that resends its calibration model. Do not assume session-only physical references survive any process restart.
- **No automatic referencing**: the operator must set Pan, Tilt and Zoom positions after boot or tracking service restart. Persisted setup profiles, lens FOV values and calibration files are not erased. Physically captured Zoom wide/tight encoder endpoints are session-only.
- Dedicated mode fixes the large approximately 2-second stutter in the observed A/B test. The web GFX still depends on Uvicorn WebSocket publication and may have smaller remaining jitter. Native HDMI/1080i50 timing remains unverified.

## Before installing

1. Ensure the code and tests for `35273fe` are deployed on the Pi and the web service is currently in legacy mode.
2. End old manually launched owners using Ctrl+C. Confirm with `pgrep -af 'app.tracking_owner|app.pico_owner'`.
3. Confirm no old `95-pico-owner.conf` remains. The installer refuses to override it.
4. This changes boot services and requires re-referencing cameras; **do this offline, not during a broadcast**.

## Install

```bash
cd /home/reallines/kairix-reallines
.venv/bin/python -m unittest discover -s tests -p 'test_tracking_service_check.py' -v
sudo bash scripts/reallines_tracking_mode.sh install
sudo bash scripts/reallines_tracking_mode.sh status
```

Expected: both services active and enabled; owner health reports `configured: true` after the web service startup. Check journal for errors:

```bash
systemctl is-enabled kairix-reallines-tracking.service kairix-reallines-dev.service
systemctl is-active kairix-reallines-tracking.service kairix-reallines-dev.service
journalctl -u kairix-reallines-tracking.service -u kairix-reallines-dev.service -n 60 --no-pager
cd /home/reallines/kairix-reallines
.venv/bin/python -m app.tracking_service_check --configured
```

Then re-reference Pan/Tilt/Zoom in Engineering. Test continuous pan under normal browser/Engineering load.

## Reboot acceptance

**Reboot only when no live graphics are required.** After successful install:

```bash
sudo reboot
```

After SSH reconnect:

```bash
systemctl is-enabled kairix-reallines-tracking.service kairix-reallines-dev.service
systemctl is-active kairix-reallines-tracking.service kairix-reallines-dev.service
cd /home/reallines/kairix-reallines
.venv/bin/python -m app.tracking_service_check --configured --wait 10
```

Both services must become active without manual terminals. The operator must **re-reference the axes**; `tracking.valid=false` before referencing is the safe expected state.

## Explicit rollback to legacy mode

```bash
cd /home/reallines/kairix-reallines
sudo bash scripts/reallines_tracking_mode.sh rollback
```

This removes only the new dedicated web drop-in and disables the new owner service; it **does not delete data or change other drop-ins**. Re-reference the physical encoders. To re-enable later after rollback, run `install` again; the installer accepts the previously installed service unit when its contents match the patch.

**Important**: the tracking service `Upholds=` the web service. For deliberate web maintenance, stop the tracking service first or the web service will automatically restart. `systemctl restart kairix-reallines-dev.service` only restarts the web service. Restarting the tracking owner invalidates physical references and will restart web as necessary. The operator must re-reference after either operation where the owner/configuration has lost its references.

## Remaining acceptance and limitations

- CPU, 200 Hz processing overruns and GFX end-to-end delivery under heavy Engineering load require further validation.
- Pi reboot, owner crash/restart, missing Pico, changing active profile, changing live zoom endpoints, and IMU-optional behaviour need physical bench acceptance before a real race day.
- systemd readiness only checks that the owner's health RPC responds; it does not assert calibrated/valid tracking. Avoid an automatic failover to another UART reader. No duplicate UART readers, smoothing or intentional sample buffering.
