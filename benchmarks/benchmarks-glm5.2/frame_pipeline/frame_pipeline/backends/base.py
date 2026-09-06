"""Common backend interface for H.264 encoders in the KHL Frame Pipeline Benchmark.

A backend is constructed with a frozen settings dict, then `encode_frame(cf)` is called
per CapturedFrame and returns (optional EncodedAccessUnit list, summary_dict).  A backend
never silently falls back to a software encoder — it must raise `BackendUnavailable`
instead, so the harness can record the failure cleanly.

Backends expose:
  - accept_format(): the memory domain they consume (host_numpy or gpu_device + format)
  - prepare(cf) -> (frame_obj, summary): adapter step mapping CapturedFrame.frame_obj to
    the backend's required representation, with timing + copy accounting
  - encode_frame(cf) -> list[EncodedAccessUnit]: synchronous encode (we measure app queue
    separately; NVENC async is NOT supported per GetEncoderCaps async_encode_support=0)
  - flush() -> list[EncodedAccessUnit]
  - sps_pps() -> bytes
  - close()
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from ..contracts import CapturedFrame, EncodedAccessUnit


class BackendUnavailable(RuntimeError):
    """Raised when a backend cannot be initialized (e.g. NVENC truly unavailable).

    Distinct from a software fallback: a backend that *silently* fell back to a software
    encoder is a BENCHMARK FAILURE and must be detected by `verify_hardware()` instead.
    """


@dataclass
class BackendSettings:
    codec: str = "h264"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    bitrate: int = 10_000_000
    max_bitrate: int = 10_000_000
    # common equivalents
    preset: str = "P1"          # P1 == fastest
    tuning: str = "ultra_low_latency"
    rate_control: str = "cbr"
    bf: int = 0                 # B-frames
    lookahead: int = 0
    aq: int = 0
    temporalaq: int = 0
    gop: int = 60               # reproducible GOP (= 2*fps at 30, documented)
    idr_period: int = 60
    repeatspspps: int = 1
    # backend-specific
    profile_b0: str = "main"    # B0 PyAV can express this; matches the job target
    # B1 cannot select profile — disclosed mismatch; forced High by PyNvVideoCodec.
    gpu_id: int = 0


class EncoderBackend(ABC):
    name: str = "abstract"
    accept_memory_domain: str = "host_numpy"
    accept_pixel_format: str = "NV12"

    def __init__(self, settings: BackendSettings):
        self.settings = settings
        self._closed = False

    @abstractmethod
    def verify_hardware(self) -> dict:
        """Return a proof dict proving hardware NVENC is in use (no silent SW fallback).

        Must include at least {'hardware': bool, 'evidence': str}.
        """

    @abstractmethod
    def prepare(self, cf: CapturedFrame) -> tuple[object, dict]:
        """Adapt CapturedFrame.frame_obj -> backend input + return (frame_obj, summary).

        summary must include timing + explicit copy counts so the harness can attribute
        copies.  summary keys: capture_cb_duration_s, color_convert_duration_s,
        frame_prep_duration_s, copies {d2d,d2h,h2d}, memory_domain_in, memory_domain_out.
        """

    @abstractmethod
    def encode_frame(self, cf: CapturedFrame) -> List[EncodedAccessUnit]:
        """Synchronous encode of one captured frame.  May return 0..N access units."""

    @abstractmethod
    def flush(self) -> List[EncodedAccessUnit]:
        """Drain the encoder queue."""

    @abstractmethod
    def sps_pps(self) -> bytes:
        """Return raw SPS+PPS bytes with start codes (for the verifier)."""

    def close(self):
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
