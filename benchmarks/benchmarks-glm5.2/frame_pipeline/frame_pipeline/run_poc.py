"""POC orchestrator for KHL-FRAME-PIPELINE-BENCH-01.

Runs the discriminating POC tests from the job spec:
  A. Single encoder per backend @ 1080p30 + 1080p60, static + motion.
  B. Multi-encoder (1080p30: 1/2/4; 1080p60: 1/2) — same-frame fan-out AND independent.
  C. Live Kit warehouse (1080p30: 1 and 2 pipelines) — driven from a separate live
     capture script under live_kit_capture.py; this orchestrator only emits the B-series.

Generated results default to $HOME/kit-ai/benchmarks/frame-pipeline/<run-id>/ ; override
with FRAME_PIPELINE_RESULTS_DIR.  Nothing is written inside the git repo except source.

Encoders run in SUBPROCESSES per backend (one process per backend batch) so that
PyNvVideoCodec CUDA context lifetime stays bounded and any free/double-free noise at
shutdown cannot contaminate subsequent runs.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable


def results_root() -> Path:
    base = os.environ.get("FRAME_PIPELINE_RESULTS_DIR",
                          str(Path.home() / "kit-ai" / "benchmarks" / "frame-pipeline"))
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def new_run_dir(tag: str) -> Path:
    ts = time.strftime("%Y%m%dT%H%M%S")
    d = results_root() / f"{ts}-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_subprocess(module_args: list, run_dir: Path, label: str) -> dict:
    """Run a worker subprocess; capture stdout+stderr to run_dir."""
    log = run_dir / f"{label}.log"
    with open(log, "w") as fh:
        proc = subprocess.run([PY, "-m", "frame_pipeline._worker"] + module_args,
                              stdout=fh, stderr=subprocess.STDOUT,
                              cwd=str(HERE.parent))
    summary_path = run_dir / f"{label}.summary.json"
    if summary_path.exists():
        with open(summary_path) as f:
            return {"label": label, "rc": proc.returncode, "summary": json.load(f)}
    return {"label": label, "rc": proc.returncode, "summary": None}


# ----- test definitions ---------------------------------------------------
def test_a_single(run_dir: Path, fps: int, mode: str, backend: str,
                  warmup: float = 10.0, measured: float = 30.0,
                  repetitions: int = 1) -> list:
    """Test A: single encoder at the given fps/mode/backend."""
    out = []
    bitrate = 10_000_000 if fps == 30 else 20_000_000
    for rep in range(repetitions):
        label = f"A_{backend}_1080p{fps}_{mode}_rep{rep}"
        args = ["--backend", backend, "--fps", str(fps), "--mode", mode,
                "--bitrate", str(bitrate), "--warmup", str(warmup),
                "--measured", str(measured), "--run-dir", str(run_dir),
                "--stream-id", label, "--repetition", str(rep)]
        out.append(run_subprocess(args, run_dir, label))
    return out


def test_b_multi(run_dir: Path, fps: int, n_encoders: int, fan_mode: str,
                 warmup: float = 10.0, measured: float = 30.0) -> dict:
    """Test B: multi-encoder.  fan_mode in {fanout, independent}."""
    bitrate = 10_000_000 if fps == 30 else 20_000_000
    label = f"B_{fan_mode}_1080p{fps}_x{n_encoders}"
    # We use B1-CPU for multi-encoder POC (B1-GPU shares one CU context: safer in subprocess)
    args = ["--backend", "b1_pynv_cpu", "--fps", str(fps), "--mode", "motion",
            "--bitrate", str(bitrate), "--warmup", str(warmup),
            "--measured", str(measured), "--run-dir", str(run_dir),
            "--multi", str(n_encoders), "--fan-mode", fan_mode,
            "--stream-id", label]
    return run_subprocess(args, run_dir, label)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warmup", type=float, default=10.0)
    ap.add_argument("--measured", type=float, default=30.0)
    ap.add_argument("--quick", action="store_true",
                    help="shrink durations AND scope for a fast smoke run")
    ap.add_argument("--tests", default="A,B",
                    help="comma-separated subset of {A,B,C}")
    args = ap.parse_args()

    run_dir = new_run_dir("poc")
    (run_dir / "env_probe.json").write_text(
        json.dumps(subprocess.run([PY, "-c",
            "from frame_pipeline.env_probe import probe_all; import json; print(json.dumps(probe_all(), indent=2, default=str))"],
            capture_output=True, text=True, cwd=str(HERE.parent)).stdout))

    warmup = 2.0 if args.quick else args.warmup
    measured = 5.0 if args.quick else args.measured
    reps = 1 if args.quick else 2  # 2 reps for the most important single-stream case

    tests = set(args.tests.split(","))
    results = {"run_dir": str(run_dir), "warmup_s": warmup, "measured_s": measured,
               "tests": {}}

    if "A" in tests:
        a = []
        backends = ["b0_pyav", "b1_pynv_cpu", "b1_pynv_gpu"]
        for backend in backends:
            for fps in (30, 60):
                for mode in ("static", "motion"):
                    reps_a = reps if (fps == 30 and mode == "motion") else 1
                    a += test_a_single(run_dir, fps, mode, backend, warmup, measured, reps_a)
        results["tests"]["A"] = a

    if "B" in tests:
        b = {}
        # 1080p30: 1, 2, 4 ; 1080p60: 1, 2 ; each in fanout + independent
        for fps, ns in ((30, [1, 2, 4]), (60, [1, 2])):
            for n in ns:
                for fan in ("fanout", "independent"):
                    k = f"1080p{fps}_x{n}_{fan}"
                    b[k] = test_b_multi(run_dir, fps, n, fan, warmup, measured)
        results["tests"]["B"] = b

    with open(run_dir / "poc_summary.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("POC complete. Run dir:", run_dir)


if __name__ == "__main__":
    main()
