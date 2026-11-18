# Native latest-pose protocol v1

RealLines publishes a bounded binary pose ring at `/dev/shm/kairix-reallines-pose-v1` by default. It is local IPC, not persistent state. The file is 9,792 bytes: a 64-byte header followed by 64 packed 152-byte slots. All integers are unsigned little-endian and doubles are IEEE-754 little-endian.

## Header

| Offset | Type | Meaning |
|---:|---|---|
| 0 | char[8] | `KRLPOSE1` |
| 8 | u32 | Protocol version (`1`) |
| 12 | u32 | Header bytes (`64`) |
| 16 | u32 | Slot bytes (`152`) |
| 20 | u32 | Slot count (`64`) |
| 24 | u64 | Random nonzero producer identity |
| 32 | u64 | Active committed generation |
| 40 | u64 | Producer creation time in Pi monotonic nanoseconds |
| 48 | byte[16] | Reserved, zero |

## Pose slot

| Offset | Type | Meaning |
|---:|---|---|
| 0 | u64 | Opening commit generation |
| 8 | u64 | Monotonic pose sequence within this producer |
| 16 | u64 | Producer identity |
| 24 | u64 | Tracking acquisition time, Pi monotonic ns |
| 32 | u64 | IPC publication time, Pi monotonic ns |
| 40 | u64 | Pico packet sequence |
| 48 | u64 | Pico timestamp, microseconds in the Pico clock domain |
| 56 | u64 | UART receipt time, Pi monotonic ns |
| 64..119 | 7 × f64 | Camera X, Y, Z, Pan, Tilt, Roll and horizontal FOV |
| 120 | u32 | Flags: bit 0 tracking valid, bit 1 world valid, bit 2 integrity trusted |
| 124 | u64 | Camera model/calibration generation |
| 132 | u64 | Encoder-reference generation |
| 140 | u32 | CRC-32 over bytes 0..139 |
| 144 | u64 | Closing commit generation |

The writer fills `generation % 64`, writes both slot commit values and CRC, then publishes the same generation at header offset 32. A reader reads the header generation, copies that slot, rereads the header, and accepts only an unchanged header, matching opening/closing generations and valid CRC. Obsolete slots are overwritten; no producer or consumer queue grows.

Producer restart creates a new producer identity, resets pose sequence/generation, and reinitializes the same mmap inode so existing mappings are not truncated. Readers reject missing, torn, corrupt, stale or incompatible snapshots. Renderer policy defaults to blank output when tracking/world state is invalid or stale; an explicit diagnostic `hold` policy is available.

Pico timestamps are retained for correlation but are not directly subtracted from Pi timestamps. Acquisition, UART receipt, IPC publication and native render times share the Pi monotonic clock domain.
