"""Correctness verification: H.264 parse + decode + PSNR vs the ACTUAL captured
source frame (frame_id-matched), never vs a reconstruction (Lesson G).

The encoder workerm, when run with retain_au>0, stores (frame_id, source_rgba,
encoded_au) triples in memory and also writes one .h264 sample per stream. For a
robust offline PSNR we instead re-encode a KNOWN captured source frame through an
identical encoder config and align via frame_id.

This module provides helpers used by scripts/verify_correctness.py.
"""

from __future__ import annotations

from .verify_h264 import verify_bitstream, psnr  # re-export


def decode_first_frame(au_bytes: bytes):
    """Decode the first frame of an Annex-B H.264 access unit; returns an
    (H, W, 3) uint8 RGB ndarray, or None if nothing decodes."""
    import av
    dec = av.codec.CodecContext.create("h264", "r")
    frames = list(dec.decode(av.packet.Packet(au_bytes))) + list(dec.decode(None))
    if not frames:
        return None
    return frames[0].to_ndarray(format="rgb24")


def rgba_to_rgb(rgba, width: int, height: int):
    """Drop the alpha channel of an RGBA8 source frame to (H, W, 3) RGB."""
    import numpy as np
    arr = np.frombuffer(rgba, dtype=np.uint8).reshape((height, width, 4))
    return arr[..., :3]
