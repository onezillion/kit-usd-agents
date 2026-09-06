"""KHL Frame Pipeline Benchmark (Stage 1).

POC harness comparing capture/prepare/H.264-HW-encode backends for the Omniverse Kit
streaming application.  Stage 1 ends at a valid encoded H.264 access unit; Janus/RTP/
WebRTC transport is OUT OF SCOPE.

Modules:
  - contracts: encoder-independent CapturedFrame / EncodedAccessUnit dataclasses.
  - bounded_queue: bounded freshness-preferring queue with metrics.
  - telemetry: per-stream JSONL events + p50/p95/p99 latency + aggregate nvidia-smi.
  - backends.base / b0_pyav / b1_pynv: B0 existing-style and B1 NVIDIA GPU encode paths.
  - sources.synthetic: deterministic static + high-motion frame sources.
  - h264_verify: Annex-B NAL walk, SPS/PPS/profile/IDR/decode + PSNR/SSIM.
  - harness: warmup/measured multi-encoder runner (fan-out + independent).
  - env_probe: capture Kit/GPU/driver/CUDA/Python/PyAV/NVENC/PyNvVideoCodec provenance.
"""
__version__ = "0.1.0"
