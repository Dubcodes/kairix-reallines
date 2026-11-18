from __future__ import annotations

import hashlib
import mmap
import os
import secrets
import struct
import time
import zlib
from pathlib import Path
from typing import Any


MAGIC = b"KRLPOSE1"
PROTOCOL_VERSION = 1
SLOT_COUNT = 64
HEADER_SIZE = 64
HEADER = struct.Struct("<8sIIIIQQQ")
# commit, pose sequence, producer id, acquisition/publication monotonic ns,
# Pico sequence/time, XYZ, pan/tilt/roll/FOV, flags, model/reference generations,
# payload checksum, trailing commit.
SLOT = struct.Struct("<QQQQQQQQdddddddIQQIQ")
FILE_SIZE = HEADER_SIZE + SLOT_COUNT * SLOT.size
FLAG_TRACKING_VALID = 1 << 0
FLAG_WORLD_VALID = 1 << 1
FLAG_INTEGRITY_TRUSTED = 1 << 2


class NativePoseError(RuntimeError):
    pass


def _identity64(parts: tuple[Any, ...]) -> int:
    text = "\x1f".join("" if value is None else str(value) for value in parts)
    return int.from_bytes(
        hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "little"
    )


class LatestPoseWriter:
    """Single-producer, multi-reader double-checked mmap pose ring."""

    def __init__(
        self, path: Path, *, clock_ns=time.monotonic_ns, producer_id: int | None = None
    ) -> None:
        self.path = Path(path)
        self.clock_ns = clock_ns
        self.producer_id = (
            int(producer_id if producer_id is not None else secrets.randbits(64)) or 1
        )
        self.sequence = 0
        self.generation = 0
        self.model_generation = 0
        self.reference_generation = 0
        self._model_identity: tuple[Any, ...] | None = None
        self._reference_identity: tuple[Any, ...] | None = None
        self._file = None
        self._map: mmap.mmap | None = None

    def open(self) -> None:
        if self._map is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("r+b" if self.path.exists() else "w+b")
        os.chmod(self.path, 0o640)
        if self.path.stat().st_size != FILE_SIZE:
            self._file.truncate(FILE_SIZE)
        self._map = mmap.mmap(self._file.fileno(), FILE_SIZE, access=mmap.ACCESS_WRITE)
        self._map[:] = b"\0" * FILE_SIZE
        HEADER.pack_into(
            self._map,
            0,
            MAGIC,
            PROTOCOL_VERSION,
            HEADER_SIZE,
            SLOT.size,
            SLOT_COUNT,
            self.producer_id,
            0,
            self.clock_ns(),
        )

    def publish(
        self,
        camera: dict[str, Any],
        tracking: dict[str, Any],
        *,
        acquisition_ns: int,
        pico_sequence: int = 0,
        pico_timestamp_us: int = 0,
        pico_receive_ns: int = 0,
        model_identity: tuple[Any, ...] = (),
        reference_identity: tuple[Any, ...] = (),
    ) -> int:
        if self._map is None:
            raise NativePoseError("pose writer is not open")
        if model_identity != self._model_identity:
            self.model_generation += 1
            self._model_identity = model_identity
        if reference_identity != self._reference_identity:
            self.reference_generation += 1
            self._reference_identity = reference_identity
        self.sequence += 1
        self.generation += 1
        generation = self.generation
        publication_ns = self.clock_ns()
        flags = 0
        if tracking.get("valid") is True and camera.get("valid") is True:
            flags |= FLAG_TRACKING_VALID
        if camera.get("world_valid") is True:
            flags |= FLAG_WORLD_VALID
        if tracking.get("integrity") == "TRUSTED":
            flags |= FLAG_INTEGRITY_TRUSTED
        values = (
            generation,
            self.sequence,
            self.producer_id,
            int(acquisition_ns),
            publication_ns,
            int(pico_sequence or 0),
            int(pico_timestamp_us or 0),
            int(pico_receive_ns or 0),
            float(camera.get("x", 0.0)),
            float(camera.get("y", 0.0)),
            float(camera.get("z", camera.get("height", 1.7))),
            float(camera.get("pan", 0.0)),
            float(camera.get("tilt", 0.0)),
            float(camera.get("roll", 0.0)),
            float(camera.get("fov", 60.0)),
            flags,
            self.model_generation,
            self.reference_generation,
        )
        payload = struct.pack("<QQQQQQQQdddddddIQQ", *values)
        checksum = zlib.crc32(payload) & 0xFFFFFFFF
        offset = HEADER_SIZE + (generation % SLOT_COUNT) * SLOT.size
        SLOT.pack_into(self._map, offset, *values, checksum, generation)
        # Publishing the active generation is the final commit.  mmap writes are
        # memory visibility only; deliberately do not flush to storage per pose.
        struct.pack_into("<Q", self._map, HEADER.size - 16, generation)
        return self.sequence

    def publish_store(
        self,
        store: Any,
        samples: dict[str, dict[str, Any]],
        acquisition_timestamp: float,
    ) -> int:
        axes = store.tracking.get("axes", {})
        pico = next(
            (
                sample
                for sample in samples.values()
                if sample.get("packet_seq") is not None
            ),
            {},
        )
        model_identity = (
            store.profile_index.get("active_id"),
            store.world_calibration_index.get("active_id"),
            store.world_calibration.get("updated"),
            store.world_calibration.get("valid"),
            repr(store.profile.get("rig_offset")),
        )
        reference_identity = tuple(
            axes.get(axis, {}).get("reference_id") for axis in ("pan", "tilt", "zoom")
        )
        return self.publish(
            store.camera,
            store.tracking,
            acquisition_ns=int(acquisition_timestamp * 1_000_000_000),
            pico_sequence=int(pico.get("packet_seq") or 0),
            pico_timestamp_us=int(pico.get("pico_timestamp_us") or 0),
            pico_receive_ns=int(float(pico.get("sample_mono") or 0.0) * 1_000_000_000),
            model_identity=model_identity,
            reference_identity=reference_identity,
        )

    def close(self) -> None:
        if self._map is not None:
            self._map.close()
            self._map = None
        if self._file is not None:
            self._file.close()
            self._file = None


class LatestPoseReader:
    def __init__(
        self, path: Path, *, stale_seconds: float = 0.25, clock_ns=time.monotonic_ns
    ) -> None:
        self.path = Path(path)
        self.stale_ns = int(max(0.01, stale_seconds) * 1_000_000_000)
        self.clock_ns = clock_ns
        self._file = None
        self._map: mmap.mmap | None = None
        self.last_producer_id: int | None = None

    def open(self) -> None:
        self._file = self.path.open("rb")
        if self.path.stat().st_size != FILE_SIZE:
            raise NativePoseError("incompatible pose shared-memory size")
        self._map = mmap.mmap(self._file.fileno(), FILE_SIZE, access=mmap.ACCESS_READ)
        header = HEADER.unpack_from(self._map, 0)
        if header[:5] != (MAGIC, PROTOCOL_VERSION, HEADER_SIZE, SLOT.size, SLOT_COUNT):
            self.close()
            raise NativePoseError("incompatible pose protocol header")

    def read(self, generation: int | None = None) -> dict[str, Any]:
        if self._map is None:
            raise NativePoseError("pose reader is not open")
        header1 = HEADER.unpack_from(self._map, 0)
        active = int(header1[6])
        wanted = active if generation is None else int(generation)
        if wanted <= 0 or active - wanted >= SLOT_COUNT or wanted > active:
            raise NativePoseError("requested pose is unavailable")
        offset = HEADER_SIZE + (wanted % SLOT_COUNT) * SLOT.size
        values = SLOT.unpack_from(self._map, offset)
        header2 = HEADER.unpack_from(self._map, 0)
        if generation is None and header2[6] != active:
            raise NativePoseError("pose changed during read")
        if values[0] != wanted or values[-1] != wanted:
            raise NativePoseError("torn pose snapshot")
        payload = struct.pack("<QQQQQQQQdddddddIQQ", *values[:-2])
        if zlib.crc32(payload) & 0xFFFFFFFF != values[-2]:
            raise NativePoseError("corrupt pose snapshot")
        producer_restart = (
            self.last_producer_id is not None and self.last_producer_id != values[2]
        )
        self.last_producer_id = values[2]
        age_ns = max(0, self.clock_ns() - values[4])
        flags = values[15]
        return {
            "protocol_version": PROTOCOL_VERSION,
            "generation": wanted,
            "sequence": values[1],
            "producer_id": values[2],
            "producer_restart": producer_restart,
            "acquisition_mono_ns": values[3],
            "publication_mono_ns": values[4],
            "pico_sequence": values[5],
            "pico_timestamp_us": values[6],
            "pico_receive_mono_ns": values[7],
            "x": values[8],
            "y": values[9],
            "z": values[10],
            "pan": values[11],
            "tilt": values[12],
            "roll": values[13],
            "fov": values[14],
            "tracking_valid": bool(flags & FLAG_TRACKING_VALID),
            "world_valid": bool(flags & FLAG_WORLD_VALID),
            "integrity_trusted": bool(flags & FLAG_INTEGRITY_TRUSTED),
            "model_generation": values[16],
            "reference_generation": values[17],
            "age_ms": age_ns / 1_000_000.0,
            "stale": age_ns > self.stale_ns,
        }

    def latest_history(self, limit: int = 16) -> list[dict[str, Any]]:
        if self._map is None:
            raise NativePoseError("pose reader is not open")
        active = int(HEADER.unpack_from(self._map, 0)[6])
        first = max(1, active - min(SLOT_COUNT, max(1, int(limit))) + 1)
        result = []
        for generation in range(first, active + 1):
            try:
                result.append(self.read(generation))
            except NativePoseError:
                continue
        return result

    def close(self) -> None:
        if self._map is not None:
            self._map.close()
            self._map = None
        if self._file is not None:
            self._file.close()
            self._file = None
