"""Encoder-independent data contracts for the KHL Frame Pipeline Benchmark.

These dataclasses intentionally avoid coupling to any specific encoder library. They
represent the minimum information needed to compare capture/encode pipelines fairly.

All timestamps are POSIX seconds (float, perf_counter-based for monotonic deltas;
capture timestamps may be wall-clock for live sources).  Memory-domain classifications
are explicit strings so that the report can distinguish proven GPU-resident paths from
API-hidden or unknown domains.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# Memory domains — exact strings, do not freehand.  These are the categories required
# by the benchmark's evidence discipline (known explicit transfer / GPU-local / CPU /
# API-hidden / unknown).
DOMAIN_GPU_DEVICE = "gpu_device"        # CUDA device memory (proven CAI v3)
DOMAIN_HOST_NUMPY = "host_numpy"        # NumPy ndarray in host memory
DOMAIN_HOST_PILLAR = "host_pinned"      # Page-locked/pinned host buffer
DOMAIN_KIT_CAPSULE = "kit_capsule"      # Kit ByteCapture native capsule (treated as host staging)
DOMAIN_API_HIDDEN = "api_hidden"        # Copy/conversion hidden inside a 3rd-party API
DOMAIN_UNKNOWN = "unknown"              # Not proven at measurement time


@dataclass
class CapturedFrame:
    """A frame ready to be handed to an encoder backend.

    `frame_obj` is the backend-specific input (numpy array for CPU-input encoders, or an
    object exposing `.cuda()` returning a list of __cuda_array_interface__ dicts for
    GPU-input encoders).  We never try to dereference the pointer here; this contract only
    records metadata about ownership and domain so the report can attribute copies.
    """
    frame_id: int
    capture_ts: float
    width: int
    height: int
    pixel_format: str                  # "NV12", "RGBA", ...
    memory_domain: str                 # one of DOMAIN_*
    pitch_or_stride: Optional[int] = None
    cuda_device: Optional[int] = None
    cuda_context: Optional[int] = None
    producer_stream: Optional[int] = None
    # Lifetime ownership flags — True means the producer has transferred ownership to the
    # consumer of this CapturedFrame; the consumer must release it.
    release_ownership: bool = False
    # Backend-specific opaque handle.  We do NOT introspect this here.
    frame_obj: Any = None
    # Provenance: which explicit copies/transfers took place before this object existed,
    # as a list of (stage, domain_from, domain_to, kind) tuples.  kind in
    # {"explicit","api_hidden","conversion","native"}.  Kept by the producer.
    provenance: list = field(default_factory=list)

    def summary_dict(self) -> dict:
        # NOTE: asdict() recursively processes frame_obj, which for GPU sources holds a
        # CUdeviceptr that cannot be deepcopied/pickled.  Build the dict manually instead.
        d = {
            "frame_id": self.frame_id,
            "capture_ts": self.capture_ts,
            "width": self.width,
            "height": self.height,
            "pixel_format": self.pixel_format,
            "memory_domain": self.memory_domain,
            "pitch_or_stride": self.pitch_or_stride,
            "cuda_device": self.cuda_device,
            "cuda_context": self.cuda_context,
            "producer_stream": self.producer_stream,
            "release_ownership": self.release_ownership,
            "provenance": list(self.provenance),
        }
        return d


@dataclass
class EncodedAccessUnit:
    """An encoded H.264 access unit produced by a backend.

    `data` may be None if the backend queued the AU internally and only the metadata is
    available at measurement time (then `data` is filled in at flush).  For correctness
    validation we always retain the encoded bytes of at least the verifier sample set.
    """
    frame_id: int
    source_capture_ts: float
    encode_submit_ts: float            # when the producer called Encode()
    encode_completion_ts: float        # when encoded bytes became available to us
    pts: Optional[int] = None
    dts: Optional[int] = None
    codec: str = "h264"
    profile: Optional[str] = None      # "main", "high", ... decoded from SPS later
    is_keyframe: bool = False
    encoded_size_bytes: int = 0
    backend: str = ""                  # "b0_pyav", "b1_pynv_cpu", "b1_pynv_gpu"
    backend_metadata: dict = field(default_factory=dict)
    data: Optional[bytes] = None       # raw AU bytes (may be omitted for non-sample frames)

    def summary_dict(self) -> dict:
        d = asdict(self)
        if self.data is not None:
            d["data_len"] = len(self.data)
        d.pop("data", None)
        return d
