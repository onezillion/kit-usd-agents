"""Deterministic synthetic frame source.

Generates reproducible RGBA frames for the POC. Two content modes are provided:

* ``static``  - a fixed scene (identical every frame).
* ``motion``  - a repeatable high-motion scene (scrolling texture + a fast
  bouncing block + a frame-index counter bar) that stresses the encoder.

The *same* numpy generator produces the pixel content for both the CPU source
(used by B0) and the CUDA source (uploaded once to device by B1), so the two
backends encode genuinely identical frame content for a given
``(mode, seed, frame_index)``.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from .contracts import CapturedFrame, MemoryDomain, PixelFormat, now_ns


class SceneGenerator:
    """Deterministic RGBA scene renderer (numpy, host memory)."""

    def __init__(self, width: int, height: int, mode: str, seed: int = 1337) -> None:
        if mode not in ("static", "motion"):
            raise ValueError("mode must be 'static' or 'motion'")
        self.width = width
        self.height = height
        self.mode = mode
        self.seed = seed
        # Precomputed coordinate grids (float32) reused across frames.
        ys, xs = np.mgrid[0:height, 0:width]
        self.xs = xs.astype(np.float32)
        self.ys = ys.astype(np.float32)
        # Fixed random background texture from the seed (static across frames).
        rng = np.random.default_rng(seed)
        self.base_r = (0.5 + 0.5 * np.sin(xs * 0.021 + ys * 0.013)).astype(np.float32)
        self.base_g = (0.5 + 0.5 * np.sin(xs * 0.017 + ys * 0.011 + 1.3)).astype(np.float32)
        self.base_b = (0.5 + 0.5 * np.cos(xs * 0.008 + ys * 0.019 + 2.1)).astype(np.float32)
        self.checker = (((xs // 64).astype(np.int64) + (ys // 64).astype(np.int64)) % 2).astype(np.float32)
        self.noise = rng.random((height, width), dtype=np.float32)

    def render_index(self, index: int) -> np.ndarray:
        """Return an (H, W, 4) uint8 RGBA frame for a given frame index."""

        xs, ys = self.xs, self.ys
        if self.mode == "static":
            phase = 0.0
        else:
            phase = float(index) * 0.35  # fast but deterministic motion
        # Scrolling base: shift sin/cos argument by phase -> apparent motion.
        r = (0.5 + 0.5 * np.sin((xs + phase * 8.0) * 0.021 + (ys + phase * 2.0) * 0.013))
        g = (0.5 + 0.5 * np.sin((xs + phase * 3.0) * 0.017 + ys * 0.011 + 1.3))
        b = (0.5 + 0.5 * np.cos(xs * 0.008 + (ys + phase * 5.0) * 0.019 + 2.1))
        # Blend with checker + noise so I/P frames differ meaningfully in motion mode.
        mix = 0.35 if self.mode == "motion" else 0.15
        r = (1.0 - mix) * r + mix * self.checker
        g = (1.0 - mix) * g + mix * self.noise
        b = (1.0 - mix) * b + mix * self.base_b
        frame = np.empty((self.height, self.width, 4), dtype=np.uint8)
        frame[..., 0] = np.clip(r * 255.0, 0, 255).astype(np.uint8)
        frame[..., 1] = np.clip(g * 255.0, 0, 255).astype(np.uint8)
        frame[..., 2] = np.clip(b * 255.0, 0, 255).astype(np.uint8)
        frame[..., 3] = 255
        if self.mode == "motion":
            self._add_bouncing_block(frame, index)
        return frame

    def _add_bouncing_block(self, frame: np.ndarray, index: int) -> None:
        """Deterministic fast-moving block + frame-index bar (high motion)."""

        h, w = self.height, self.width
        bw, bh = w // 6, h // 4
        # Lissajous path, period depends only on index for repeatability.
        bx = int((w - bw) * (0.5 + 0.5 * np.sin(index * 0.11)))
        by = int((h - bh) * (0.5 + 0.5 * np.sin(index * 0.163 + 1.0)))
        frame[by:by + bh, bx:bx + bw, 0:3] = np.array([255, 64, 16], dtype=np.uint8)
        # Frame-index bar sweeps horizontally; encodes the index visibly.
        barx = int((index * 37) % max(1, w - 8))
        frame[0:24, barx:barx + 8, 0:3] = np.array([255, 255, 255], dtype=np.uint8)


class SyntheticSource:
    """Produces CapturedFrame objects at a fixed cadence from a SceneGenerator.

    The same generator instance can back either a CPU or a CUDA source; the
    pixel content for ``(mode, seed, index)`` is identical either way.
    """

    def __init__(self, generator: SceneGenerator, domain: MemoryDomain = MemoryDomain.CPU) -> None:
        self.gen = generator
        self.domain = domain
        self._last_index_rendered: Optional[int] = None
        self._cached_rgba: Optional[np.ndarray] = None

    def frame_count_for(self, duration_s: float, fps: int) -> int:
        return int(round(duration_s * fps))

    def make_frame(self, frame_id: int) -> CapturedFrame:
        """Build one CapturedFrame. CPU domain returns the numpy RGBA buffer."""

        if self._cached_rgba is None or self._last_index_rendered != frame_id:
            self._cached_rgba = self.gen.render_index(frame_id)
            self._last_index_rendered = frame_id
        return CapturedFrame(
            frame_id=frame_id,
            capture_ts_ns=now_ns(),
            width=self.gen.width,
            height=self.gen.height,
            pixel_format=PixelFormat.RGBA,
            memory_domain=self.domain,
            buffer=self._cached_rgba,
            pitch_bytes=self.gen.width * 4,
        )
