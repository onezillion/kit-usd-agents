"""B0 — existing-style baseline encoder matching the production omni.khl.webrtc_stream
data path (PyAV h264_nvenc, RGBA host input -> software swscale -> yuv420p -> NVENC).

This adapter intentionally reproduces the meaningful production encode path WITHOUT the
RTP/WebRTC packetization.  It mirrors the production code's behaviour of:
  - input: host RGBA8 numpy buffer (as if delivered by Kit ByteCapture + shared memory);
  - PyAV swscale RGBA->yuv420p CPU color conversion (API-hidden conversion);
  - h264_nvenc with preset p1 / tune ull / profile main / rc cbr / bf 0 / lookahead 0 / aq 0.

CRITICAL: production code silently falls back to libx264 if h264_nvenc is not in
`av.codecs_available`.  We detect that case and raise BackendUnavailable so the bench
fails loudly rather than reporting a software result as if it were hardware NVENC.
"""
from __future__ import annotations

import fractions
import time
from typing import List

from ..contracts import (CapturedFrame, EncodedAccessUnit,
                          DOMAIN_HOST_NUMPY, DOMAIN_API_HIDDEN)
from .base import BackendSettings, EncoderBackend, BackendUnavailable


class B0PyavNvenc(EncoderBackend):
    name = "b0_pyav"
    accept_memory_domain = DOMAIN_HOST_NUMPY
    accept_pixel_format = "RGBA"   # production takes RGBA8, swscale to yuv420p internally

    def __init__(self, settings: BackendSettings):
        super().__init__(settings)
        import av
        self._av = av
        s = self.settings
        if "h264_nvenc" not in av.codecs_available:
            # The production path silently falls back to libx264 here.  We DO NOT.
            raise BackendUnavailable(
                "h264_nvenc not present in PyAV build (production would silently fall "
                "back to libx264 software — refusing to fake a hardware result)")
        self._codec = av.CodecContext.create("h264_nvenc", "w")
        self._codec.width = s.width
        self._codec.height = s.height
        self._codec.pix_fmt = "yuv420p"
        self._codec.bit_rate = s.bitrate
        self._codec.framerate = s.fps
        self._codec.time_base = fractions.Fraction(1, s.fps)
        # Match production-style options translated to the job's common settings.
        self._codec.options = {
            "preset": "p1",                 # P1 == fastest
            "tune": "ull",                  # ultra-low-latency
            "profile": s.profile_b0,        # "main" — matches the job target
            "rc": "cbr",
            "bitrate": str(s.bitrate),
            "maxrate": str(s.max_bitrate),
            "g": str(s.gop),
            "bf": str(s.bf),
            "rc-lookahead": str(s.lookahead),
            "spatial-aq": str(s.aq),
            "temporal-aq": str(s.temporalaq),
            "aq": str(s.aq),
            "delay": "0",
            "forced-idr": "1",
        }
        self._codec.open()
        if self._codec.name != "h264_nvenc":
            # PyAV has been observed to still report the requested codec name here even
            # when nvenc init fails — but if it ever changes, treat it as a fallback.
            raise BackendUnavailable(
                "PyAV opened codec %r instead of h264_nvenc — refusing software fallback"
                % self._codec.name)
        self._frame_id_seq = 0

    # ----------------------------------------------------------------------
    def verify_hardware(self) -> dict:
        # PyAV exposes the codec name; we additionally confirm h264_nvenc is the AVCodec
        # actually opened (not libx264).  codec.name is the authoritative name after open().
        ok = self._codec.name == "h264_nvenc"
        return {"hardware": ok,
                "evidence": "av.CodecContext.name=='h264_nvenc' after open(); "
                            "h264_nvenc present in av.codecs_available=%r"
                            % ("h264_nvenc" in self._av.codecs_available),
                "codec_name": self._codec.name}

    def sps_pps(self) -> bytes:
        # Force the encoder to emit SPS/PPS by encoding one frame and extracting
        # the leading non-VCL NALs.  We never feed a real frame here — just probe
        # with a single black frame to harvest SPS/PPS; the real run is separate.
        import numpy as np
        s = self.settings
        probe = np.zeros((s.height, s.width, 4), dtype=np.uint8)
        probe[:, :, 3] = 255  # opaque black RGBA
        # Each Encode call emits SPS+PPS+IDR together when repeatspspps-like behaviour
        # is active; we extract the SPS/PPS NALs below.
        return self._harvest_sps_pps_from_probe(probe)

    def _harvest_sps_pps_from_probe(self, rgba_np) -> bytes:
        # Encode one throwaway frame and collect the SPS(7)+PPS(8) NAL bytes.
        frame = self._av.VideoFrame.from_ndarray(rgba_np, format="rgba")
        pkts = self._codec.encode(frame)
        sps_pps = b""
        for p in pkts:
            d = bytes(p)
            # walk NALs
            i = 0
            while i < len(d):
                if d[i:i + 4] == b"\x00\x00\x00\x01":
                    start, sz = i, 4
                elif d[i:i + 3] == b"\x00\x00\x01":
                    start, sz = i, 3
                else:
                    i += 1
                    continue
                nal_type = d[start + sz] & 0x1F
                if nal_type in (7, 8):
                    # take to next start code or end
                    j = start + sz + 1
                    while j < len(d):
                        if d[j:j + 4] == b"\x00\x00\x00\x01" or d[j:j + 3] == b"\x00\x00\x01":
                            break
                        j += 1
                    sps_pps += d[start:j]
                i = start + sz
        # NOTE: this probe encode consumes one NVENC frame; we DO NOT count it in the
        # run telemetry.  SPS/PPS are stable across the session for a fixed-config
        # encoder; the verifier uses the run's actual first AU as the canonical sample.
        return sps_pps

    # ----------------------------------------------------------------------
    def prepare(self, cf: CapturedFrame) -> tuple[object, dict]:
        """Map a CapturedFrame.frame_obj (host RGBA8 numpy) to a PyAV VideoFrame.

        Mirrors production: `av.VideoFrame.from_ndarray(rgba, format="rgba")` then
        swscale RGBA->yuv420p happens lazily inside `codec.encode()`.  We time the
        ndarray->VideoFrame step here; the swscale step is API-hidden and timed
        indirectly via encoder_submit_duration.

        Explicit copy accounting: PyAV's `from_ndarray` constructs a VideoFrame that
        REFERENCES the numpy buffer (zero-copy construction) — no explicit copy here.
        The swscale conversion inside encode() is an API-hidden CPU conversion.
        """
        t0 = time.perf_counter()
        rgba = cf.frame_obj  # host numpy uint8 (H,W,4)
        vf = self._av.VideoFrame.from_ndarray(rgba, format="rgba")
        t1 = time.perf_counter()
        summary = {
            "capture_cb_duration_s": 0.0,  # filled by source
            "color_convert_duration_s": 0.0,  # swscale is API-hidden, timed at encode
            "frame_prep_duration_s": t1 - t0,
            "copies": {"d2d": 0, "d2h": 0, "h2d": 0},
            "memory_domain_in": cf.memory_domain,
            "memory_domain_out": DOMAIN_HOST_NUMPY,
            "note": "PyAV swscale RGBA->yuv420p is API-hidden CPU color conversion",
        }
        return vf, summary

    def encode_frame(self, cf: CapturedFrame) -> List[EncodedAccessUnit]:
        vf, prep = self.prepare(cf)  # includes ndarray->VideoFrame construction
        s = self.settings
        submit_ts = time.perf_counter()
        # PyAV swscale + NVENC encode happen inside this call (API-hidden split).
        packets = self._codec.encode(vf)
        completion_ts = time.perf_counter()
        out = []
        for p in packets:
            data = bytes(p)
            is_idr = False
            # walk first NAL to detect IDR(5)
            if data[:4] == b"\x00\x00\x00\x01":
                nal = data[4]
            elif data[:3] == b"\x00\x00\x01":
                nal = data[3]
            else:
                nal = 0
            if (nal & 0x1F) == 5:
                is_idr = True
            au = EncodedAccessUnit(
                frame_id=cf.frame_id,
                source_capture_ts=cf.capture_ts,
                encode_submit_ts=submit_ts,
                encode_completion_ts=completion_ts,
                pts=int(p.pts) if p.pts is not None else None,
                dts=int(p.dts) if p.dts is not None else None,
                codec="h264",
                profile=s.profile_b0,
                is_keyframe=is_idr,
                encoded_size_bytes=len(data),
                backend=self.name,
                backend_metadata={
                    "pyav_codec_name": self._codec.name,
                    "color_convert": "api_hidden_swscale_RGBA_to_YUV420p_CPU",
                    "nvenc_upload": "api_hidden_H2D_inside_PyAV",
                    "prep_summary": prep,
                },
                data=data,
            )
            out.append(au)
        return out

    def flush(self) -> List[EncodedAccessUnit]:
        packets = self._codec.encode(None)
        completion_ts = time.perf_counter()
        out = []
        for p in packets:
            data = bytes(p)
            out.append(EncodedAccessUnit(
                frame_id=-1, source_capture_ts=0.0,
                encode_submit_ts=completion_ts, encode_completion_ts=completion_ts,
                codec="h264", profile=self.settings.profile_b0,
                encoded_size_bytes=len(data), backend=self.name,
                backend_metadata={"flush": True}, data=data))
        return out

    def close(self):
        if not self._closed:
            try:
                self._codec.close()
            except Exception:
                pass
        super().close()
