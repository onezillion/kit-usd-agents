"""Bounded preallocated shared-memory ring with torn-frame detection.

One ``ShmRing`` per stream. Producer (Kit capture callback) writes a slot then
signals the slot index on a bounded queue; consumer (encoder subprocess) reads the
slot and validates a per-slot header. This addresses the overwrite race in a
plain ring where the producer can wrap and rewrite a slot the consumer is
mid-read on.

Layout per slot:
    [ SLOT_HEADER_BYTES header ][ payload (RGBA8 w*h*4) ]

Header (packed into the first SLOT_HEADER_BYTES bytes of the slot, little endian):
    u64 magic
    u64 frame_id
    u64 capture_ts_ns
    u32 payload_crc32
    u32 payload_bytes

The producer writes the payload FIRST, then computes CRC, then writes the header
LAST (with the magic written as the final 8 bytes). The consumer reads the header,
snapshots (frame_id, crc), reads the payload, recomputes CRC, and finally re-reads
the magic+frame_id; if any changed, the producer overwrote mid-read -> torn.
"""

from __future__ import annotations

import struct
import zlib
from multiprocessing import shared_memory
from typing import Optional

from .contracts import (
    SLOT_HEADER_BYTES, SHM_SLOT_MAGIC, mono_ns,
)

_HDR = struct.Struct("<QQQII")  # magic, frame_id, capture_ts, crc32, payload_bytes
assert _HDR.size <= SLOT_HEADER_BYTES, _HDR.size


class ShmRingError(RuntimeError):
    pass


def slot_stride(width: int, height: int) -> int:
    return SLOT_HEADER_BYTES + width * height * 4


class ProducerRing:
    """Owned by the Kit-side capture process. Creates the SHM.

    The producer copies from a raw ByteCapture capsule address straight into the
    SHM via a single ``ctypes.memmove`` (one known H2H copy), then writes the
    per-slot header last so the consumer can detect an overwrite. The SHM base
    address is taken once so the per-frame copy is a bare memmove, no slicing.
    """

    def __init__(self, name: Optional[str], stream_id: str, width: int, height: int,
                 nslots: int, create: bool = True):
        import ctypes
        self.stream_id = stream_id
        self.width, self.height = width, height
        self.nslots = max(2, nslots)
        self.payload_bytes = width * height * 4
        self.stride = slot_stride(width, height)
        self.total = self.stride * self.nslots
        if create:
            self.shm = shared_memory.SharedMemory(
                name=name, create=True, size=self.total)
        else:
            self.shm = shared_memory.SharedMemory(name=name)
        self.name = self.shm.name
        self.buf = self.shm.buf
        self._base = ctypes.addressof(ctypes.c_char.from_buffer(self.buf))
        self._frame_id = 0
        self._next_slot = 0

    def write_from_address(self, src_addr: int, capture_ts_ns: int) -> tuple[int, int]:
        """Copy ``payload_bytes`` from a raw CPU address into the next ring slot.

        Returns (slot_index, frame_id). Raises ``ShmRingError`` on NULL address.
        Order: pixel memmove -> crc over the SHM payload -> header pack. The header
        (with magic) is written last; ``frame_id`` increments monotonically per slot.
        """
        import ctypes
        if not src_addr:
            raise ShmRingError("null source address")
        slot = self._next_slot
        off = slot * self.stride
        payload_off = off + SLOT_HEADER_BYTES
        # 1) payload copy (H2H: capture buffer -> SHM)
        ctypes.memmove(self._base + payload_off, src_addr, self.payload_bytes)
        # 2) crc over the SHM payload copy
        crc = zlib.crc32(
            self.buf[payload_off:payload_off + self.payload_bytes]) & 0xFFFFFFFF
        frame_id = self._frame_id
        # 3) header last (magic in same packed write)
        _HDR.pack_into(self.buf, off, SHM_SLOT_MAGIC, frame_id, capture_ts_ns,
                       crc, self.payload_bytes)
        self._frame_id += 1
        self._next_slot = (slot + 1) % self.nslots
        return slot, frame_id

    def close(self, unlink: bool = True) -> None:
        try:
            self.shm.close()
        finally:
            if unlink:
                try:
                    self.shm.unlink()
                except FileNotFoundError:
                    pass


class ConsumerRing:
    """Owned by the encoder subprocess. Attaches to an existing SHM by name."""

    def __init__(self, name: str, stream_id: str, width: int, height: int, nslots: int):
        self.stream_id = stream_id
        self.width, self.height = width, height
        self.nslots = max(2, nslots)
        self.payload_bytes = width * height * 4
        self.stride = slot_stride(width, height)
        self.shm = shared_memory.SharedMemory(name=name)
        self.buf = self.shm.buf
        self.torn = 0

    def read(self, slot: int) -> tuple[memoryview, int, int, bool]:
        """Read slot -> (payload_view, frame_id, capture_ts_ns, ok).

        Validates the per-slot header; ``ok`` False means a torn/overwrite or bad
        magic was detected (caller counts it, does NOT encode).
        """
        off = slot * self.stride
        try:
            magic, frame_id, cts, crc, nbytes = _HDR.unpack_from(self.buf, off)
        except struct.error:
            self.torn += 1
            return memoryview(b""), -1, 0, False
        if magic != SHM_SLOT_MAGIC or nbytes != self.payload_bytes:
            self.torn += 1
            return memoryview(b""), -1, 0, False
        payload_off = off + SLOT_HEADER_BYTES
        view = self.buf[payload_off:payload_off + self.payload_bytes]
        # recompute crc over the payload snapshot
        data_crc = zlib.crc32(bytes(view)) & 0xFFFFFFFF
        # re-read header to detect overwrite during the crc computation
        magic2, frame_id2, _, crc2, _ = _HDR.unpack_from(self.buf, off)
        if (magic2 != SHM_SLOT_MAGIC or frame_id2 != frame_id
                or crc2 != crc or data_crc != crc):
            self.torn += 1
            return memoryview(b""), -1, 0, False
        return view, frame_id, cts, True

    def close(self) -> None:
        try:
            self.shm.close()
        except Exception:
            pass
