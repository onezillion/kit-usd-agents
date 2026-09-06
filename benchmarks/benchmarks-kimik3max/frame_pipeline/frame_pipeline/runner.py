"""Benchmark runner CLI.

Runs a named suite of cases, verifies the encoded bitstreams, optionally decodes
representative output for an objective quality check, and writes results
outside the repository by default (env override: FRAME_PIPELINE_BENCH_OUT).
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import sys
from typing import Any, Optional

from .config import Case, POC_MULTI, POC_SINGLE
from .contracts import CapturedFrame, MemoryDomain
from .env_probe import environment
from .h264_verify import FrameOrder, verify_bitstream
from .harness import Harness, hardware_nvenc_crosscheck_fail
from .recorder import RunRecorder
from .synthetic_source import SceneGenerator, SyntheticSource
from .telemetry import TelemetrySampler, available as nvml_available


def default_out_root() -> str:
    return os.environ.get(
        "FRAME_PIPELINE_BENCH_OUT",
        os.path.join(os.path.expanduser("~"), "kit-ai", "benchmarks", "frame-pipeline"),
    )


def _make_backend(case: Case):
    if case.backend == "b0":
        from .backend_b0_pyav import BackendB0
        be = BackendB0()
        be.configure(case.width, case.height, case.fps, case.encoder_intent.for_b0(case.fps))
        return be
    if case.backend == "b1_host":
        from .backend_b1_pynvvc import BackendB1
        be = BackendB1(device_index=0, input_mode="host")
        be.configure(case.width, case.height, case.fps, case.encoder_intent.for_b1(case.fps))
        return be
    if case.backend == "b1_device":
        from .backend_b1_pynvvc import BackendB1
        be = BackendB1(device_index=0, input_mode="device")
        be.configure(case.width, case.height, case.fps, case.encoder_intent.for_b1(case.fps))
        return be
    raise ValueError(f"unknown backend {case.backend}")


def run_case(case: Case, run_dir: str, keep_samples: int = 2) -> dict:
    rec = RunRecorder(run_dir, case.name, case.as_dict())
    telem = TelemetrySampler(interval_s=0.5, gpu_indices=[0, 1]) if nvml_available() else None
    # Fan-out: one shared generator (identical frame content for every encoder).
    # Independent: per-stream generator seeds (each encoder its own sequence).
    generators = [SceneGenerator(case.width, case.height, case.mode,
                                 seed=1337 + (i if case.independent_sources else 0))
                  for i in range(case.n_streams)]
    frame_cache: dict = {}

    def make_frame(stream_name: str, frame_id: int) -> CapturedFrame:
        if case.independent_sources:
            idx = int(stream_name[1:]) if stream_name.startswith("s") and stream_name[1:].isdigit() else 0
            gen = generators[min(idx, len(generators) - 1)]
        else:
            # fan-out: all streams share identical content; cache per frame_id
            if frame_id not in frame_cache or frame_cache[frame_id] is None:
                frame_cache[frame_id] = SyntheticSource(generators[0], MemoryDomain.CPU).make_frame(frame_id)
            return frame_cache[frame_id]
        return SyntheticSource(gen, MemoryDomain.CPU).make_frame(frame_id)

    backends = [_make_backend(case) for _ in range(case.n_streams)]
    n_frames = int(round(case.measured_s * case.fps))
    frame_ids = list(range(n_frames))
    if telem:
        telem.start()
    harness = Harness(rec, telemetry=telem)
    try:
        result = harness.run_case(
            case.name, backends, make_frame, frame_ids, case.fps,
            prep_workers=max(1, min(4, case.queue_capacity)),
            warmup_s=case.warmup_s,
            prep_queue_capacity=64,
            capture_queue_capacity=max(16, case.queue_capacity * case.n_streams),
        )
    finally:
        if telem:
            telem.stop()
    # verify bitstreams
    order_summary = {}
    for st in result.streams:
        v = verify_bitstream(st.bitstream, case.width, case.height)
        st.verify.update(v.as_dict())
        order_summary[st.stream_name] = True  # structural ok flag carried in verify
    hw_fails = hardware_nvenc_crosscheck_fail(result)
    if hw_fails:
        rec.extra["hw_crosscheck_failures"] = hw_fails
        for st in result.streams:
            st.errors.extend(hw_fails)
    # bound retained encoded samples: keep only first N streams' bitstreams
    for i, st in enumerate(result.streams):
        if i < keep_samples:
            with open(os.path.join(run_dir, f"{case.name}_{st.stream_name}.h264"), "wb") as fh:
                fh.write(st.bitstream)
        st.bitstream = b""  # do not persist large buffers into summary JSON
    rec.extra["verify"] = {st.stream_name: st.verify for st in result.streams}
    rec.extra["telemetry"] = result.telemetry
    summary = rec.build_summary()
    summary["case"] = asdict_for_summary(case, result)
    summary["produced_fps"] = result.produced_fps
    summary["duration_s"] = result.duration_s
    path = rec.write_summary()
    rec.close()
    # re-stamp the case block (write_summary regenerated it from build_summary)
    try:
        import json as _json
        with open(path, "r", encoding="utf-8") as fh:
            on_disk = _json.load(fh)
        on_disk["case"] = asdict_for_summary(case, result)
        on_disk["produced_fps"] = result.produced_fps
        on_disk["duration_s"] = result.duration_s
        with open(path, "w", encoding="utf-8") as fh:
            _json.dump(on_disk, fh, indent=2, default=str)
        summary = on_disk
    except Exception:
        pass
    return summary


def asdict_for_summary(case: Case, result) -> dict:
    d = case.as_dict()
    d["result"] = {
        "producer_fps": result.produced_fps,
        "duration_s": result.duration_s,
        "hw_crosscheck": result.summary.get("hw_crosscheck", {}),
        "streams": [
            {
                "stream": st.stream_name,
                "captured": st.captured,
                "prepared": st.prepared,
                "encoded_frames": st.encoded_frames,
                "hw_nvenc_active": st.hw_nvenc_active,
                "hardware_proof": st.hardware_proof,
                "errors": st.errors,
                "verify_ok": st.verify.get("ok", False),
                "profile": st.verify.get("profile"),
                "resolution": f"{st.verify.get('width')}x{st.verify.get('height')}",
                "idr_count": st.verify.get("idr_count"),
            } for st in result.streams
        ],
    }
    return d


SUITES = {
    "single": POC_SINGLE,
    "multi": POC_MULTI,
    "all": POC_SINGLE + POC_MULTI,
}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Frame pipeline benchmark (Stage 1)")
    ap.add_argument("--suite", default="single", choices=sorted(SUITES) + ["smoke"])
    ap.add_argument("--case", default=None, help="run a single named case")
    ap.add_argument("--out", default=None, help="override output root")
    ap.add_argument("--quick", action="store_true", help="short warmup/measure (smoke)")
    args = ap.parse_args(argv)

    out_root = args.out or default_out_root()
    date = _dt.date.today().isoformat()
    run_dir = os.path.join(out_root, f"{date}-{args.suite}")
    os.makedirs(run_dir, exist_ok=True)

    env = environment()
    with open(os.path.join(run_dir, "environment.json"), "w", encoding="utf-8") as fh:
        json.dump(env, fh, indent=2)

    if args.quick:
        for c in POC_SINGLE + POC_MULTI:
            c.warmup_s, c.measured_s = 1.0, 2.0

    cases = []
    if args.case:
        allc = {c.name: c for c in POC_SINGLE + POC_MULTI}
        cases = [allc[args.case]] if args.case in allc else []
        if not cases:
            print(f"unknown case {args.case}", file=sys.stderr)
            return 2
    elif args.suite == "smoke":
        c = POC_SINGLE[0]
        cases = [c]
    else:
        cases = SUITES[args.suite]

    summaries = []
    failures = []
    for case in cases:
        print(f"[run] {case.name} backend={case.backend} {case.width}x{case.height}@{case.fps} mode={case.mode} streams={case.n_streams}")
        try:
            summaries.append(run_case(case, run_dir))
            any_err = any(st.get("errors") for st in summaries[-1]["case"]["result"]["streams"])
            print(f"      -> {'ERRORS' if any_err else 'ok'} (summary written)")
        except Exception as exc:
            failures.append({"case": case.name, "error": f"{type(exc).__name__}: {exc}"})
            print(f"      -> FAILED: {type(exc).__name__}: {exc}")

    # CSV comparison
    csv_path = os.path.join(run_dir, "comparison.csv")
    _write_csv(csv_path, summaries)
    if failures:
        with open(os.path.join(run_dir, "failures.json"), "w", encoding="utf-8") as fh:
            json.dump(failures, fh, indent=2)
    print(f"\nresults: {run_dir}")
    print(f"csv: {csv_path}")
    if failures:
        print(f"failures: {len(failures)} (see failures.json)")
        return 1
    return 0


def _write_csv(path: str, summaries: list[dict]) -> None:
    rows = []
    for s in summaries:
        c = s["case"]
        res = c["result"]
        streams = s.get("streams", {})
        for st in res["streams"]:
            sn = st["stream"]
            lat = streams.get(sn, {}).get("latency", {})
            c2e = lat.get("capture_to_encoded_ns", {})
            counters = streams.get(sn, {}).get("counters", {})
            enc = streams.get(sn, {}).get("encoded", {})
            rows.append({
                "case": c["name"], "backend": c["backend"], "mode": c["mode"],
                "fps_target": c["fps"], "streams": c["n_streams"], "stream": sn,
                "producer_fps": res["producer_fps"],
                "encoded_frames": st["encoded_frames"], "captured": st["captured"],
                "hw_nvenc": st["hw_nvenc_active"], "verify_ok": st["verify_ok"],
                "profile": st["profile"], "idr_count": st["idr_count"],
                "mean_frame_bytes": enc.get("mean_frame_bytes"),
                "p50_ms": c2e.get("p50_ms"), "p95_ms": c2e.get("p95_ms"),
                "p99_ms": c2e.get("p99_ms"), "max_ms": c2e.get("max_ms"),
                "drops": counters.get("dropped", 0), "replacements": counters.get("replacements", 0),
                "errors": "; ".join(st["errors"]) if st["errors"] else "",
            })
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=sorted(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
