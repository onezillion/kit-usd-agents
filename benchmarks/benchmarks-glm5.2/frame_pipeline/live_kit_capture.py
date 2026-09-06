"""Live Kit warehouse capture (Test C) — runs INSIDE Kit via Kit Lab MCP.

Self-contained script that performs the Stage-1 live capture path:
  Kit perspective camera (deterministic placement under /World/FpbBench/*)
    -> omni.kit.widget.viewport.capture.ByteCapture (production B0 capture API)
    -> host numpy RGBA8 staging buffer (explicit host memcpy, mirrors production)
    -> PyAV h264_nvenc encode (inline via the bench venv's site-packages)

This reproduces the meaningful production capture/encode path (ByteCapture -> host
staging -> PyAV/NVENC) WITHOUT Janus/RTP, on the live Warehouse scene.

NOTE on direct GPU handoff to B1 (PyNvVideoCodec GPU input):
  ByteCapture delivers a CPU PyCapsule buffer; there is no Kit-public API surface in
  this build to obtain the render product as a CUDA device pointer that PyNvVideoCodec
  could consume via __cuda_array_interface__.  We therefore record B1-live-GPU as
  CAPABILITY-GATED rather than fabricating a comparison.  The B0 baseline live capture
  IS measured here.

Because this script runs inside Kit's Python, it imports av/PyNvVideoCodec from the
bench venv via a sys.path injection (verified to work in Kit 110.1.3 + Python 3.12.13).
"""
from __future__ import annotations

import asyncio
import ctypes
import fractions
import time
from typing import Optional

# Inject the bench venv site-packages so av / PyNvVideoCodec import inside Kit.
import sys
_BENCH_SITE = "/home/ubuntu/kit-ai/venvs/frame-pipeline-bench/lib/python3.12/site-packages"
if _BENCH_SITE not in sys.path:
    sys.path.insert(0, _BENCH_SITE)

import numpy as np
import av


class LiveCaptureBench:
    def __init__(self, camera_path: str, width: int, height: int, fps: int,
                 bitrate: int, warmup_s: float, measured_s: float,
                 motion: bool = False, run_dir: str = "/tmp/fpb_live",
                 stream_id: str = "C_live"):
        self.camera_path = camera_path
        self.w, self.h, self.fps = width, height, fps
        self.bitrate = bitrate
        self.warmup_s, self.measured_s = warmup_s, measured_s
        self.motion = motion
        self.run_dir = run_dir
        self.stream_id = stream_id
        self.frame_size = width * height * 4  # RGBA8
        self._capsule_name: Optional[str] = None
        self._PyCapsule_GetPointer = ctypes.pythonapi.PyCapsule_GetPointer
        self._PyCapsule_GetPointer.restype = ctypes.c_void_p
        self._PyCapsule_GetPointer.argtypes = (ctypes.py_object, ctypes.c_char_p)
        self._PyCapsule_GetName = ctypes.pythonapi.PyCapsule_GetName
        self._PyCapsule_GetName.restype = ctypes.c_char_p
        self._PyCapsule_GetName.argtypes = (ctypes.py_object,)
        # telemetry
        self.captured = 0
        self.encoded = 0
        self.capture_durations = []
        self.encode_durations = []
        self.latencies = []
        self.au_sizes = []
        self.first_au: Optional[bytes] = None
        import os
        os.makedirs(run_dir, exist_ok=True)
        self._events_path = f"{run_dir}/events_{stream_id}.jsonl"
        self._fh = open(self._events_path, "a", buffering=1)

    def _event(self, kind, payload):
        import json
        rec = {"t": time.time(), "stream": self.stream_id, "kind": kind}
        rec.update(payload)
        self._fh.write(json.dumps(rec, default=float) + "\n")

    async def run(self) -> dict:
        import omni.kit.widget.viewport.capture as cap_mod
        import omni.kit.viewport.utility as vu
        # Acquire / create a viewport bound to the bench camera.
        # In Kit 110.1.3 get_active_viewport_and_window() returns (viewport_window, viewport_window)
        # and the ViewportAPI is obtained via .viewport_api on the window; but the active viewport
        # object also exposes the API directly.  Try both.
        vpw, vp = vu.get_active_viewport_and_window()
        # In this Kit build get_active_viewport_and_window() returns the ViewportAPI object
        # as the first element; it exposes camera_path / schedule_capture / render_mode directly.
        vapi = vpw if hasattr(vpw, "schedule_capture") else vp
        vapi.camera_path = self.camera_path
        try:
            vapi.render_resolution = (self.w, self.h)
        except Exception:
            pass  # not all Kit builds expose render_resolution on the API
        # wait one frame for camera to take
        await asyncio.sleep(0.2)

        # set up PyAV h264_nvenc (B0 baseline)
        codec = av.CodecContext.create("h264_nvenc", "w")
        codec.width = self.w; codec.height = self.h
        codec.pix_fmt = "yuv420p"; codec.bit_rate = self.bitrate
        codec.framerate = self.fps; codec.time_base = fractions.Fraction(1, self.fps)
        codec.options = {
            "preset": "p1", "tune": "ull", "profile": "main", "rc": "cbr",
            "bitrate": str(self.bitrate), "maxrate": str(self.bitrate),
            "g": str(2 * self.fps), "bf": "0", "rc-lookahead": "0",
            "spatial-aq": "0", "temporal-aq": "0", "aq": "0",
            "delay": "0", "forced-idr": "1",
        }
        codec.open()
        hw_ok = (codec.name == "h264_nvenc")
        self._event("encoder_init", {"codec": codec.name, "hw": hw_ok, "camera": self.camera_path,
                                       "render_res": (self.w, self.h)})

        # staged host buffer for the captured RGBA8 frame (explicit host staging copy)
        host_buf = np.zeros((self.h, self.w, 4), dtype=np.uint8)

        # First, discover the capsule name via a probe capture (matches production pattern)
        if self._capsule_name is None:
            for _ in range(20):
                ev = asyncio.Event()
                def _probe(buffer, size, w, h, format):
                    if self._capsule_name is None:
                        try:
                            self._capsule_name = self._PyCapsule_GetName(buffer)
                        except Exception:
                            pass
                    ev.set()
                vapi.schedule_capture(cap_mod.ByteCapture(_probe, aov_name="LdrColor"))
                try:
                    await asyncio.wait_for(ev.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                if self._capsule_name:
                    break
        self._event("capsule_name", {"name": self._capsule_name})

        # main capture+encode loop
        stop_at = time.perf_counter() + self.warmup_s + self.measured_s
        measured_start = stop_at - self.measured_s
        next_t = time.perf_counter()
        period = 1.0 / self.fps
        loop = asyncio.get_event_loop()

        async def capture_one(idx) -> Optional[bytes]:
            done = asyncio.Event()
            captured = {"buf": None, "ts": None, "dur": None}
            t0 = time.perf_counter()
            def _on_cap(buffer, size, w, h, format):
                captured["dur"] = time.perf_counter() - t0
                try:
                    ptr = self._PyCapsule_GetPointer(buffer, self._capsule_name)
                    if ptr:
                        ctypes.memmove(host_buf.ctypes.data, ptr, self.frame_size)
                        captured["buf"] = host_buf.copy()
                        captured["ts"] = time.perf_counter()
                except Exception as e:
                    self._event("cap_err", {"i": idx, "e": str(e)})
                done.set()
            vapi.schedule_capture(cap_mod.ByteCapture(_on_cap, aov_name="LdrColor"))
            try:
                await asyncio.wait_for(done.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                return None
            if captured["buf"] is None:
                return None
            # encode
            frame = av.VideoFrame.from_ndarray(captured["buf"], format="rgba")
            submit_ts = time.perf_counter()
            pkts = codec.encode(frame)
            comp_ts = time.perf_counter()
            au = b"".join(bytes(p) for p in pkts)
            self.capture_durations.append(captured["dur"])
            self.encode_durations.append(comp_ts - submit_ts)
            self.latencies.append(comp_ts - captured["ts"])
            if au:
                self.au_sizes.append(len(au))
            if self.first_au is None and au:
                self.first_au = au
            self._event("frame", {"i": idx, "cap_ms": captured["dur"] * 1000,
                                    "enc_ms": (comp_ts - submit_ts) * 1000,
                                    "lat_ms": (comp_ts - captured["ts"]) * 1000,
                                    "au_bytes": len(au)})
            return au

        idx = 0
        while time.perf_counter() < stop_at:
            await capture_one(idx)
            if time.perf_counter() >= measured_start:
                self.captured += 1
                self.encoded += 1
            idx += 1
            next_t += period
            sleep_for = next_t - time.perf_counter()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            else:
                next_t = time.perf_counter()
        # flush
        flush_pkts = codec.encode(None)
        try:
            codec.flush_buffers()
        except Exception:
            pass
        del codec
        self._fh.flush(); self._fh.close()
        import statistics
        def pct(ls, p):
            if not ls: return 0.0
            s = sorted(ls); return s[max(0, min(len(s)-1, int(round(p/100*(len(s)-1)))))]
        return {
            "stream_id": self.stream_id,
            "camera_path": self.camera_path,
            "resolution": [self.w, self.h],
            "fps_target": self.fps,
            "motion": self.motion,
            "warmup_s": self.warmup_s, "measured_s": self.measured_s,
            "hw_nvenc": hw_ok,
            "captured": self.captured, "encoded": self.encoded,
            "fps_achieved": self.encoded / self.measured_s if self.measured_s else 0.0,
            "capture_ms": {"p50": pct(self.capture_durations, 50), "p95": pct(self.capture_durations, 95),
                            "max": max(self.capture_durations) if self.capture_durations else 0},
            "encode_ms": {"p50": pct(self.encode_durations, 50), "p95": pct(self.encode_durations, 95),
                           "max": max(self.encode_durations) if self.encode_durations else 0},
            "latency_ms": {"p50": pct(self.latencies, 50), "p95": pct(self.latencies, 95),
                            "p99": pct(self.latencies, 99),
                            "max": max(self.latencies) if self.latencies else 0},
            "au_bytes_p50": pct(self.au_sizes, 50),
            "first_au_bytes": len(self.first_au) if self.first_au else 0,
            "flush_au_bytes": sum(len(bytes(p)) for p in flush_pkts),
        }
