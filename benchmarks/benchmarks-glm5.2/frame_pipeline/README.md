# KHL Frame Pipeline Benchmark (Stage 1) — `benchmarks/frame_pipeline`

Proof-of-concept benchmark for the Omniverse Kit frame pipeline:

```
camera/render output -> frame capture -> frame prep / copy / color conversion -> H.264 HW encode
```

**Stage 1 only.**  Janus / RTP / WebRTC transport and Stage 2 work are explicitly out of scope
and are NOT implemented here.  The production `omni.khl.webrtc_stream` extension was inspected
read-only for evidence; it was NOT modified.

## Scope

- **B0 — existing-style baseline:** PyAV `h264_nvenc`, host RGBA8 input, CPU swscale
  RGBA→YUV420p, then NVENC.  Reproduces the meaningful production encode path without the
  RTP packetization.  Detects & refuses silent software fallback to libx264.
- **B1 — NVIDIA GPU-oriented encode path:** PyNvVideoCodec 2.2.2.
  - `b1_pynv_cpu` — host NV12 input (fair equivalent of B0 without swscale).
  - `b1_pynv_gpu` — device-memory NV12 input via `__cuda_array_interface__` (cuda-python;
    pycuda not required because no CUDA toolkit is installed on this host).
- **B2** — lower-level Video Codec SDK / NvEncoderCUDA — NOT implemented; B1 already
  provides every capability the Stage 1 decision needs (see Final Report §15).

## Layout

```
benchmarks/frame_pipeline/
├── config.json               # machine-readable benchmark config (test matrix + settings)
├── requirements.txt          # pinned deps (numpy, av, PyNvVideoCodec, cuda-python)
├── README.md                 # this file
├── frame_pipeline/
│   ├── __init__.py
│   ├── contracts.py          # CapturedFrame / EncodedAccessUnit dataclasses
│   ├── bounded_queue.py      # bounded freshness-preferring queue + metrics
│   ├── telemetry.py          # JSONL events + p50/p95/p99 + aggregate nvidia-smi
│   ├── env_probe.py          # Kit/GPU/driver/CUDA/PyAV/NVENC/PyNvVideoCodec probe
│   ├── h264_verify.py        # NAL/SPS/PPS/profile/IDR/decode + PSNR/SSIM
│   ├── harness.py            # warmup/measured multi-encoder runner
│   ├── run_poc.py            # POC orchestrator (Tests A + B; C lives in live_kit_capture.py)
│   ├── _worker.py            # subprocess worker (one process per backend)
│   ├── backends/
│   │   ├── base.py           # EncoderBackend ABC + BackendSettings
│   │   ├── b0_pyav.py        # B0 PyAV h264_nvenc
│   │   └── b1_pynv.py        # B1 PyNvVideoCodec (CPU + GPU input)
│   └── sources/
│       └── synthetic.py      # deterministic static + high-motion GPU/host sources
├── live_kit_capture.py       # Test C: live warehouse capture via Kit Lab MCP
└── tests/
    └── test_unit.py          # pytest unit tests
```

Generated results live **outside** the repository at `$HOME/kit-ai/benchmarks/frame-pipeline/<run-id>/`
(override with `FRAME_PIPELINE_RESULTS_DIR`).  No generated runs, caches, credentials, or large
encoded video files are committed; only small retained AU samples for the verifier.

## Setup (user-local venv only; do NOT modify system Python)

```bash
python3 -m venv /home/ubuntu/kit-ai/venvs/frame-pipeline-bench
/home/ubuntu/kit-ai/venvs/frame-pipeline-bench/bin/python -m pip install -r benchmarks/frame_pipeline/requirements.txt
```

## Run

```bash
# Unit tests + static checks
/home/ubuntu/kit-ai/venvs/frame-pipeline-bench/bin/python -m pytest benchmarks/frame_pipeline/tests -q
/home/ubuntu/kit-ai/venvs/frame-pipeline-bench/bin/python -m py_compile benchmarks/frame_pipeline/frame_pipeline/*.py

# Environment probe
/home/ubuntu/kit-ai/venvs/frame-pipeline-bench/bin/python -m frame_pipeline.env_probe /tmp/env_probe.json

# Quick smoke POC (short durations, 1 rep)
cd benchmarks/frame_pipeline
/home/ubuntu/kit-ai/venvs/frame-pipeline-bench/bin/python -m frame_pipeline.run_poc --quick --tests A,B

# Full POC (warmup 10s, measured 30s, 2 reps on the key single-stream case)
/home/ubuntu/kit-ai/venvs/frame-pipeline-bench/bin/python -m frame_pipeline.run_poc --tests A,B
```

## Encoder equivalence (READ BEFORE COMPARING)

The job requests Main profile.  B0 (PyAV) honours `profile=main` and emits Main
(SPS `profile_idc=0x4d`).  B1 (PyNvVideoCodec 2.2.2) exposes **no** documented H.264
`profile` knob and emits High (`profile_idc=0x64`).  This is a material equivalence
mismatch and is disclosed in the Final Report §5 and §9; it is NOT hidden.  All other
settings (preset P1, ultra-low-latency, CBR, bf=0, lookahead=0, AQ off, fixed GOP) are
kept equivalent.

## NVENC async semantics

`GetEncoderCaps()['async_encode_support'] == 0` on this Blackwell config.  We therefore
do NOT claim NVENC async encode mode.  Encode() is synchronous from the caller's
perspective; application queueing is measured **separately**.

## Live Kit capture (Test C)

`live_kit_capture.py` drives the live Warehouse scene via the Kit Lab MCP at
`http://127.0.0.1:9910/mcp`.  It uses a benchmark-created perspective camera at a
deterministic placement, runs a static and a repeatable motion view, captures via
`omni.kit.widget.viewport.capture.ByteCapture` (matching the production baseline), and
hands frames to the B0 baseline encoder.  Direct live-Kit GPU handoff to B1-GPU is
**capability-gated** (no proven Kit→CUDA texture interop); this is recorded as a gap,
not fabricated.
