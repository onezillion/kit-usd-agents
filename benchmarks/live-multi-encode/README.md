# Live Multi-Camera H.264 Encoding Benchmark (Stage 1) — `benchmarks/live-multi-encode`

Production-oriented **Stage-1** benchmark for the KHL Kit application:

```
live Kit camera / independent render product
  -> frame capture (ByteCapture, AOV=LdrColor, RGBA8)
  -> bounded preallocated shared-memory ring (torn-frame safe)
  -> managed encoder subprocess (dedicated venv, NOT Kit-process)
  -> hardware H.264 (PyAV h264_nvenc)
  -> valid Annex-B access units
```

**Stage 1 ends at a valid encoded H.264 access unit.** No Janus / RTP / aiortc /
WebRTC / browser transport is implemented (that is Stage 2, out of scope).

## Architecture decision

**Architecture B (encoder subprocess)** is the baseline, because it is the proven
production architecture (`omni.khl.webrtc_stream`), isolates encoder Python/CUDA
state from the embedded Kit interpreter, and avoids blocking Kit's event loop with
encode work. The extra host/SHM copy is *measured*, not treated as a failure.

The capture source is **live inside Kit** (real cameras + real independent render
products). The encoder runs in a **separate process** via the dedicated benchmark
venv `/home/ubuntu/kit-ai/venvs/frame-pipeline-bench` — there is **no** external
`site-packages` injection into Kit (avoids runtime contamination).

## Primary engineering question

> How should the current Kit application reliably operate 4–10 simultaneous live
> camera + hardware encoder pipelines with bounded latency?

Scale ramp: `1 2 4 5 6 8 10` @ `1920x1080`, `30 FPS` target per stream, Warehouse
scene workload (`omniverse://140.110.27.92/NVIDIA/Demos/WarehousePhysics/.../World_Demopack.usd`).

## Layout

```
live_encode/
  __init__.py
  contracts.py        CapturedFrame / EncodedAccessUnit / TransferClass / counters
  shm_ring.py         Bounded preallocated SHM ring w/ per-slot frame_id+crc header
  verify_h264.py     Annex-B NAL/SPS/PPS/IDR parser + verifier + psnr
  encoder_worker.py   Encoder subprocess entry (PyAV h264_nvenc, refuses sw fallback)
  shm_transport.py    Producer/consumer SHM attach + slot accounting helpers
kit_side/
  __init__.py
  capture_driver.py   Runs INSIDE Kit via Kit Lab MCP: N cameras + N hydra render
                      products + ByteCapture -> SHM ring. Torn-frame safe, non-blocking.
scripts/
  run_scale.py        Orchestrator: ramp N, warmup+measured, telemetry, results
  probe_clock.py      Cross-process perf_counter offset ping-pong
results are written to $HOME/kit-ai/benchmarks/live-multi-encode/<run-id>/  (NOT into git).
```

## Honest-model rules (read before trusting numbers)

- **Live only for main results.** Synthetic/replay frames are used ONLY for isolated
  encoder debugging. Every scaling row is `pipeline=live`.
- **Independent resources proven, not assumed.** Per-N fps-scaling + capture callback
  timestamp scatter show whether the single RTX HydraEngine serializes render products.
- **Hardware proof, never assumed.** `h264_nvenc` opened == NVENC variant, cross-checked
  against NVML NVENC engine utilization. A silent libx264 fallback is a *failure*.
- **Torn-frame detection.** Each SHM slot carries a `frame_id` + CRC32 header written by
  the producer *after* the pixel copy; the consumer re-checks after read.
- **One clock domain.** Cross-process latency uses an empirically measured
  `perf_counter` offset (ping-pong); same-process deltas are authoritative.
- **Copy accounting.** Each transfer is labelled known-D2H / known-H2H / known-H2D /
  GPU-local / API-hidden / unknown.

## Setup

The encoder subprocess uses the dedicated venv (already provisioned):

```
/home/ubuntu/kit-ai/venvs/frame-pipeline-bench/bin/python   # py3.12: numpy, av 18.1,
                                                            # PyNvVideoCodec 2.2.2,
                                                            # cuda-python, nvidia-ml-py
```

## Run

Driven through the Kit Lab MCP against the live Kit session. See `scripts/run_scale.py`.
