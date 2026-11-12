"""Authoritative RealLines Pico protocol V1, matching the *flashed* 63-byte firmware.

A5 5A, version 1, tracking type 1, cumulative signed 64-bit counts for
Pan/Tilt/Zoom/reserved Focus, 3 button counters, informational firmware flags,
and a CRC16/CCITT-FALSE. No physical encoder fault bit exists in firmware V1.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"\xa5\x5a"
VERSION = 1
FRAME_TYPE_TRACKING = 1
PACKET = struct.Struct("<2sBBIQI4qB3HHH")
FRAME_BYTES = PACKET.size  # 63
MAX_BUFFER = 8192
assert FRAME_BYTES == 63

# V1 flags are *metadata*, not quadrature/position-integrity reports.
FLAG_WATCHDOG_REBOOT = 0x0001
FLAG_FOCUS_RESERVED = 0x0002
KNOWN_FLAG_MASK = 0xFF03  # High byte encodes firmware major/minor version.


def crc16_ccitt(data: bytes, initial: int = 0xFFFF) -> int:
    crc = initial
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass(frozen=True)
class PicoPacket:
    sequence: int
    session_id: int
    pico_us: int
    pan: int
    tilt: int
    zoom: int
    button_bits: int = 0
    button_presses: tuple[int, int, int] = (0, 0, 0)
    focus: int = 0
    flags: int = 0x0102

    @property
    def uptime_ms(self) -> int:
        return self.pico_us // 1000

    @property
    def watchdog_reset(self) -> bool:
        return bool(self.flags & FLAG_WATCHDOG_REBOOT)

    @property
    def focus_reserved(self) -> bool:
        return bool(self.flags & FLAG_FOCUS_RESERVED)

    @property
    def firmware_version(self) -> str:
        return f"{(self.flags >> 8) & 0x0F}.{(self.flags >> 12) & 0x0F}"

    @property
    def unknown_flags(self) -> int:
        return self.flags & ~KNOWN_FLAG_MASK


def encode_packet(packet: PicoPacket) -> bytes:
    """Encoder for offline golden vectors; firmware is the live packet producer."""
    body = PACKET.pack(
        MAGIC, VERSION, FRAME_TYPE_TRACKING, packet.sequence, packet.pico_us,
        packet.session_id, packet.pan, packet.tilt, packet.zoom, packet.focus,
        packet.button_bits, *packet.button_presses, packet.flags, 0,
    )
    return body[:-2] + struct.pack("<H", crc16_ccitt(body[:-2]))


class PicoStreamDecoder:
    """Bounded byte-stream decoder with incremental resynchronisation."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.crc_errors = 0
        self.protocol_errors = 0
        self.dropped_bytes = 0
        self.valid_packets = 0

    def feed(self, data: bytes) -> list[PicoPacket]:
        if not data:
            return []
        self.buffer.extend(data)
        packets: list[PicoPacket] = []
        if len(self.buffer) > MAX_BUFFER:
            overflow = len(self.buffer) - MAX_BUFFER
            del self.buffer[:overflow]
            self.dropped_bytes += overflow
        while True:
            start = self.buffer.find(MAGIC)
            if start < 0:
                # Retain a trailing 0xA5 in case 0x5A arrives in the next read.
                keep = 1 if self.buffer[-1:] == MAGIC[:1] else 0
                dropped = len(self.buffer) - keep
                del self.buffer[:dropped]
                self.dropped_bytes += dropped
                break
            if start:
                del self.buffer[:start]
                self.dropped_bytes += start
            if len(self.buffer) < FRAME_BYTES:
                break
            raw = bytes(self.buffer[:FRAME_BYTES])
            if raw[2] != VERSION or raw[3] != FRAME_TYPE_TRACKING:
                self.protocol_errors += 1
                self.dropped_bytes += 1
                del self.buffer[0]
                continue
            if crc16_ccitt(raw[:-2]) != int.from_bytes(raw[-2:], "little"):
                self.crc_errors += 1
                self.dropped_bytes += 1
                del self.buffer[0]
                continue
            parts = PACKET.unpack(raw)
            packets.append(PicoPacket(
                sequence=parts[3], pico_us=parts[4], session_id=parts[5],
                pan=parts[6], tilt=parts[7], zoom=parts[8], focus=parts[9],
                button_bits=parts[10], button_presses=tuple(parts[11:14]),
                flags=parts[14],
            ))
            self.valid_packets += 1
            del self.buffer[:FRAME_BYTES]
        return packets
