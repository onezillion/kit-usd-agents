"""Live Kit capture benchmark driver.

Reads the warehouse frames captured via Kit's ByteCapture path (saved as .npy
by the in-Kit capture routine), downsamples them to 1080p, and runs them through
the Stage-1 encode pipeline for B0 and B1 backends at 1 and 2 encode sessions.

This models the production capture->convert->encode segment using *real* Kit
camera/render output (not the synthetic source), with a deterministic camera and
a repeatable high-motion dolly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Optional

import numpy as np

from .contracts import CapturedFrame, MemoryDomain, PixelFormat, now_ns
from .h264_verify import verify_bitstream
from .recorder import RunRecorder
from .harness import Harness, hardware_nvenc_crosscheck_fail
from .telemetry import TelemetrySampler, available as nvml_available

LIVE_FRAMES_DIR = os.environ.get(
    "FRAME_PIPELINE_LIVE_FRAMES",
    "/home/ubuntu/kit-ai/benchmarks/frame-pipeline/live_frames",
)


def _downsample_to_1080(arr4m: np.ndarray, w: int, h: int) -> np.ndarray:
    """Bilinear 2x downsample (3840x2160 -> 1920x1080) via area average."""

    # arr shape (h, w, 4). Use a simple 2x2 mean for determinism.
    hh, ww = arr4m.shape[0], arr4m.shape[1]
    ds = arr4m.reshape(hh // 2, 2, ww // 2, 2, arr4m.shape[2]).mean(axis=(1, 3))
    return ds.astype(np.uint8)


def load_live_frames(tag: str, count: int, dir_: str = LIVE_FRAMES_DIR) -> list[np.ndarray]:
    out = []
    for i in range(count):
        p = os.path.join(dir_, f"{tag}_{i:03d}.npy")
        if not os.path.exists(p):
            break
        a = np.load(p)
        out.append(_downsample_to_1080(a, a.shape[1], a.shape[0]))
    return out


def run_live_case(
    tag: str,
    mode: str,
    backend: str,
    n_encoders: int,
    loops: int,
    out_dir: str,
    fps: int = 30,
) -> dict:
    frames = load_live_frames(tag, 30)
    if not frames:
        raise RuntimeError(f"no live frames at {LIVE_FRAMES_DIR}/{tag}_*.npy")
    W, H = frames[0].shape[1], frames[0].shape[0]
    # Build a cycling sequence of CapturedFrames at the requested fps.
    from .config import EncoderIntent
    intent = EncoderIntent()
    if backend == "b0":
        from .backend_b0_pyav import BackendB0
        makers = [BackendB0 for _ in range(n_encoders)]
    else:
        from .backend_b1_pynvvc import BackendB1
        makers = [lambda: BackendB1(0, "host") for _ in range(n_encoders)]

    seq_len = len(frames) * loops
    name = f"live_{tag}_{backend}_{n_encoders}enc"
    rec = RunRecorder(out_dir, name, {"tag": tag, "backend": backend, "n_encoders": n_encoders, "fps": fps})
    telem = TelemetrySampler(interval_s=0.5, gpu_indices=[0]) if nvml_available() else None

    backends = []
    for i, mk in enumerate(makers):
        be = mk()
        be.configure(W, H, fps, intent.for_b1(fps) if backend != "b0" else intent.for_b0(fps))
        backends.append(be)

    def make_frame(stream: str, fid: int) -> CapturedFrame:
        a = frames[fid % len(frames)]
        return CapturedFrame(frame_id=fid, capture_ts_ns=now_ns(), width=W, height=H,
                             pixel_format=PixelFormat.RGBA, memory_domain=MemoryDomain.CPU,
                             buffer=a, pitch_bytes=W * 4)

    frame_ids = list(range(seq_len))
    if telem:
        telem.start()
    harness = Harness(rec, telemetry=telem)
    try:
        result = harness.run_case(
            name, backends, make_frame, frame_ids, fps,
            prep_workers=4, warmup_s=0.0,
            prep_queue_capacity=32, capture_queue_capacity=32,
        )
    finally:
        if telem:
            telem.stop()
    for st in result.streams:
        v = verify_bitstream(st.bitstream, W, H)
        st.verify.update(v.as_dict())
        st.bitstream = b""
    hw = hardware_nvenc_crosscheck_fail(result)
    rec.extra["verify"] = {st.stream_name: st.verify for st in result.streams}
    rec.extra["hw_crosscheck_failures"] = hw
    rec.extra["telemetry"] = result.telemetry
    summary = rec.build_summary()
    summary["result"] = {
        "produced_fps": result.produced_fps,
        "streams": [{"stream": st.stream_name, "captured": st.captured,
                     "encoded_frames": st.encoded_frames, "verify_ok": st.verify.get("ok"),
                     "profile": st.verify.get("profile"), "hw": st.hw_nvenc_active,
                     "errors": st.errors} for st in result.streams],
    }
    rec.write_summary()
    rec.close()
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Live Kit frame pipeline bench")
    ap.add_argument("--tag", default="static4k")
    ap.add_argument("--backend", default="b1_host", choices=["b0", "b1_host"])
    ap.add_argument("--encoders", type=int, default=1)
    ap.add_argument("--loops", type=int, default=3)
    ap.add_argument("--out", default=os.path.join(os.path.expanduser("~"), "kit-ai", "benchmarks", "frame-pipeline"))
    args = ap.parse_args(argv)
    out_dir = os.path.join(args.out, "live")
    os.makedirs(out_dir, exist_ok=True)
    mode = "static" if "static" in args.tag else "motion"
    print(f"[live] tag={args.tag} backend={args.backend} encoders={args.encoders} loops={args.loops}")
    s = run_live_case(args.tag, mode, args.backend, args.encoders, args.loops, out_dir)
    print("[live] done:", json.dumps(s["result"], indent=1)[:800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
