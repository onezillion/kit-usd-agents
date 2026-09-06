"""KHL Frame Pipeline Benchmark — Stage 1 POC harness.

Runs warmup + measured intervals for one or more encoder streams, supporting:
  - single-encoder per backend (Test A);
  - multi-encoder same-frame fan-out (one source feeds N encoders);
  - multi-encoder independent sources (each encoder gets its own source sequence);
  - synchronized start/stop barriers across encoders (Test B).

Per stream: bounded freshness queue, telemetry JSONL, counters, p50/p95/p99/max latency.
Aggregate: device-wide nvidia-smi polling (labelled `aggregate`, never per-stream).

The harness deliberately measures application queueing SEPARATELY from hardware encode
latency (NVENC async is NOT supported on this config per GetEncoderCaps).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .backends.base import BackendSettings, EncoderBackend
from .bounded_queue import BoundedFrameQueue
from .contracts import CapturedFrame, EncodedAccessUnit
from .telemetry import Telemetry


@dataclass
class StreamResult:
    stream_id: str
    backend: str
    summary: dict = field(default_factory=dict)
    queue_stats: dict = field(default_factory=dict)
    samples_first_au: Optional[bytes] = None   # for the verifier
    samples_reference_nv12: Optional[bytes] = None  # host NV12 source frame for PSNR
    sps_pps: Optional[bytes] = None              # canonical SPS+PPS for decode setup
    queue_high_water: int = 0
    drops: int = 0
    replacements: int = 0


def _stream_worker(stream_id: str, backend: EncoderBackend, queue: BoundedFrameQueue,
                   telemetry: Telemetry, warmup_s: float, measured_s: float,
                   start_barrier: threading.Barrier, stop_at_perf: list,
                   sample_first_au: bool, out: dict):
    """Consume frames from `queue`, encode them, record telemetry.

    `stop_at_perf` is a one-element list shared with the main thread; main sets
    stop_at_perf[0] after the barrier releases.  We spin until that deadline.
    Frames encoded before (barrier_release + warmup_s) are warmup and are NOT counted
    in the measured summary (we still log them as events for completeness).
    """
    start_barrier.wait()
    # measured-window start = stop deadline - measured_s (set by main right after barrier)
    # We poll until main populates stop_at_perf[0]; once set, compute our own measured start.
    measured_start = None
    last_qstats_t = 0.0
    first_au_bytes = None
    first_ref_nv12 = None
    n_encoded_measured = 0
    while True:
        now = time.perf_counter()
        if stop_at_perf[0] > 0.0:
            if measured_start is None:
                measured_start = stop_at_perf[0] - measured_s
            if now >= stop_at_perf[0]:
                break
        cf = queue.get(block=True, timeout=0.05)
        if cf is None:
            continue
        in_measured = (measured_start is None) or (cf.capture_ts >= measured_start)
        # record the captured frame (counts produced + capture/prep timing)
        src_timing = getattr(cf, "_source_timing", {})
        telemetry.record_captured(
            cf.summary_dict(),
            capture_cb_duration=src_timing.get("capture_cb_duration_s", 0.0),
            color_convert=src_timing.get("color_convert_duration_s", 0.0),
            frame_prep=src_timing.get("source_total_duration_s", 0.0),
            copies=src_timing.get("copies", {"d2d": 0, "d2h": 0, "h2d": 0}),
        )
        try:
            aus = backend.encode_frame(cf)
        except Exception as e:
            telemetry.event("encode_error", {"frame_id": cf.frame_id,
                                              "error": "%s: %s" % (type(e).__name__, e)})
            continue
        for au in aus:
            if in_measured:
                # flush AUs have source_capture_ts=0.0 and would corrupt latency stats;
                # only count AUs tied to a real captured frame.
                if au.source_capture_ts > 0.0:
                    telemetry.record_encoded(au.summary_dict(), cf.capture_ts,
                                             au.encode_submit_ts, au.encode_completion_ts)
                    n_encoded_measured += 1
            if sample_first_au and au.data and in_measured and au.is_keyframe and first_au_bytes is None:
                # Capture the first IDR AU (carries SPS/PPS+IDR) for the verifier so the
                # decoder has a complete, decodable access unit and PSNR is meaningful.
                first_au_bytes = au.data
                if hasattr(cf, "frame_obj"):
                    fo = cf.frame_obj
                    if hasattr(fo, "shape") and fo.shape == (cf.height * 3 // 2, cf.width):
                        first_ref_nv12 = bytes(fo.tobytes())
        if now - last_qstats_t > 0.5:
            telemetry.record_queue_stats(queue.stats_snapshot())
            last_qstats_t = now
    # flush remaining queued encoder output (NOT counted in latency stats — no source ts)
    aus = backend.flush()
    for au in aus:
        telemetry.event("encoded", au.summary_dict())
        if sample_first_au and first_au_bytes is None and au.data:
            first_au_bytes = au.data
    out["n_encoded"] = n_encoded_measured
    out["first_au_bytes"] = first_au_bytes
    out["first_ref_nv12"] = first_ref_nv12
    out["queue_stats"] = queue.stats_snapshot()


def run_streams(specs: list, warmup_s: float, measured_s: float,
                run_dir: str, sample_first_au: bool = True) -> List[StreamResult]:
    """Run a set of streams in parallel.

    specs: list of dicts with keys:
        stream_id, backend (EncoderBackend instance), source callable returning CapturedFrame
        given a frame index, fps (target), queue_maxsize, queue_policy
    All streams start via a Barrier (=1 per stream + 1 main) and stop together at
    warmup_s + measured_s seconds after the barrier releases.
    """
    n = len(specs)
    # Barrier parties: n consumer (encoder) threads + n producer threads + 1 main.
    barrier = threading.Barrier(2 * n + 1)
    stop_at_perf = [0.0]

    threads = []
    outs = [{} for _ in range(n)]
    queues = []
    telemetries = []
    backends = []
    sources = []

    for i, spec in enumerate(specs):
        q = BoundedFrameQueue(maxsize=spec.get("queue_maxsize", 2),
                              policy=spec.get("queue_policy", "replace_oldest"))
        queues.append(q)
        tele = Telemetry(run_dir, spec["stream_id"], enable_nvsmi=(i == 0))
        telemetries.append(tele)
        backends.append(spec["backend"])
        sources.append(spec["source"])

    # producer threads (one per stream) — feed the queue at the target FPS
    producer_stop = threading.Event()

    def producer(i):
        spec = specs[i]; q = queues[i]; src = sources[i]
        fps = spec["fps"]
        period = 1.0 / fps
        barrier.wait()
        # Wait until main has set the stop deadline (it does so right after barrier
        # release).  Without this, the producer could see stop_at_perf[0]==0.0 and exit
        # immediately, deadlocking the consumers (no frames to consume).
        while stop_at_perf[0] == 0.0 and not producer_stop.is_set():
            time.sleep(0.001)
        idx = 0
        next_t = time.perf_counter()
        while not producer_stop.is_set() and time.perf_counter() < stop_at_perf[0]:
            cf = src(idx)
            cf.frame_id = idx
            q.put(cf, block=False)
            idx += 1
            next_t += period
            sleep_for = next_t - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_t = time.perf_counter()

    # consumer threads
    for i, spec in enumerate(specs):
        t = threading.Thread(target=_stream_worker,
                             args=(spec["stream_id"], backends[i], queues[i],
                                   telemetries[i], warmup_s, measured_s, barrier,
                                   stop_at_perf, sample_first_au, outs[i]),
                             name="enc-%s" % spec["stream_id"])
        threads.append(t)

    producers = [threading.Thread(target=producer, args=(i,), name="src-%s" % specs[i]["stream_id"])
                 for i in range(n)]
    for t in threads:
        t.start()
    for t in producers:
        t.start()

    # release the barrier — start clock
    barrier.wait()
    t_start = time.perf_counter()
    stop_at_perf[0] = t_start + warmup_s + measured_s

    # wait for the measured window
    while time.perf_counter() < stop_at_perf[0]:
        time.sleep(0.1)
    producer_stop.set()

    for t in producers:
        t.join(timeout=2.0)
    for t in threads:
        t.join(timeout=5.0)

    # finalize results
    results = []
    for i, spec in enumerate(specs):
        tele = telemetries[i]
        tele.set_span(measured_s)
        qstats = queues[i].stats_snapshot()
        summary = tele.counters.summary()
        # harvest SPS/PPS BEFORE closing the backend (close may release the encoder)
        try:
            sps_pps = backends[i].sps_pps()
        except Exception:
            sps_pps = None
        results.append(StreamResult(
            stream_id=spec["stream_id"],
            backend=spec["backend"].name,
            summary=summary,
            queue_stats=qstats,
            samples_first_au=outs[i].get("first_au_bytes"),
            samples_reference_nv12=outs[i].get("first_ref_nv12"),
            sps_pps=sps_pps,
            queue_high_water=qstats.get("high_water", 0),
            drops=qstats.get("dropped", 0),
            replacements=qstats.get("replaced", 0),
        ))
        tele.close()
    # ensure all backends are closed
    for b in backends:
        try:
            b.close()
        except Exception:
            pass
    return results
