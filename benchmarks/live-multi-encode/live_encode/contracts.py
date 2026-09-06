"""Encoder-independent data contracts + separated event counters.

Adapted (with thanks) from the read-only reference
``benchmarks/benchmarks-kimik3max/frame_pipeline/frame_pipeline/contracts.py``
for this job's Stage-1 *live* multi-camera pipeline. Deliberately avoids coupling
to any single encoder library.

Stage 1::

    live render product -> capture -> bounded transfer/SHM -> prep/convert
    -> H.264 hw encode -> EncodedAccessUnit

No RTP/Janus/WebRTC (Stage 2) is modelled.

Memory-domain / transfer classification (report-level honesty):

* ``KNOWN_EXPLICIT_D2H``  - device->host readback (e.g. ByteCapture LdrColor).
* ``KNOWN_EXPLICIT_H2H``  - host->host copy (ctypes.memmove into SHM / numpy copy).
* ``KNOWN_EXPLICIT_H2D``  - host->device upload (encoder input staging).
* ``KNOWN_GPU_LOCAL``     - stays on the GPU device.
* ``KNOWN_CPU``           - host CPU work (color conversion).
* ``API_HIDDEN_COMPOSITE``- copies hidden inside a library call we cannot time apart.
* ``UNKNOWN_UNPROVEN``    - asserted but not yet evidenced.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


class MemoryDomain(str, enum.Enum):
    CPU = "cpu"
    CUDA = "cuda"
    UNKNOWN = "unknown"


class TransferClass(str, enum.Enum):
    KNOWN_EXPLICIT_D2H = "known_explicit_d2h"
    KNOWN_EXPLICIT_H2H = "known_explicit_h2h"
    KNOWN_EXPLICIT_H2D = "known_explicit_h2d"
    KNOWN_GPU_LOCAL = "known_gpu_local"
    KNOWN_CPU = "known_cpu"
    API_HIDDEN_COMPOSITE = "api_hidden_composite"
    UNKNOWN_UNPROVEN = "unknown_unproven"


class PixelFormat(str, enum.Enum):
    RGBA = "rgba"
    BGRA = "bgra"
    NV12 = "nv12"
    YUV420P = "yuv420p"
    UNKNOWN = "unknown"


def mono_ns() -> int:
    """Monotonic nanosecond timestamp; the ONLY source for latency deltas."""

    return time.perf_counter_ns()


def wall_ns() -> int:
    """Wall-clock ns for cross-correlation with telemetry / logs only."""

    return time.time_ns()


# A fixed-size slot header prepended to every SHM slot by the producer so the
# consumer can (a) detect torn/parent-overwrite and (b) recover the frame id.
# Layout (little endian, 32 bytes, padded):
#   u64 magic      = SHM_SLOT_MAGIC
#   u64 frame_id
#   u64 capture_ts_ns   (Kit perf_counter_ns domain)
#   u32 payload_crc32   (CRC32 of the RGBA payload bytes only)
#   u32 payload_bytes
SLOT_HEADER_BYTES = 32
SHM_SLOT_MAGIC = 0x4B484C5F4C4D4531  # "KHL_LME1"


@dataclass
class FrameSlot:
    """Descriptor for one SHM slot handed producer->consumer via the signal queue."""

    stream_id: str
    slot_index: int
    frame_id: int
    capture_ts_ns: int     # Kit perf_counter_ns at capture-callback completion
    width: int
    height: int
    pixel_format: PixelFormat = PixelFormat.RGBA


@dataclass
class EncodedAccessUnit:
    """A finished H.264 access unit; the end of Stage 1."""

    backend: str
    stream_id: str
    frame_id: int
    capture_ts_ns: int           # copied from producer (cross-process: use offset)
    encode_submit_ts_ns: int     # subprocess perf_counter_ns
    encode_complete_ts_ns: int   # subprocess perf_counter_ns
    pts: Optional[int]
    is_keyframe: bool
    size_bytes: int
    bitstream: bytes = field(repr=False)
    extra: dict = field(default_factory=dict)

    def to_record(self, include_bitstream: bool = False) -> dict:
        d = asdict(self)
        d.pop("bitstream", None)
        if include_bitstream:
            d["bitstream_len"] = len(self.bitstream)
        return d


@dataclass
class StreamCounters:
    """Separated counters mandated by the job. An encoder-loop iteration is NOT an
    encoded frame; ``encoded_access_units`` increments only on a real H.264 AU."""

    capture_requested: int = 0
    capture_completed: int = 0
    frames_transferred: int = 0      # producer memcpy into SHM succeeded
    frames_enqueued: int = 0        # slot idx placed on the signal queue
    frames_replaced: int = 0        # freshness eviction of an un-consumed slot
    frames_dropped: int = 0         # producer could not enqueue (queue full, cap err)
    encode_submitted: int = 0       # encoder.encode() called
    encoded_access_units: int = 0   # a real H.264 AU was produced
    torn_frames: int = 0            # consumer crc/frame_id mismatch (overwrite race)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class BackendCaps:
    name: str
    version: str
    hardware_nvenc: bool           # MUST be True; silent-software is a failure
    hardware_proof: str
    input_formats: tuple = ()
    gpu_direct_input: bool = False
    notes: str = ""
    raw: dict = field(default_factory=dict)
