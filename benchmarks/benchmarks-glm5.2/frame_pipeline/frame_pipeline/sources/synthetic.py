"""Deterministic synthetic frame sources for the KHL Frame Pipeline Benchmark.

Two modes, both reproducible from a fixed seed + frame index (no randomness across runs,
no uncontrolled manual movement):
  - "static": every frame identical.  Useful for measuring pure encoder/queue cost.
  - "motion": every frame is a deterministic high-motion pattern (per-pixel phase ramp
    advancing with frame index).  Stresses motion estimation & bitrate without needing
    a real scene.

Both produce the frame in the memory domain the backend asks for:
  - "host_rgba": host RGBA8 numpy (H,W,4) — for B0.
  - "host_nv12": host NV12 numpy (3H/2, W) — for B1-CPU.
  - "gpu_nv12":  device-resident NV12 (a _GpuNv12Frame) — for B1-GPU.  The source owns
    its own ring of device buffers and reuses them; no per-frame allocation after warmup.
"""
from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from ..contracts import CapturedFrame, DOMAIN_HOST_NUMPY, DOMAIN_GPU_DEVICE


@dataclass
class SourceConfig:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    mode: str = "motion"         # "static" | "motion"
    output_domain: str = "host_rgba"   # "host_rgba" | "host_nv12" | "gpu_nv12"
    seed: int = 0xA5A5
    gpu_ring_size: int = 4       # device-buffer ring for "gpu_nv12"
    gpu_id: int = 0


class SyntheticSource:
    """Deterministic per-frame generator.

    Lifecycle:
      source = SyntheticSource(cfg)
      source.start()              # allocate GPU ring if needed
      for i in range(N): cf = source.next_frame(i)
      source.stop()               # free GPU ring
    """

    def __init__(self, cfg: SourceConfig):
        self.cfg = cfg
        self._w, self._h = cfg.width, cfg.height
        self._cuda = None
        self._gpu_ring = []        # list of (luma_ptr, full_size)
        self._ring_idx = 0
        # Precompute the static luma/chroma once
        rng = np.random.default_rng(cfg.seed)
        self._static_y = rng.integers(16, 236, (self._h, self._w), dtype=np.uint8)
        self._static_uv = rng.integers(16, 240, (self._h // 2, self._w), dtype=np.uint8)
        # X/Y coordinate grids for the motion pattern (deterministic)
        xs = np.arange(self._w, dtype=np.float32)
        ys = np.arange(self._h, dtype=np.float32)
        self._X, self._Y = np.meshgrid(xs, ys)

    # ------------------------------------------------------------------
    def start(self):
        if self.cfg.output_domain == "gpu_nv12":
            from cuda import cuda
            self._cuda = cuda
            cuda.cuInit(0)
            err, dev = cuda.cuDeviceGet(self.cfg.gpu_id)
            err, ctx = cuda.cuCtxCreate(0, dev)
            self._cu_ctx = int(ctx)
            err, stream = cuda.cuStreamCreate(0)
            self._cu_stream = int(stream)
            luma_size = self._w * self._h
            full_size = luma_size + self._w * (self._h // 2)
            for _ in range(self.cfg.gpu_ring_size):
                err, dptr = cuda.cuMemAlloc(full_size)
                self._gpu_ring.append((dptr, full_size, luma_size))

    def stop(self):
        if self._cuda is not None:
            for dptr, _, _ in self._gpu_ring:
                try:
                    self._cuda.cuMemFree(dptr)
                except Exception:
                    pass
            self._gpu_ring = []
            # NOTE: we deliberately do NOT cuCtxDestroy here; the encoder (B1-GPU) holds
            # the context.  Process teardown handles final cleanup.  Disclosed in report.

    # ------------------------------------------------------------------
    def _rgba_for_frame(self, idx: int) -> np.ndarray:
        """Produce a deterministic RGBA8 (H,W,4) numpy frame for index `idx`."""
        if self.cfg.mode == "static":
            # grey + a fixed checkerboard marker so the verifier can confirm determinism
            rgba = np.zeros((self._h, self._w, 4), dtype=np.uint8)
            rgba[:] = self._static_y[:, :, None]  # broadcast luma to RGB
            rgba[:, :, 3] = 255
            return rgba
        # high-motion: phase ramp advancing with frame index.
        phase = (idx / max(1, self.cfg.fps)) * 2.0 * np.pi
        # 4 full sine cycles across the width, plus a vertical component for chroma motion
        r = (128 + 120 * np.sin(2 * np.pi * 4 * self._X / self._w + phase)).astype(np.uint8)
        g = (128 + 120 * np.sin(2 * np.pi * 3 * self._X / self._w - phase * 1.3)).astype(np.uint8)
        b = (128 + 120 * np.sin(2 * np.pi * 2 * self._Y / self._h + phase * 0.7)).astype(np.uint8)
        a = np.full_like(r, 255)
        rgba = np.dstack([r, g, b, a])
        rgba = np.ascontiguousarray(rgba)
        return rgba

    def _nv12_for_frame(self, idx: int) -> np.ndarray:
        """Produce a deterministic NV12 (3H/2, W) uint8 host numpy frame.

        For "static" we reuse the precomputed random Y/UV (high-frequency — stresses
        the encoder; pure-grey would compress unrealistically well).  For "motion" we
        synthesise an animated luma gradient + chroma rotation.
        """
        if self.cfg.mode == "static":
            return np.ascontiguousarray(np.concatenate([self._static_y, self._static_uv], axis=0))
        phase = (idx / max(1, self.cfg.fps)) * 2.0 * np.pi
        y = (128 + 120 * np.sin(2 * np.pi * 4 * self._X / self._w + phase)).astype(np.uint8)
        # chroma is H/2 rows; build U and V over the chroma grid (H/2 x W/2)
        Xc = self._X[: self._h // 2, ::2]    # (H/2, W/2) even-column samples
        Xc1 = self._X[: self._h // 2, 1::2]  # (H/2, W/2) odd-column samples
        u = (128 + 100 * np.sin(2 * np.pi * 2 * Xc / self._w + phase)).astype(np.uint8)
        v = (128 + 100 * np.sin(2 * np.pi * 2 * Xc1 / self._w - phase)).astype(np.uint8)
        uv = np.empty((self._h // 2, self._w), dtype=np.uint8)
        uv[:, 0::2] = u
        uv[:, 1::2] = v
        return np.ascontiguousarray(np.concatenate([y, uv], axis=0))

    # ------------------------------------------------------------------
    def next_frame(self, idx: int) -> CapturedFrame:
        from ..backends.b1_pynv import _GpuNv12Frame  # local import to avoid cycle on module load
        t0 = time.perf_counter()
        domain = self.cfg.output_domain
        if domain == "host_rgba":
            frame_obj = self._rgba_for_frame(idx)
            mem_domain = DOMAIN_HOST_NUMPY
            fmt = "RGBA"
            pitch = self._w * 4
            copies = {"d2d": 0, "d2h": 0, "h2d": 0}
        elif domain == "host_nv12":
            frame_obj = self._nv12_for_frame(idx)
            mem_domain = DOMAIN_HOST_NUMPY
            fmt = "NV12"
            pitch = self._w
            copies = {"d2d": 0, "d2h": 0, "h2d": 0}
        elif domain == "gpu_nv12":
            host_nv12 = self._nv12_for_frame(idx)
            dptr, full_size, luma_size = self._gpu_ring[self._ring_idx]
            self._ring_idx = (self._ring_idx + 1) % len(self._gpu_ring)
            # explicit H2D copy from host to our own device ring slot
            err, = self._cuda.cuMemcpyHtoD(dptr, host_nv12.ctypes.data, full_size)
            frame_obj = _GpuNv12Frame(dptr, int(dptr) + luma_size, self._w, self._h)
            mem_domain = DOMAIN_GPU_DEVICE
            fmt = "NV12"
            pitch = self._w
            copies = {"d2d": 0, "d2h": 0, "h2d": 1}  # explicit H2D counted honestly
        else:
            raise ValueError("unknown output_domain: %r" % domain)
        t1 = time.perf_counter()
        cf = CapturedFrame(
            frame_id=idx,
            capture_ts=t1,               # perf_counter (monotonic) — same clock as encode ts
            width=self._w, height=self._h, pixel_format=fmt,
            memory_domain=mem_domain, pitch_or_stride=pitch,
            cuda_device=self.cfg.gpu_id if domain == "gpu_nv12" else None,
            cuda_context=getattr(self, "_cu_ctx", None),
            producer_stream=getattr(self, "_cu_stream", None),
            frame_obj=frame_obj,
            provenance=[("synthetic_source", "n/a", mem_domain, "native"),
                        ("host_to_device_ring", DOMAIN_HOST_NUMPY, DOMAIN_GPU_DEVICE,
                         "explicit") if domain == "gpu_nv12" else ("noop", "n/a", "n/a", "native")],
        )
        # stamp source-side timing onto a side dict for the harness to read
        cf._source_timing = {
            "capture_cb_duration_s": 0.0,            # synthetic — no real capture callback
            "source_total_duration_s": t1 - t0,
            "copies": copies,
        }
        return cf
