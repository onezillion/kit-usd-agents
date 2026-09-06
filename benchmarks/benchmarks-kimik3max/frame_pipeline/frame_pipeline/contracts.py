"""Encoder-independent data contracts for the frame pipeline benchmark.

These contracts deliberately avoid coupling the harness to any single encoder
library. Both the B0 (PyAV + h264_nvenc) and B1 (PyNvVideoCodec) backends
produce/consume these structures.

Stage 1 of the pipeline is::

    camera/render output -> capture -> prepare/copy/convert -> H.264 hw encode

Stage 1 ends at a valid encoded H.264 access-unit/bitstream (``EncodedAccessUnit``).
No RTP/Janus/WebRTC transport is modelled here (that is Stage 2, out of scope).

Memory-domain / transfer classification used throughout the benchmark:

* ``KNOWN_EXPLICIT_TRANSFER``  - an explicit copy we issue (memcpy / cudaMemcpy).
* ``KNOWN_GPU_LOCAL``          - an operation that stays on the GPU device.
* ``KNOWN_CPU``                - an operation that runs on the host CPU.
* ``API_HIDDEN_COMPOSITE``     - work hidden inside a library call whose internal
                                 copies/transfers we cannot time independently.
* ``UNKNOWN_UNPROVEN``         - asserted but not yet evidenced.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


class MemoryDomain(str, enum.Enum):
    """Where the frame buffer physically resides."""

    CPU = "cpu"
    CUDA = "cuda"
    UNKNOWN = "unknown"


class TransferClass(str, enum.Enum):
    """Honest classification of a memory/copy step."""

    KNOWN_EXPLICIT_TRANSFER = "known_explicit_transfer"
    KNOWN_GPU_LOCAL = "known_gpu_local"
    KNOWN_CPU = "known_cpu"
    API_HIDDEN_COMPOSITE = "api_hidden_composite"
    UNKNOWN_UNPROVEN = "unknown_unproven"


class PixelFormat(str, enum.Enum):
    RGBA = "rgba"
    RGB = "rgb"
    NV12 = "nv12"
    YUV444 = "yuv444"
    YUV420P = "yuv420p"
    UNKNOWN = "unknown"


def now_ns() -> int:
    """Monotonic nanosecond timestamp used for latency accounting."""

    return time.monotonic_ns()


def wall_ns() -> int:
    """Wall-clock nanosecond timestamp for cross-correlating with telemetry."""

    return time.time_ns()


@dataclass
class CapturedFrame:
    """A frame handed from a frame source to the preparation/encode stage.

    The ``buffer`` is an opaque object (e.g. ``numpy.ndarray`` for CPU frames).
    The harness never assumes zero-copy: ``memory_domain`` plus the transfer
    log in the harness describe where copies actually happen.
    """

    frame_id: int
    capture_ts_ns: int           # monotonic ns at capture/production
    width: int
    height: int
    pixel_format: PixelFormat
    memory_domain: MemoryDomain
    buffer: Any = None           # repr depends on backend/source (repr only, never assumed zero-copy)
    pitch_bytes: Optional[int] = None
    cuda_device: Optional[int] = None
    cuda_context: Optional[int] = None
    producer_stream: Optional[int] = None
    producer_event: Optional[int] = None
    # Name of a callable returning None that releases the buffer, if owned externally.
    release: Optional[Any] = None

    def summary(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "capture_ts_ns": self.capture_ts_ns,
            "width": self.width,
            "height": self.height,
            "pixel_format": self.pixel_format.value,
            "memory_domain": self.memory_domain.value,
            "pitch_bytes": self.pitch_bytes,
            "cuda_device": self.cuda_device,
        }


@dataclass
class EncodedAccessUnit:
    """A finished H.264 access unit; the end of Stage 1."""

    backend: str
    frame_id: int
    capture_ts_ns: int           # copied from CapturedFrame (latency baseline)
    encode_submit_ts_ns: int
    encode_complete_ts_ns: int
    pts: Optional[int]
    dts: Optional[int]
    codec: str                    # e.g. "h264"
    profile: Optional[str]        # parsed from SPS, filled by verifier
    is_keyframe: bool
    size_bytes: int
    bitstream: bytes = field(repr=False)
    extra: dict = field(default_factory=dict)

    @property
    def capture_to_encode_ns(self) -> int:
        return self.encode_complete_ts_ns - self.capture_ts_ns

    def to_record(self, include_bitstream: bool = False) -> dict:
        d = asdict(self)
        if not include_bitstream:
            d.pop("bitstream", None)
        else:
            d["bitstream_len"] = len(self.bitstream)
            d.pop("bitstream", None)
        d["capture_to_encode_ns"] = self.capture_to_encode_ns
        return d


@dataclass
class BackendCaps:
    """Reported/measured capability of an encode backend."""

    name: str
    version: str
    hardware_nvenc: bool           # MUST be True; silently-software backends are a failure
    hardware_proof: str            # short evidence string for the report
    input_formats: tuple = ()      # accepted PixelFormat values on the encode input
    gpu_direct_input: bool = False # True only if device-frame input proven to work
    notes: str = ""
    raw: dict = field(default_factory=dict)
