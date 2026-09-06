"""Benchmark harness.

Pipeline per case::

    producer thread  ->  [BoundedFrameQueue: captured]  ->  prep pool (parallel)
      ->  encode-submit thread (ordered)  ->  EncodedAccessUnit  ->  recorder

Separating *preparation* (RGBA->NV12/YUV color conversion) from *encode
submission* is essential: at 1080p the CPU color conversion is tens of
milliseconds while the hardware encode submit is ~1 ms. Running them serially
would starve a 30/60 fps target; the prep pool parallelizes the heavy step
while a single submit thread preserves encode input order.

All queues are bounded and prefer freshness (replace-oldest) so producer
backpressure becomes recorded drops/replacements rather than unbounded delay.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .backend import EncoderBackend
from .bounded_queue import BoundedFrameQueue
from .contracts import CapturedFrame, EncodedAccessUnit, now_ns
from .recorder import RunRecorder
from .telemetry import TelemetrySampler


@dataclass
class StreamResult:
    stream_name: str
    backend: str
    captured: int = 0
    prepared: int = 0
    encoded_frames: int = 0
    bitstream: bytes = b""
    hw_nvenc_active: bool = False
    hardware_proof: str = ""
    transfer_log: list = field(default_factory=list)
    verify: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


@dataclass
class CaseResult:
    name: str
    config: dict
    streams: list = field(default_factory=list)
    telemetry: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    queue_stats: dict = field(default_factory=dict)
    produced_fps: float = 0.0
    duration_s: float = 0.0


class _EncodeWorker(threading.Thread):
    """Serially encodes prepared inputs for one stream (preserves order)."""

    def __init__(self, name, backend, in_queue, recorder, result, sink) -> None:
        super().__init__(name=f"enc-{name}", daemon=True)
        self.name_s, self.be, self.q, self.rec, self.result, self.sink = (
            name, backend, in_queue, recorder, result, sink)
        self._last_capture_ns: Optional[int] = None
        self.done = threading.Event()

    def run(self) -> None:
        be, rec, res = self.be, self.rec, self.result
        try:
            while True:
                item = self.q.get(timeout=0.1)
                if item is None:
                    if self.q.is_closed:
                        break
                    continue
                raw, frame = item
                if self._last_capture_ns is not None:
                    rec.record_frame_interval(self.name_s, frame.capture_ts_ns - self._last_capture_ns)
                self._last_capture_ns = frame.capture_ts_ns
                aus = be.encode_raw(raw, frame)
                if getattr(be, "last_submit_ns", 0):
                    rec.latency(self.name_s, "submit_ns").add(be.last_submit_ns)
                for au in aus:
                    res.encoded_frames += 1
                    if au.capture_ts_ns:
                        rec.latency(self.name_s, "capture_to_encoded_ns").add(au.capture_to_encode_ns)
                    rec.record_encoded_size(self.name_s, au.size_bytes)
                    self.sink.extend(au.bitstream)
                res.captured += 1
        except Exception as exc:
            res.errors.append(f"{type(exc).__name__}: {exc}")
            rec.event("encode_error", {"stream": self.name_s, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            try:
                for au in be.flush():
                    res.encoded_frames += 1
                    rec.record_encoded_size(self.name_s, au.size_bytes)
                    self.sink.extend(au.bitstream)
            except Exception as exc:
                res.errors.append(f"flush {type(exc).__name__}: {exc}")
            res.bitstream = bytes(self.sink)
            caps = be.caps()
            res.hw_nvenc_active = caps.hardware_nvenc
            res.hardware_proof = caps.hardware_proof
            res.transfer_log = list(getattr(be, "_transfer_log", []))
            try:
                be.close()
            except Exception:
                pass
            self.done.set()


class Harness:
    def __init__(self, recorder: RunRecorder, telemetry: Optional[TelemetrySampler] = None) -> None:
        self.rec = recorder
        self.telemetry = telemetry

    def run_case(
        self,
        case_name: str,
        backends: list[EncoderBackend],
        make_frame: Callable[[str, int], CapturedFrame],
        frame_ids: list[int],
        fps: int,
        prep_workers: int = 4,
        warmup_s: float = 0.0,
        prep_queue_capacity: int = 64,
        capture_queue_capacity: int = 64,
    ) -> CaseResult:
        if warmup_s > 0:
            self._warmup(backends, make_frame, fps, warmup_s)

        streams = [f"s{i}" for i in range(len(backends))]
        # Open all backends up-front (after warmup closed them).
        for be in backends:
            be.open()
        results = {s: StreamResult(stream_name=s, backend=backends[i].name) for i, s in enumerate(streams)}
        sinks = {s: bytearray() for s in streams}
        # bounded queues
        capture_q = BoundedFrameQueue(capture_queue_capacity, policy="replace_oldest", name="capture")
        enc_q = {s: BoundedFrameQueue(prep_queue_capacity, policy="replace_oldest", name=f"enc-{s}") for s in streams}
        workers = [_EncodeWorker(s, backends[i], enc_q[s], self.rec, results[s], sinks[s]) for i, s in enumerate(streams)]
        for w in workers:
            w.start()

        prep_done = threading.Event()
        pump_err: list = []

        def prep_pump() -> None:
            # Receives (frame_id,) markers; materializes per-stream frames and
            # prepares them in bounded parallel batches.
            try:
                batch: list = []
                while True:
                    fid = capture_q.get(timeout=0.1)
                    if fid is None:
                        if capture_q.is_closed:
                            break
                        continue
                    batch.append(fid)
                    if len(batch) < prep_workers:
                        continue
                    self._prep_markers(batch, streams, backends, make_frame, enc_q)
                    batch.clear()
                if batch:
                    self._prep_markers(batch, streams, backends, make_frame, enc_q)
            except Exception as exc:
                pump_err.append(f"{type(exc).__name__}: {exc}")
                self.rec.event("prep_error", {"error": f"{type(exc).__name__}: {exc}"})
            finally:
                for q in enc_q.values():
                    q.close()
                prep_done.set()

        pump = threading.Thread(target=prep_pump, name="prep-pump", daemon=True)
        pump.start()
        # producer
        t0 = time.monotonic_ns()
        self._produce(capture_q, frame_ids, fps)
        capture_q.close()
        t1 = time.monotonic_ns()
        pump.join(timeout=300.0)
        for w in workers:
            w.done.wait(timeout=120.0)
            w.join(timeout=5.0)
        duration_s = (t1 - t0) / 1e9
        case = CaseResult(
            name=case_name,
            config={"fps": fps, "frames": len(frame_ids), "streams": len(backends),
                    "prep_workers": prep_workers},
            streams=[results[s] for s in streams],  # noqa: E501
            queue_stats={"capture": capture_q.snapshot(),
                         **{s: enc_q[s].snapshot() for s in streams}},
            produced_fps=round(len(frame_ids) / duration_s, 2) if duration_s > 0 else 0.0,
            duration_s=round(duration_s, 4),
        )
        if pump_err:
            for s in streams:
                results[s].errors.extend(pump_err)
        if self.telemetry is not None:
            case.telemetry = self.telemetry.summarize()
            self._crosscheck_hw(case)
        self.rec.extra["queues"] = case.queue_stats
        self.rec.flush()
        return case

    def _prep_markers(self, batch, streams, backends, make_frame, enc_q) -> None:
        """Materialize frames for a batch of frame_ids across all streams and
        prepare them in parallel, then hand raw inputs to each stream worker."""

        from concurrent.futures import ThreadPoolExecutor
        backend_by_stream = {s: backends[i] for i, s in enumerate(streams)}
        jobs = [(s, make_frame(s, fid)) for fid in batch for s in streams]
        with ThreadPoolExecutor(max_workers=max(1, len(jobs)), thread_name_prefix="prepone") as pool:
            futs = [(s, f, pool.submit(backend_by_stream[s].prepare, [f])) for (s, f) in jobs]
            for (s, frame, fut) in futs:
                for raw in fut.result():
                    enc_q[s].put((raw, frame))

    def _produce(self, capture_q, frame_ids, fps) -> None:
        frame_dt = 1.0 / fps
        next_t = time.monotonic()
        for fid in frame_ids:
            now = time.monotonic()
            delay = next_t - now
            if delay > 0:
                time.sleep(delay)
            next_t = max(next_t + frame_dt, time.monotonic())
            capture_q.put(fid)

    def _warmup(self, backends, make_frame, fps, warmup_s) -> None:
        n = max(1, int(warmup_s * fps))
        # Warmup uses dedicated throwaway frames so it never touches the measured
        # generators or the recorder's clock, keeping the measurement clean.
        from .synthetic_source import SceneGenerator, SyntheticSource
        from .contracts import MemoryDomain
        for i, be in enumerate(backends):
            be.open()
            warm_gen = SceneGenerator(be.width or 1920, be.height or 1080, "motion", seed=999999 + i)
            for fid in range(n):
                frame = SyntheticSource(warm_gen, MemoryDomain.CPU).make_frame(fid)
                be.encode(frame)
            be.flush()
            be.close()

    def _crosscheck_hw(self, case: CaseResult) -> None:
        gpus = case.telemetry.get("gpus", {})
        max_enc = 0.0
        for gm in gpus.values():
            enc = gm.get("enc_util_pct", {})
            if isinstance(enc, dict):
                max_enc = max(max_enc, enc.get("max", 0.0))
        case.summary.setdefault("hw_crosscheck", {})["max_enc_util_pct_observed"] = max_enc


def hardware_nvenc_crosscheck_fail(case: CaseResult, require_util: float = 0.5) -> list[str]:
    """Return failure strings if a stream claims HW but encoder utilization ~ 0."""

    fails: list[str] = []
    gpus = case.telemetry.get("gpus", {})
    max_enc = 0.0
    for gm in gpus.values():
        enc = gm.get("enc_util_pct", {})
        if isinstance(enc, dict):
            max_enc = max(max_enc, enc.get("max", 0.0))
    for st in case.streams:
        if st.hw_nvenc_active and max_enc < require_util and st.encoded_frames > 8:
            fails.append(
                f"{st.stream_name}: claims HW NVENC but max encoder utilization "
                f"{max_enc}% < {require_util}% -> possible silent software fallback"
            )
    return fails
