"""Single-producer latest-only tracking snapshots. No disk flush on each update.

A fixed 4-slot ring prevents a producer from overwriting the currently read slot
except when the consumer falls more than three updates behind. CRC and a second
header check reject torn reads. Producer PID is *not* an authority token; the
owner Unix socket and explicit configuration token are the authority boundary.
"""
from __future__ import annotations

import json
import mmap
import os
import secrets
import struct
import time
import zlib
from pathlib import Path

MAGIC = b'KRLTRK01'
VERSION = 1
SLOTS = 4
SLOT_BYTES = 65536
HEADER = struct.Struct('<8sIIQQ')  # magic, version, reserved, producer ID, latest generation (8-byte aligned)
ENTRY = struct.Struct('<QII')   # generation, JSON bytes, CRC32
HEADER_BYTES = 64
SIZE = HEADER_BYTES + SLOTS * SLOT_BYTES


class TrackingMmapError(RuntimeError):
    pass


class TrackingWriter:
    def __init__(self, path: Path, clock=time.monotonic):
        self.path = Path(path)
        self.clock = clock
        self.handle = None
        self.mm = None
        self.generation = 0
        self.producer_id = secrets.randbits(64) or 1

    def open(self):
        if self.mm is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('r+b' if self.path.exists() else 'w+b')
        os.chmod(self.path, 0o640)
        if self.path.stat().st_size != SIZE:
            self.handle.truncate(SIZE)
        self.mm = mmap.mmap(self.handle.fileno(), SIZE)
        self.mm[:] = b'\0' * SIZE
        HEADER.pack_into(self.mm, 0, MAGIC, VERSION, 0, self.producer_id, 0)

    def write(self, payload: dict):
        if self.mm is None:
            raise TrackingMmapError('writer not open')
        data = json.dumps({'mono': self.clock(), **payload}, separators=(',', ':'), allow_nan=False).encode('utf8')
        if len(data) > SLOT_BYTES - ENTRY.size:
            raise TrackingMmapError('tracking snapshot exceeds fixed slot capacity')
        self.generation += 1
        generation = self.generation
        offset = HEADER_BYTES + generation % SLOTS * SLOT_BYTES
        self.mm[offset + ENTRY.size:offset + ENTRY.size + len(data)] = data
        ENTRY.pack_into(self.mm, offset, generation, len(data), zlib.crc32(data) & 0xffffffff)
        struct.pack_into('<Q', self.mm, HEADER.size - 8, generation)
        return generation

    def close(self):
        if self.mm is not None:
            self.mm.close()
            self.mm = None
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class TrackingReader:
    def __init__(self, path: Path, *, stale_seconds=0.10, clock=time.monotonic):
        self.path = Path(path)
        self.stale_seconds = stale_seconds
        self.clock = clock
        self.handle = None
        self.mm = None
        self.last_producer = None

    def open(self):
        if self.mm is not None:
            return
        if not self.path.exists() or self.path.stat().st_size != SIZE:
            raise TrackingMmapError('tracking mmap missing or incompatible')
        self.handle = self.path.open('rb')
        self.mm = mmap.mmap(self.handle.fileno(), SIZE, access=mmap.ACCESS_READ)
        if HEADER.unpack_from(self.mm, 0)[:2] != (MAGIC, VERSION):
            self.close()
            raise TrackingMmapError('tracking mmap protocol mismatch')

    def read(self):
        if self.mm is None:
            self.open()
        first = HEADER.unpack_from(self.mm, 0)
        if first[:2] != (MAGIC, VERSION) or first[4] == 0:
            raise TrackingMmapError('no tracking snapshot')
        offset = HEADER_BYTES + first[4] % SLOTS * SLOT_BYTES
        generation, length, crc = ENTRY.unpack_from(self.mm, offset)
        if generation != first[4] or not (0 < length <= SLOT_BYTES - ENTRY.size):
            raise TrackingMmapError('torn tracking snapshot')
        raw = self.mm[offset + ENTRY.size:offset + ENTRY.size + length]
        second = HEADER.unpack_from(self.mm, 0)
        if first != second or zlib.crc32(raw) & 0xffffffff != crc:
            raise TrackingMmapError('tracking snapshot changed during read')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise TrackingMmapError('invalid tracking JSON') from exc
        mono = float(value['mono'])
        age = self.clock() - mono
        if not (0 <= age <= self.stale_seconds):
            raise TrackingMmapError('tracking producer stale or clock invalid')
        value['producer_id'] = first[3]
        value['generation'] = generation
        value['age_ms'] = age * 1000
        return value

    def close(self):
        if self.mm is not None:
            self.mm.close()
            self.mm = None
        if self.handle is not None:
            self.handle.close()
            self.handle = None
