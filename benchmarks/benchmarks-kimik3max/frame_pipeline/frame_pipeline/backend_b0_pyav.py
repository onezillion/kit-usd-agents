"""B0 backend: PyAV + libx264/h264_nvenc, reproducing the production architecture.

The production WebRTC extension (reference only, NOT modified) does:

    ByteCapture (LdrColor, RGBA, D2H readback)  ->  ctypes.memmove to shared
    memory  ->  spawn process  ->  av.VideoFrame(rgba) planes[0].update(slot)
    ->  encoder "h264_nvenc".

For the POC we remove RTP/Janus (Stage 2, out of scope) and benchmark the
capture->copy->convert->encode segment in a single process, but keep the same
*meaningful* behaviour: host RGBA input, RGBA->YUV conversion by the encoder
pipeline (a hidden/composite CPU operation), hardware NVENC via ``h264_nvenc``.

A software fallback is a benchmark failure. ``hardware_nvenc`` is only reported
when the actual codec selected is an NVENC variant (``h264_nvenc``/``nvenc*``),
and is independently cross-checked against NVENC engine utilization.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Optional

import numpy as np

from .backend import EncoderBackend
from .contracts import (
    BackendCaps, CapturedFrame, EncodedAccessUnit, MemoryDomain,
    PixelFormat, TransferClass, now_ns,
)

_NVENC_NAMES = ("h264_nvenc", "nvenc", "nvenc_h264")


class BackendB0(EncoderBackend):
    name = "b0_pyav_nvenc"

    def __init__(self) -> None:
        self.width = self.height = self.fps = 0
        self.settings: dict = {}
        self._av = None
        self._codec_ctx = None
        self._opened_ok = False
        self.hardware_proof = ""
        self._frame_counter = 0
        self._transfer_log: list = []
        self.last_prep_ns = 0
        self.last_submit_ns = 0

    def configure(self, width: int, height: int, fps: int, settings: dict) -> None:
        self.width, self.height, self.fps = width, height, fps
        self.settings = dict(settings)

    def open(self) -> None:
        import av  # type: ignore

        self._av = av
        gop = int(self.settings.get("gop", self.fps * 2))
        bitrate = self.settings.get("bitrate", "10M")
        options = {
            "preset": str(self.settings.get("preset", "p1")),
            "tune": str(self.settings.get("tune", "ll")),      # low latency
            "profile": str(self.settings.get("profile", "main")),
            "rc": "cbr",
            "bf": "0",
            "b_adapt": "0",
            "rc-lookahead": "0",
            "g": str(gop),
        }
        try:
            ctx = av.codec.CodecContext.create("h264_nvenc", "w")
        except Exception as exc:
            raise RuntimeError(f"B0 h264_nvenc unavailable (no silent sw fallback): {exc}") from exc
        ctx.width = self.width
        ctx.height = self.height
        ctx.pix_fmt = "yuv420p"
        ctx.time_base = Fraction(1, 90000)
        ctx.framerate = Fraction(self.fps, 1)
        ctx.bit_rate = int(self._bitrate_to_int(bitrate))
        ctx.options = options
        try:
            ctx.open()
        except Exception as exc:
            raise RuntimeError(f"B0 h264_nvenc open failed (NOT treated as success): {exc}") from exc
        # confirm the opened codec really is NVENC, not a software fallback
        codec_name = ctx.codec.name if ctx.codec else ""
        if codec_name not in _NVENC_NAMES:
            raise RuntimeError(
                f"B0 requested h264_nvenc but opened codec {codec_name!r}: REFUSING software fallback"
            )
        self._codec_ctx = ctx
        self._opened_ok = True
        self.hardware_proof = (
            f"FFmpeg codec '{codec_name}' opened (NVENC variant); "
            "engine utilization cross-checked by harness"
        )
        self._log(TransferClass.KNOWN_CPU, "RGBA->YUV420 conversion inside encoder (API-hidden swscale)")

    @staticmethod
    def _bitrate_to_int(b: Any) -> int:
        if isinstance(b, (int, float)):
            return int(b)
        s = str(b).strip().lower()
        mult = 1
        if s.endswith("k"):
            mult, s = 1000, s[:-1]
        elif s.endswith("m"):
            mult, s = 1000000, s[:-1]
        return int(float(s) * mult)

    def _log(self, cls: TransferClass, detail: str) -> None:
        self._transfer_log.append({"class": cls.value, "detail": detail})

    def prepare(self, frames: list[CapturedFrame]) -> list[Any]:
        """Build yuv420p av.VideoFrame objects (RGBA->YUV420 reformat is CPU).

        This runs the heavy color conversion and does *not* touch the shared
        encoder state, so it is safe to run on a prep thread. The ``pts`` value
        is computed from the stable frame_id, not a mutable counter, keeping it
        deterministic regardless of thread scheduling.
        """

        out = []
        for frame in frames:
            rgba = np.asarray(frame.buffer)
            vframe = self._av.VideoFrame.from_ndarray(rgba, format="rgba")
            vframe = vframe.reformat(width=self.width, height=self.height, format="yuv420p")
            vframe.pts = frame.frame_id * int(90000 / self.fps)
            vframe.time_base = Fraction(1, 90000)
            out.append(vframe)
        return out

    def encode_raw(self, vframe: Any, frame: CapturedFrame) -> list[EncodedAccessUnit]:
        if not self._opened_ok:
            raise RuntimeError("backend not opened")
        t_submit0 = now_ns()
        packets = self._codec_ctx.encode(vframe)
        self.last_submit_ns = now_ns() - t_submit0
        return self._wrap(packets, frame)

    def encode(self, frame: CapturedFrame) -> list[EncodedAccessUnit]:
        vframe = self.prepare([frame])[0]
        return self.encode_raw(vframe, frame)

    def flush(self) -> list[EncodedAccessUnit]:
        if self._codec_ctx is None:
            return []
        packets = self._codec_ctx.encode(None)
        return [self._packet_to_au(p, -1, 0) for p in packets]

    def _wrap(self, packets: list, frame: CapturedFrame) -> list[EncodedAccessUnit]:
        return [self._packet_to_au(p, frame.frame_id, frame.capture_ts_ns) for p in packets]

    def _packet_to_au(self, p: Any, frame_id: int, capture_ts_ns: int) -> EncodedAccessUnit:
        data = bytes(p)
        return EncodedAccessUnit(
            backend=self.name,
            frame_id=frame_id,
            capture_ts_ns=capture_ts_ns,
            encode_submit_ts_ns=now_ns(),
            encode_complete_ts_ns=now_ns(),
            pts=int(p.pts) if p.pts is not None else None,
            dts=int(p.dts) if p.dts is not None else None,
            codec="h264",
            profile=self.settings.get("profile", "main"),
            is_keyframe=bool(p.is_keyframe),
            size_bytes=len(data),
            bitstream=data,
        )

    def caps(self) -> BackendCaps:
        version = "unknown"
        try:
            import av  # type: ignore
            version = str(av.__version__)
        except Exception:
            pass
        return BackendCaps(
            name=self.name,
            version=f"pyav {version} / ffmpeg h264_nvenc",
            hardware_nvenc=bool(self._opened_ok),
            hardware_proof=self.hardware_proof or "encoder not constructed",
            input_formats=(PixelFormat.RGBA,),
            gpu_direct_input=False,
            notes="host RGBA input; RGBA->YUV by encoder (API-hidden composite CPU op)",
        )

    def close(self) -> None:
        self._codec_ctx = None
        self._opened_ok = False
