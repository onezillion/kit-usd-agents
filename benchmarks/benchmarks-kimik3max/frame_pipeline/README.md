# Frame Pipeline Benchmark (Stage 1)

Proof-of-concept benchmark for the Omniverse Kit capture → encode pipeline:

```
camera/render output → frame capture → preparation/copy/convert → H.264 hardware encode
```

Stage 1 ends at a valid encoded H.264 access-unit/bitstream. **No** RTP/Janus/WebRTC
transport, browser playback, or other Stage 2 work is included.

The goal is to help decide which capture/encode pipeline is the better foundation for the
application's future streaming implementation: the existing-style host path (**B0**, PyAV +
`h264_nvenc`) or the GPU-oriented NVIDIA path (**B1**, PyNvVideoCodec).

This is a **direct camera/render-output** production profile — not desktop/Sunshine/
swapchain screen streaming. The existing `omni.khl.webrtc_stream` extension is reference
material only and is not modified by this benchmark.

## Layout

```
frame_pipeline/
  contracts.py         Encoder-independent CapturedFrame / EncodedAccessUnit / BackendCaps
  bounded_queue.py     Bounded real-time queue with replace-oldest freshness policy + stats
  backend.py           EncoderBackend interface
  backend_b0_pyav.py   B0: PyAV VideoFrame(rgba) + h264_nvenc (production-style host path)
  backend_b1_pynvvc.py B1: PyNvVideoCodec NVENC (host NV12, and gated device-input)
  synthetic_source.py  Deterministic RGBA frame generator (static + repeatable high motion)
  conversion.py        RGBA->NV12: CPU (numpy) reference + GPU (NVRTC cubin JIT) converter
  h264_verify.py       Annex B parser/verifier (SPS/PPS/IDR/profile/resolution/crop/order)
  telemetry.py         Aggregate NVML + /proc/stat sampler (device-wide, labelled aggregate)
  recorder.py          JSONL event log + latency percentiles + summary builder
  harness.py           Producer → capture q → parallel prep pool → per-stream encode workers
  env_probe.py         GPU/driver/CUDA/Python/library/NVENC capability capture
  config.py            Machine-readable case matrix + common encoder intent
  runner.py            CLI: runs suites, verifies streams, writes summary/CSV/reports
  live_bench.py        Drive encode of *real* Kit-captured warehouse frames (.npy)
tests/                 Unit tests (contracts, queue policy, H.264 verifier)
```

## Environment

A dedicated user-local venv is used (does not touch system or global installs):

```
/home/ubuntu/kit-ai/venvs/frame-pipeline-bench   # Python 3.12, numpy, av, PyNvVideoCodec,
                                                  # pynvml, cuda-python, nvidia-cuda-nvrtc-cu12, pytest
```

Add the package to `PYTHONPATH` or run from the `benchmarks/frame_pipeline` directory.

## Run

```bash
cd benchmarks/frame_pipeline
PY=/home/ubuntu/kit-ai/venvs/frame-pipeline-bench/bin/python

# unit tests
$PY -m pytest -q

# full single-encoder suite (B0 + B1, 1080p30/60, static + motion, 10s warm / 30s measured)
$PY -m frame_pipeline --suite single

# multi-encoder suite (fan-out + independent sources, 1/2/4 encoders)
$PY -m frame_pipeline --suite multi

# single named case / quick smoke
$PY -m frame_pipeline --case single_b1h_motion_30
$PY -m frame_pipeline --case single_b1h_motion_30 --quick

# live encode of Kit-captured warehouse frames (requires live_frames/*.npy)
$PY -m frame_pipeline.live_bench --tag static4k --backend b1_host --encoders 2 --loops 3
```

## Output

Generated results go **outside the repository** by default:

```
$HOME/kit-ai/benchmarks/frame-pipeline/<date-suite>/
  environment.json     GPU/driver/CUDA/Python/library/NVENC capabilities
  <case>.summary.json  per-stream metrics, latency percentiles, queue stats, hw proof
  <case>.events.jsonl  append-only event log
  <case>_s0.h264       bounded retained encoded sample (HW-validated)
  comparison.csv       cross-case comparison
  failures.json        any failed cases (if present)
```

Override with `FRAME_PIPELINE_BENCH_OUT=/path`. Live frames path with
`FRAME_PIPELINE_LIVE_FRAMES=/path`. Retained encoded samples are kept small.

## Honest-modeling notes (read before trusting numbers)

* **Hardware proof, never assumed.** A backend only reports `hardware_nvenc=True` after a
  real NVENC session is constructed, and the harness cross-checks NVENC engine utilization
  via NVML. A silent software H.264 fallback is a benchmark *failure*, never a pass.
* **Prep vs encode are separated.** At 1080p the CPU RGBA→NV12 conversion is the bottleneck
  (~47 ms single-thread) while the HW encode submit is ~0.5 ms. They run on different
  threads; latency percentiles are reported per stage.
* **No zero-copy claims.** Every memory step is labelled: known explicit transfer, known
  GPU-local, known CPU, API-hidden/composite, or unknown/unproven.
* **Queue latency is not hidden in encoder latency.** Bounded queues record depth,
  high-water, oldest age, drops, and replacements separately.
* **Encoder-config equivalence is disclosed.** PyNvVideoCodec 2.2.2 ignores `profile`,
  `fps`, `bf`, `rc`, `lookahead` keys (see the report's equivalence table). Do not compare
  backends as if these were identically applied.

See `docs/` in the results directory and the final engineering report for findings.
