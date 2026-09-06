"""Encoder backend interface.

Both backends implement :class:`EncoderBackend`. The harness drives them
identically. ``uses_hardware_nvenc`` must be backed by actual evidence (engine
utilization and/or backend identity), never assumed: a silent software H.264
fallback is a benchmark failure and is surfaced via ``hardware_proof``.
"""

from __future__ import annotations

from typing import Any, Optional

from .contracts import BackendCaps, CapturedFrame, EncodedAccessUnit


class EncoderBackend:
    """Abstract encode backend (Stage 1 ends at an EncodedAccessUnit)."""

    name: str = "abstract"

    # -- lifecycle ----------------------------------------------------------
    def configure(self, width: int, height: int, fps: int, settings: dict) -> None:
        raise NotImplementedError

    def open(self) -> None:
        raise NotImplementedError

    # -- encode -------------------------------------------------------------
    def encode(self, frame: CapturedFrame) -> list[EncodedAccessUnit]:
        """Encode one captured frame; may return zero or more access units.

        Ultra-low-latency NVENC pipelines frequently buffer output and return an
        empty list for several frames; ``flush`` drains the remainder.
        """

        raise NotImplementedError

    def flush(self) -> list[EncodedAccessUnit]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    # -- capability ---------------------------------------------------------
    def caps(self) -> BackendCaps:
        raise NotImplementedError

    def __enter__(self) -> "EncoderBackend":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- owning GPU converter (optional) ------------------------------------
    def set_gpu_converter(self, conv: Any) -> None:
        self._gpu_converter = conv
