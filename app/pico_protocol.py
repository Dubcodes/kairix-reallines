"""RealLines Pico UART V1 protocol: complete cumulative counts, not deltas.

All fields are little-endian. A 58-byte packet is transmitted at 500 Hz.
This module is pure Python to make the binary contract testable on Windows.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"KRP1"
PACKET = struct.Struct("<4sIIQqqqBHHHBIH")
FRAME_BYTES = PACKET.size
MAX_BUFFER = 8192


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
    button_bits: int
    button_presses: tuple[int, int, int]
    fault_flags: int
    uptime_ms: int


def encode_packet(packet: PicoPacket) -> bytes:
    body = PACKET.pack(MAGIC, packet.sequence, packet.session_id, packet.pico_us,
                       packet.pan, packet.tilt, packet.zoom, packet.button_bits,
                       *packet.button_presses, packet.fault_flags, packet.uptime_ms, 0)
    return body[:-2] + struct.pack("<H", crc16_ccitt(body[:-2]))


class PicoStreamDecoder:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.crc_errors = 0
        self.dropped_bytes = 0
        self.valid_packets = 0

    def feed(self, data: bytes) -> list[PicoPacket]:
        if not data:
            return []
        self.buffer.extend(data)
        if len(self.buffer) > MAX_BUFFER:
            overflow = len(self.buffer) - MAX_BUFFER
            del self.buffer[:overflow]
            self.dropped_bytes += overflow
        packets: list[PicoPacket] = []
        while len(self.buffer) >= FRAME_BYTES:
            start = self.buffer.find(MAGIC)
            if start == -1:
                drop = max(0, len(self.buffer) - len(MAGIC) + 1)
                self.dropped_bytes += drop
                del self.buffer[:drop]
                break
            if start:
                self.dropped_bytes += start
                del self.buffer[:start]
            if len(self.buffer) < FRAME_BYTES:
                break
            raw = bytes(self.buffer[:FRAME_BYTES])
            if crc16_ccitt(raw[:-2]) != int.from_bytes(raw[-2:], "little"):
                self.crc_errors += 1
                self.dropped_bytes += 1
                del self.buffer[0]
                continue
            unpacked = PACKET.unpack(raw)
            packets.append(PicoPacket(
                sequence=unpacked[1], session_id=unpacked[2], pico_us=unpacked[3],
                pan=unpacked[4], tilt=unpacked[5], zoom=unpacked[6], button_bits=unpacked[7],
                button_presses=(unpacked[8], unpacked[9], unpacked[10]),
                fault_flags=unpacked[11], uptime_ms=unpacked[12],
            ))
            self.valid_packets += 1
            del self.buffer[:FRAME_BYTES]
        return packets
