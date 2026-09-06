"""Subprocess worker for run_poc orchestrator.

Constructs the requested backend + source, runs the harness for one or more streams
(single / multi-encoder fan-out / independent), verifies the first encoded AU, and writes
a summary JSON next to the run dir.

Runs in its own process so each backend's NVENC/CUDA context lifetime is bounded.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# ensure the package is importable when invoked as python -m frame_pipeline._worker
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from frame_pipeline.backends.base import BackendSettings
from frame_pipeline.backends.b0_pyav import B0PyavNvenc
from frame_pipeline.backends.b1_pynv import B1PynvCpu, B1PynvGpu
from frame_pipeline.harness import run_streams
from frame_pipeline.h264_verify import verify_bitstream
from frame_pipeline.sources.synthetic import SyntheticSource, SourceConfig


def make_backend(name: str, s: BackendSettings):
    if name == "b0_pyav":
        return B0PyavNvenc(s)
    if name == "b1_pynv_cpu":
        return B1PynvCpu(s)
    if name == "b1_pynv_gpu":
        return B1PynvGpu(s)
    raise ValueError("unknown backend: %r" % name)


def source_domain_for(backend_name: str) -> str:
    if backend_name == "b0_pyav":
        return "host_rgba"
    if backend_name == "b1_pynv_cpu":
        return "host_nv12"
    if backend_name == "b1_pynv_gpu":
        return "gpu_nv12"
    raise ValueError(backend_name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True)
    ap.add_argument("--fps", type=int, required=True)
    ap.add_argument("--mode", required=True)
    ap.add_argument("--bitrate", type=int, required=True)
    ap.add_argument("--warmup", type=float, default=10.0)
    ap.add_argument("--measured", type=float, default=30.0)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--stream-id", required=True)
    ap.add_argument("--repetition", type=int, default=0)
    ap.add_argument("--multi", type=int, default=1)
    ap.add_argument("--fan-mode", default="independent",
                    help="fanout (one source feeds many) | independent (each its own)")
    args = ap.parse_args()

    s = BackendSettings(fps=args.fps, bitrate=args.bitrate, max_bitrate=args.bitrate,
                        gop=2 * args.fps, idr_period=2 * args.fps)

    # build sources + backends
    n = max(1, args.multi)
    specs = []
    if args.multi == 1:
        # single stream
        src_cfg = SourceConfig(fps=args.fps, mode=args.mode,
                                output_domain=source_domain_for(args.backend))
        src = SyntheticSource(src_cfg)
        src.start()
        backend = make_backend(args.backend, s)
        hw = backend.verify_hardware()
        specs.append({"stream_id": args.stream_id, "backend": backend,
                      "source": src.next_frame, "fps": args.fps, "queue_maxsize": 2})
        results = run_streams(specs, args.warmup, args.measured, args.run_dir,
                              sample_first_au=True)
        src.stop()
        summary = {"stream_id": args.stream_id, "backend": args.backend,
                   "fps_target": args.fps, "mode": args.mode, "bitrate": args.bitrate,
                   "warmup_s": args.warmup, "measured_s": args.measured,
                   "hardware_proof": hw,
                   "results": [r.summary for r in results]}
        # verify the first AU; if it lacks SPS/PPS (mid-stream sample), prepend the
        # backend's canonical SPS/PPS bytes so the decoder can set up.
        if results and results[0].samples_first_au:
            import numpy as np
            from frame_pipeline.h264_verify import split_nals, nal_type
            sample = results[0].samples_first_au
            nals = split_nals(sample)
            types = [nal_type(n) for n in nals]
            if 7 not in types or 8 not in types:
                sps_pps = results[0].sps_pps
                if sps_pps:
                    sample = sps_pps + sample
            ref = src._nv12_for_frame(0) if args.backend != "b0_pyav" else None
            vr = verify_bitstream(sample, s.width, s.height,
                                  reference_nv12=ref if ref is not None else None,
                                  decode=True)
            summary["verify"] = {
                "ok": vr.ok, "n_nalus": vr.n_nalus, "sps": vr.sps_present,
                "pps": vr.pps_present, "profile_idc": vr.profile_idc,
                "profile_name": vr.profile_name, "level_idc": vr.level_idc,
                "sps_resolution": vr.sps_resolution,
                "idr_count": vr.idr_count, "slice_count": vr.slice_count,
                "sps_before_first_idr": vr.sps_before_first_idr,
                "decoded_frames": vr.decoded_frames,
                "psnr_db": vr.psnr_db, "ssim": vr.ssim_,
                "errors": vr.errors}
        # write summary
        sp = Path(args.run_dir) / f"{args.stream_id}.summary.json"
        with open(sp, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        print("OK", args.stream_id, "->", sp)
    else:
        # multi-encoder
        if args.fan_mode == "fanout":
            # one source feeds N encoders; we distribute the SAME frame index to all.
            # IMPORTANT: PyNvVideoCodec encoders cannot share the same input numpy object
            # concurrently (Encode() may reference the buffer).  Each encoder gets its own
            # copy of the frame contents via a fresh src.next_frame(idx) call — frame index
            # is the same so the frames are byte-identical deterministically.
            src_cfg = SourceConfig(fps=args.fps, mode=args.mode,
                                    output_domain=source_domain_for(args.backend))
            src = SyntheticSource(src_cfg); src.start()
            # Give each spec its OWN source-closure that re-generates the same frame index
            # independently so no numpy object is shared across encoder instances.
            def make_source(src_obj):
                def _gen(idx):
                    return src_obj.next_frame(idx)
                return _gen
            for i in range(n):
                b = make_backend(args.backend, s)
                specs.append({"stream_id": f"{args.stream_id}_s{i}", "backend": b,
                              "source": make_source(src), "fps": args.fps, "queue_maxsize": 2})
            results = run_streams(specs, args.warmup, args.measured, args.run_dir)
            src.stop()
        else:  # independent
            sources = []
            for i in range(n):
                src_cfg = SourceConfig(fps=args.fps, mode=args.mode,
                                        output_domain=source_domain_for(args.backend),
                                        seed=0xA5A5 + i * 7919)
                src = SyntheticSource(src_cfg); src.start(); sources.append(src)
                b = make_backend(args.backend, s)
                specs.append({"stream_id": f"{args.stream_id}_s{i}", "backend": b,
                              "source": src.next_frame, "fps": args.fps, "queue_maxsize": 2})
            results = run_streams(specs, args.warmup, args.measured, args.run_dir)
            for src in sources:
                src.stop()
        # aggregate
        agg = {"label": args.stream_id, "backend": args.backend, "fan_mode": args.fan_mode,
               "n_encoders": n, "fps_target": args.fps, "mode": args.mode,
               "per_stream": [r.summary for r in results],
               "per_stream_queue": [r.queue_stats for r in results],
               "aggregate": {
                   "total_encoded": sum(r.summary.get("encoded", 0) for r in results),
                   "aggregate_fps": sum(r.summary.get("encoded", 0) for r in results) / args.measured,
                   "slowest_fps": min((r.summary.get("encoded", 0) for r in results),
                                      default=0) / args.measured,
                   "total_drops": sum(r.drops for r in results),
                   "total_replacements": sum(r.replacements for r in results),
               }}
        sp = Path(args.run_dir) / f"{args.stream_id}.summary.json"
        with open(sp, "w") as f:
            json.dump(agg, f, indent=2, default=str)
        print("OK", args.stream_id, "n=", n, "->", sp)


if __name__ == "__main__":
    main()
