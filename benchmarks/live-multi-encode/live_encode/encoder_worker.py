"""Encoder subprocess worker (Architecture B / B0 baseline).

Runs in the DEDICATED benchmark venv (NOT Kit's embedded Python). Attaches to one
stream's SHM ring + a bounded signal queue of slot indices, and encodes each
signalled slot to hardware H.264 with PyAV ``h264_nvenc``. A silent libx264
fallback is a hard failure.

Invoked as a spawned subprocess by ``scripts/run_scale.py``::

    python -m live_encode.encoder_worker \
        --stream-id S0 --shm-name <name> --width 1920 --height 1080 --nslots 5 \
        --out-dir <dir> --control-fd <r> --result <json>

The parent passes the slot-index signal queue and a stop event via
``multiprocessing`` shared objects given on the command line through a small
bootstrap (see run_scale.py); for simplicity the worker reads slot indices from a
``multiprocessing.Queue`` reference handed over by inheritance on spawn is NOT
possible, so instead run_scale spawns with ``target=_main`` passing mp objects
directly. This module exposes ``main(...)`` for that and a CLI for a standalone
isolated-encoder test from a raw .rgba file.
"""

from __future__ import annotations

import json
import os
import sys
import time
import zlib
from dataclasses import asdict
from fractions import Fraction
from queue import Empty, Full
from typing import Optional

from .contracts import PixelFormat, mono_ns
from .shm_ring import ConsumerRing
from . import verify_h264

# Signal datagram layout: "<iI" -> (stream_slot_index:int32, frame_id:uint32).
# The producer (Kit) sends one 8-byte datagram per written slot to the worker's
# unix datagram socket; frame_id lets the worker cross-check the slot header.
SIG_STRUCT_FMT = "<iI"
SIG_BYTES = 8


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[max(0, min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1)))))]


class EncoderWorker:
    """Per-stream hardware H.264 encoder consuming ring slots via a unix datagram
    signal socket + shared-memory payload ring (Architecture B, production-like)."""

    def __init__(self, stream_id: str, shm_name: str, width: int, height: int,
                 nslots: int, fps: int, bitrate: int, gop: int,
                 out_dir: str,
                 prep_threads: int = 0,
                 retain_au: int = 0):
        self.stream_id = stream_id
        self.width, self.height = width, height
        self.fps = fps
        self.bitrate = bitrate
        self.gop = gop
        self.out_dir = out_dir
        self.prep_threads = max(0, prep_threads)
        self.retain_au = max(0, retain_au)
        os.makedirs(out_dir, exist_ok=True)
        self.ring = ConsumerRing(shm_name, stream_id, width, height, nslots)
        self._codec = None
        self._av = None
        self.hardware_proof = ""
        self.codec_name = "unknown"
        # counters
        self.encode_submitted = 0
        self.encoded_access_units = 0
        self.torn_frames = 0
        self.frames_read = 0
        self.sig_mismatch = 0
        # duration lists (seconds)
        self.t_read = []
        self.t_prep = []
        self.t_submit = []
        self.au_sizes = []
        self.first_au: Optional[bytes] = None
        self._sample_frames = []  # (frame_id, rgba bytes) for PSNR ref, bounded
        self._started_mono = mono_ns()

    # -- encoder ------------------------------------------------------------
    def _open_encoder(self) -> None:
        import av  # in the venv
        self._av = av
        ctx = av.codec.CodecContext.create("h264_nvenc", "w")
        ctx.width = self.width
        ctx.height = self.height
        ctx.pix_fmt = "yuv420p"
        ctx.framerate = Fraction(self.fps, 1)
        ctx.time_base = Fraction(1, 90000)
        ctx.bit_rate = int(self.bitrate)
        # CBR via private encoder options (PyAV 18 has no max_rate attr). Set
        # bitrate/maxrate/vbv to the same value for a true constant bitrate.
        opts = {
            "preset": "p1",
            "tune": "ull",
            "profile": "main",
            "rc": "cbr",
            "bitrate": str(self.bitrate),
            "maxrate": str(self.bitrate),
            "bufsize": str(self.bitrate * 2 // max(1, self.fps) or self.bitrate),
            "bf": "0",
            "rc-lookahead": "0",
            "g": str(self.gop),
            # repeat SPS/PPS before each IDR so raw Annex-B dumps are self-contained
            "repeat_sps_pps": "1",
        }
        try:
            ctx.options = opts
        except Exception:
            # tolerate builds that reject some private opts; set one-by-one
            for k, v in opts.items():
                try:
                    ctx.options[k] = v
                except Exception:
                    pass
        ctx.open()
        name = ctx.codec.name if ctx.codec else ""
        if name not in ("h264_nvenc", "nvenc", "nvenc_h264"):
            raise RuntimeError(
                f"[{self.stream_id}] requested h264_nvenc but opened '{name}': "
                "REFUSING software fallback")
        self._codec = ctx
        self.codec_name = name
        self.hardware_proof = (
            f"FFmpeg codec '{name}' opened (NVENC variant); "
            "engine utilization cross-checked by orchestrator NVML")

    # -- main loop ----------------------------------------------------------
    def run(self, sig_sock_path: str, stop_flag: dict) -> dict:
        """Consume slot signals from a unix datagram socket until told to stop.

        ``stop_flag`` is a dict; the loop exits when ``stop_flag.get('stop')`` is
        truthy or a sentinel slot index of -1 arrives. Returns the summary dict.
        """
        import socket
        self._open_encoder()
        av = self._av
        import numpy as np
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            os.unlink(sig_sock_path)
        except FileNotFoundError:
            pass
        sock.bind(sig_sock_path)
        sock.settimeout(0.25)
        import struct as _struct
        while not stop_flag.get("stop"):
            try:
                data, _ = sock.recvfrom(64)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < SIG_BYTES:
                continue
            slot, sig_frame_id = _struct.unpack(SIG_STRUCT_FMT, data[:SIG_BYTES])
            if slot < 0:
                break
            t_read0 = mono_ns()
            view, frame_id, cts, ok = self.ring.read(slot)
            t_read1 = mono_ns()
            if not ok:
                self.torn_frames += 1
                continue
            if sig_frame_id != (frame_id & 0xFFFFFFFF):
                self.sig_mismatch += 1
                # not necessarily fatal; producer may have advanced. Count + skip.
                continue
            self.frames_read += 1
            payload = bytes(view)  # H2H snapshot so prep is not racy vs producer
            t_prep0 = mono_ns()
            arr = np.frombuffer(payload, dtype=np.uint8).reshape(
                (self.height, self.width, 4))
            vframe = av.VideoFrame.from_ndarray(arr, format="rgba")
            vframe = vframe.reformat(width=self.width, height=self.height,
                                     format="yuv420p")
            vframe.pts = frame_id * int(90000 / self.fps)
            vframe.time_base = Fraction(1, 90000)
            t_prep1 = mono_ns()
            self.encode_submitted += 1
            t_sub0 = mono_ns()
            pkts = self._codec.encode(vframe)
            t_sub1 = mono_ns()
            self.t_read.append((t_read1 - t_read0) / 1e9)
            self.t_prep.append((t_prep1 - t_prep0) / 1e9)
            self.t_submit.append((t_sub1 - t_sub0) / 1e9)
            if pkts:
                au = b"".join(bytes(p) for p in pkts)
                if au:
                    self.encoded_access_units += 1
                    self.au_sizes.append(len(au))
                    if self.first_au is None:
                        self.first_au = au
                    if self.retain_au > 0 and len(self._sample_frames) < self.retain_au:
                        self._sample_frames.append((frame_id, payload, au))
                    self._write_au(frame_id, au, cts, t_sub0, t_sub1)
        try:
            flush_pkts = self._codec.encode(None)
        except Exception:
            flush_pkts = []
        flush_au = b"".join(bytes(p) for p in flush_pkts)
        try:
            sock.close()
        except Exception:
            pass
        try:
            os.unlink(sig_sock_path)
        except FileNotFoundError:
            pass
        return self._summarize(flush_au)

    def _write_au(self, frame_id, au, capture_ts_ns, t_sub0, t_sub1):
        # append to a per-stream event log (bounded; AU bytes not stored wholesale)
        try:
            rec = {
                "t": time.time(), "mono": mono_ns(), "stream": self.stream_id,
                "frame_id": frame_id, "au_bytes": len(au),
                "capture_ts_ns": capture_ts_ns,
                "encode_submit_ns": t_sub0, "encode_complete_ns": t_sub1,
                "is_idr": self.encoded_access_units % max(1, self.gop) == 1,
            }
            with open(self._log_path(), "a", buffering=1) as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception:
            pass

    def _log_path(self):
        return os.path.join(self.out_dir, f"aus_{self.stream_id}.jsonl")

    def _summarize(self, flush_au: bytes) -> dict:
        # verify the first retained AU + the concatenation we can reconstruct
        verify = {}
        if self.first_au:
            vr = verify_h264.verify_bitstream(self.first_au, self.width, self.height)
            verify = vr.as_dict()
        # persist one small encoded sample for external verification/playback
        if self.first_au:
            try:
                with open(os.path.join(self.out_dir, f"sample_{self.stream_id}.h264"), "wb") as fh:
                    fh.write(self.first_au)
            except Exception:
                pass
        dur = max(1e-9, (mono_ns() - self._started_mono) / 1e9)
        psnr_info = self._persist_retained() if self._sample_frames else {"pairs": 0}
        return {
            "stream_id": self.stream_id,
            "resolution": [self.width, self.height],
            "fps_target": self.fps,
            "codec": self.codec_name,
            "hardware_nvenc": self.codec_name in ("h264_nvenc", "nvenc", "nvenc_h264"),
            "hardware_proof": self.hardware_proof,
            "frames_read": self.frames_read,
            "encode_submitted": self.encode_submitted,
            "encoded_access_units": self.encoded_access_units,
            "torn_frames": self.torn_frames + self.ring.torn,
            "sig_mismatch": self.sig_mismatch,
            "encoded_fps_over_run": self.encoded_access_units / dur,
            "read_ms": self._stats(self.t_read),
            "prep_ms": self._stats(self.t_prep),
            "submit_ms": self._stats(self.t_submit),
            "au_bytes_p50": _pct(self.au_sizes, 50),
            "flush_au_bytes": len(flush_au),
            "first_au_verify": verify,
            "psnr_frame_matched": psnr_info,
        }

    def _persist_retained(self) -> dict:
        """Write retained (source payload, encoded AU) pairs to disk and compute
        frame-matched PSNR (decoded AU vs the ACTUAL captured source frame).
        With bf=0 the k-th encoded AU corresponds to the k-th submitted frame."""
        import numpy as np
        from .verify_h264 import psnr
        import av
        out = {"pairs": 0, "psnr_db": []}
        dec = av.codec.CodecContext.create("h264", "r")
        decoded = []
        for (frame_id, payload, au) in self._sample_frames:
            try:
                for fr in dec.decode(av.packet.Packet(au)):
                    decoded.append(fr)
            except Exception:
                pass
        for fr in dec.decode(None):
            decoded.append(fr)
        for k, (frame_id, payload, au) in enumerate(self._sample_frames):
            try:
                np.save(os.path.join(self.out_dir, f"src_{self.stream_id}_f{frame_id}.npy"),
                        np.frombuffer(payload, dtype=np.uint8).reshape((self.height, self.width, 4)))
                with open(os.path.join(self.out_dir, f"enc_{self.stream_id}_f{frame_id}.h264"), "wb") as fh:
                    fh.write(au)
                if k < len(decoded):
                    dec_rgb = decoded[k].to_ndarray(format="rgb24")
                    src_rgb = np.frombuffer(payload, dtype=np.uint8).reshape(
                        (self.height, self.width, 4))[..., :3]
                    out["psnr_db"].append(round(float(psnr(src_rgb, dec_rgb)), 2))
                out["pairs"] += 1
            except Exception:
                pass
        return out

    @staticmethod
    def _stats(vals):
        return {
            "p50": _pct(vals, 50) * 1000.0,
            "p95": _pct(vals, 95) * 1000.0,
            "p99": _pct(vals, 99) * 1000.0,
            "max": (max(vals) if vals else 0.0) * 1000.0,
            "mean": (sum(vals) / len(vals) * 1000.0) if vals else 0.0,
        }

    def close(self):
        try:
            if self._codec is not None:
                self._codec.encode(None)
        except Exception:
            pass
        self._codec = None
        self.ring.close()


def run_subprocess(stream_id: str, shm_name: str, width: int, height: int,
                   nslots: int, fps: int, bitrate: int, gop: int,
                   out_dir: str, sig_sock_path: str, result_path: str,
                   retain_au: int = 0, prep_threads: int = 0) -> dict:
    """Entry executed inside a spawned encoder subprocess.

    Reads slot signals from ``sig_sock_path`` until a -1 sentinel or an external
    terminate, writes the summary JSON to ``result_path``. Never dies silently.
    """
    worker = EncoderWorker(
        stream_id=stream_id, shm_name=shm_name, width=width, height=height,
        nslots=nslots, fps=fps, bitrate=bitrate, gop=gop, out_dir=out_dir,
        prep_threads=prep_threads, retain_au=retain_au)
    stop_flag = {"stop": False}
    try:
        result = worker.run(sig_sock_path, stop_flag)
    except Exception as exc:
        result = {"stream_id": stream_id, "error": f"{type(exc).__name__}: {exc}",
                  "hardware_nvenc": False}
    finally:
        worker.close()
    try:
        with open(result_path, "w") as fh:
            json.dump(result, fh, indent=1)
    except Exception:
        pass
    return result


def _cli():  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="encoder subprocess / isolated smoke")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run as an encoder subprocess")
    r.add_argument("--stream-id", required=True)
    r.add_argument("--shm-name", required=True)
    r.add_argument("--width", type=int, required=True)
    r.add_argument("--height", type=int, required=True)
    r.add_argument("--nslots", type=int, default=5)
    r.add_argument("--fps", type=int, default=30)
    r.add_argument("--bitrate", type=int, default=10_000_000)
    r.add_argument("--gop", type=int, default=60)
    r.add_argument("--out-dir", required=True)
    r.add_argument("--sock", required=True)
    r.add_argument("--result", required=True)
    r.add_argument("--retain-au", type=int, default=0)
    r.add_argument("--prep-threads", type=int, default=0)

    s = sub.add_parser("smoke", help="synthetic isolated encoder test (NOT live)")
    s.add_argument("--width", type=int, default=1920)
    s.add_argument("--height", type=int, default=1080)
    s.add_argument("--frames", type=int, default=60)
    s.add_argument("--out-dir", default="/tmp/lme_enc_smoke")
    a = ap.parse_args()

    if a.cmd == "run":
        res = run_subprocess(
            stream_id=a.stream_id, shm_name=a.shm_name, width=a.width,
            height=a.height, nslots=a.nslots, fps=a.fps, bitrate=a.bitrate,
            gop=a.gop, out_dir=a.out_dir, sig_sock_path=a.sock,
            result_path=a.result, retain_au=a.retain_au,
            prep_threads=a.prep_threads)
        print(json.dumps({"stream_id": a.stream_id, "ok": "error" not in res}))
        return

    # smoke: synthetic RGBA ring + local consumer in-process (NOT a live result)
    _smoke(a)


def _smoke(a):  # pragma: no cover
    """In-process synthetic encode+verify sanity (NOT a live result)."""
    import numpy as np
    import socket, struct, threading
    os.makedirs(a.out_dir, exist_ok=True)
    from .shm_ring import ProducerRing
    nslots = 5
    ring = ProducerRing(None, "smoke", a.width, a.height, nslots)
    sock_path = os.path.join(a.out_dir, "smoke.sock")
    result_path = os.path.join(a.out_dir, "smoke_result.json")
    stop = {"stop": False}

    def producer():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        base = np.zeros((a.height, a.width, 4), dtype=np.uint8)
        yy, xx = np.mgrid[0:a.height, 0:a.width]
        fid = 0
        while fid < a.frames and not stop.get("stop"):
            base[..., 0] = (xx + fid * 5) % 256
            base[..., 1] = (yy + fid * 3) % 256
            base[..., 2] = ((xx // 40) + (yy // 40) * 7 + fid * 11) % 256
            base[..., 3] = 255
            slot, f2 = ring.write_from_address(base.ctypes.data, mono_ns())
            if slot is not None:
                try:
                    s.sendto(struct.pack(SIG_STRUCT_FMT, slot, fid & 0xFFFFFFFF),
                             sock_path)
                except Exception:
                    pass
            fid += 1
            time.sleep(1.0 / 30.0)
        # sentinel
        try:
            s.sendto(struct.pack(SIG_STRUCT_FMT, -1, 0), sock_path)
        except Exception:
            pass
        stop["stop"] = True

    w = EncoderWorker("smoke", ring.name, a.width, a.height, nslots, 30,
                      10_000_000, 60, a.out_dir, retain_au=1)
    pt = threading.Thread(target=producer, daemon=True)
    pt.start()
    res = w.run(sock_path, stop)
    pt.join(timeout=3)
    w.close()
    ring.close()
    print(json.dumps(res, indent=1))


if __name__ == "__main__":  # pragma: no cover
    _cli()
