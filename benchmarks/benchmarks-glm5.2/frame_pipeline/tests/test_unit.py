"""Unit tests for the KHL Frame Pipeline Benchmark.

Run from the repo with:
  /home/ubuntu/kit-ai/venvs/frame-pipeline-bench/bin/python -m pytest benchmarks/frame_pipeline/tests -q
"""
import os
import sys
import time
from pathlib import Path

# ensure package importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
import pytest

from frame_pipeline.contracts import CapturedFrame, EncodedAccessUnit, DOMAIN_HOST_NUMPY
from frame_pipeline.bounded_queue import BoundedFrameQueue
from frame_pipeline.h264_verify import (find_start_codes, split_nals, nal_type,
                                          parse_sps, profile_name, psnr, ssim,
                                          verify_bitstream)


# --------- contracts ---------
def test_captured_frame_summary_strips_opaque():
    arr = np.zeros((4, 4, 4), dtype=np.uint8)
    cf = CapturedFrame(frame_id=1, capture_ts=1.0, width=4, height=4,
                       pixel_format="RGBA", memory_domain=DOMAIN_HOST_NUMPY,
                       frame_obj=arr)
    d = cf.summary_dict()
    assert "frame_obj" not in d
    assert d["frame_id"] == 1


def test_encoded_au_summary_hides_data():
    au = EncodedAccessUnit(frame_id=0, source_capture_ts=0.0,
                           encode_submit_ts=0.0, encode_completion_ts=1.0,
                           encoded_size_bytes=5, data=b"hello")
    d = au.summary_dict()
    assert "data" not in d
    assert d["data_len"] == 5


# --------- bounded queue ---------
class _FakeFrame:
    def __init__(self, fid, ts):
        self.frame_id = fid
        self.capture_ts = ts


def test_queue_drop_newest_policy():
    q = BoundedFrameQueue(maxsize=2, policy="drop_newest",
                          clock=lambda: 0.0)
    assert q.put(_FakeFrame(0, 0.0))
    assert q.put(_FakeFrame(1, 0.0))
    assert not q.put(_FakeFrame(2, 0.0))   # full -> dropped
    assert q.stats.dropped == 1
    assert q.qsize() == 2


def test_queue_replace_oldest_policy():
    clk = [0.0]
    def clock():
        return clk[0]
    q = BoundedFrameQueue(maxsize=2, policy="replace_oldest", clock=clock)
    q.put(_FakeFrame(0, 0.0))
    q.put(_FakeFrame(1, 0.0))
    clk[0] = 1.0
    # replace_oldest evicts frame 0 and appends frame 2
    assert q.put(_FakeFrame(2, 1.0))
    assert q.stats.replaced == 1
    out = q.drain()
    assert [f.frame_id for f in out] == [1, 2]


def test_queue_high_water_and_depth():
    q = BoundedFrameQueue(maxsize=4, policy="drop_newest",
                          clock=lambda: 0.0)
    for i in range(3):
        q.put(_FakeFrame(i, 0.0))
    q.get()
    snap = q.stats_snapshot()
    assert snap["high_water"] == 3
    assert snap["depth"] == 2


# --------- h264 verifier ---------
def test_start_codes_and_nal_split():
    # 2 NALs: SPS + PPS
    buf = b"\x00\x00\x00\x01\x67\x42\x00\x0a\xe9\x00\x00\x00\x01\x68\xce\x38\x80"
    scs = find_start_codes(buf)
    assert len(scs) == 2
    nals = split_nals(buf)
    assert len(nals) == 2
    assert nal_type(nals[0]) == 7   # SPS
    assert nal_type(nals[1]) == 8   # PPS


def test_parse_sps_main_profile():
    # Main profile, level 2.1: 67 4d 00 15 ...
    sps_nal = b"\x67\x4d\x00\x15\xa0\x36\x07\x94\x00\x00\x00\x04\x00\x00\x00\xc8"
    info = parse_sps(sps_nal)
    assert info.profile_idc == 0x4d   # 77 = Main
    assert profile_name(info.profile_idc) == "main"


def test_psnr_identical_is_inf():
    a = np.zeros((64, 64), dtype=np.uint8)
    assert psnr(a, a) == float("inf")


def test_psnr_different_signal():
    a = np.zeros((64, 64), dtype=np.uint8)
    b = np.full((64, 64), 255, dtype=np.uint8)
    assert psnr(a, b) == 0.0


def test_ssim_identical_is_one():
    a = (np.random.default_rng(0).integers(0, 256, (48, 48))).astype(np.uint8)
    assert ssim(a, a) > 0.99


# --------- backend construction (no full encoding; just verify_hardware) ---------
def test_b0_verify_hardware():
    from frame_pipeline.backends.base import BackendSettings
    from frame_pipeline.backends.b0_pyav import B0PyavNvenc
    s = BackendSettings(fps=30, bitrate=10_000_000, max_bitrate=10_000_000,
                        gop=60, idr_period=60)
    with B0PyavNvenc(s) as b:
        hw = b.verify_hardware()
        assert hw["hardware"] is True
        assert hw["codec_name"] == "h264_nvenc"
        sps = b.sps_pps()
        assert len(sps) > 0
        nals = split_nals(sps)
        types = [nal_type(n) for n in nals]
        assert 7 in types   # SPS
        assert 8 in types   # PPS


def test_b1_cpu_verify_hardware_and_encode_one():
    from frame_pipeline.backends.base import BackendSettings
    from frame_pipeline.backends.b1_pynv import B1PynvCpu
    s = BackendSettings(fps=30, bitrate=10_000_000, max_bitrate=10_000_000,
                        gop=60, idr_period=60)
    with B1PynvCpu(s) as b:
        hw = b.verify_hardware()
        assert hw["hardware"] is True
        assert hw["sps_profile_idc"] == 0x64   # High — disclosed mismatch
        # encode one frame (returns 0 queued AUs); flush produces the AU
        nv12 = np.zeros((1080 * 3 // 2, 1920), dtype=np.uint8)
        nv12[:1080, :] = 128
        cf = CapturedFrame(frame_id=0, capture_ts=time.time(),
                           width=1920, height=1080, pixel_format="NV12",
                           memory_domain=DOMAIN_HOST_NUMPY, frame_obj=nv12)
        aus = b.encode_frame(cf)
        flush_aus = b.flush()
        all_aus = aus + flush_aus
        assert len(all_aus) >= 1
        # find the AU containing SPS+PPS+IDR (PyNvVideoCodec emits one big AU)
        sample = max(all_aus, key=lambda a: a.encoded_size_bytes)
        assert sample.encoded_size_bytes > 0
        nals = split_nals(sample.data)
        types = [nal_type(n) for n in nals]
        assert 7 in types and 8 in types and 5 in types


def test_b1_gpu_verify_hardware_and_encode_one():
    from frame_pipeline.backends.base import BackendSettings
    from frame_pipeline.backends.b1_pynv import B1PynvGpu
    s = BackendSettings(fps=30, bitrate=10_000_000, max_bitrate=10_000_000,
                        gop=60, idr_period=60)
    with B1PynvGpu(s) as b:
        hw = b.verify_hardware()
        assert hw["hardware"] is True
        # provide a HOST nv12 frame; b1_gpu will do explicit H2D
        nv12 = np.zeros((1080 * 3 // 2, 1920), dtype=np.uint8)
        nv12[:1080, :] = 128
        # encode two frames then flush (PyNvVideoCodec queues; flush returns AUs)
        aus = []
        for i in range(2):
            cf = CapturedFrame(frame_id=i, capture_ts=time.time(),
                               width=1920, height=1080, pixel_format="NV12",
                               memory_domain=DOMAIN_HOST_NUMPY, frame_obj=nv12.copy())
            aus += b.encode_frame(cf)
        aus += b.flush()
        assert len(aus) >= 1
        # at least one encoded AU should carry the prep_summary showing the H2D copy
        with_h2d = [a for a in aus
                    if a.backend_metadata.get("prep_summary", {}).get("copies", {}).get("h2d") == 1]
        assert len(with_h2d) >= 1, "expected at least one AU with an explicit H2D copy"


def test_synthetic_source_determinism():
    """Same seed + index must produce byte-identical frames across two instances."""
    from frame_pipeline.sources.synthetic import SyntheticSource, SourceConfig
    s1 = SyntheticSource(SourceConfig(mode="motion", output_domain="host_nv12", seed=123))
    s2 = SyntheticSource(SourceConfig(mode="motion", output_domain="host_nv12", seed=123))
    f1 = s1.next_frame(7).frame_obj
    f2 = s2.next_frame(7).frame_obj
    assert np.array_equal(f1, f2)
