"""Unit tests for data contracts and the bounded queue policy."""

from frame_pipeline.contracts import (
    CapturedFrame, EncodedAccessUnit, MemoryDomain, PixelFormat,
)
from frame_pipeline.bounded_queue import BoundedFrameQueue


def test_encoded_latency():
    au = EncodedAccessUnit(
        backend="t", frame_id=1, capture_ts_ns=1000, encode_submit_ts_ns=1000,
        encode_complete_ts_ns=2500, pts=0, dts=0, codec="h264", profile="main",
        is_keyframe=True, size_bytes=10, bitstream=b"\x00",
    )
    assert au.capture_to_encode_ns == 1500
    rec = au.to_record()
    assert "bitstream" not in rec
    assert rec["capture_to_encode_ns"] == 1500


def test_bounded_queue_replace_oldest():
    q = BoundedFrameQueue(capacity=2, policy="replace_oldest")
    assert q.put(1)
    assert q.put(2)
    assert q.put(3)  # full -> evict oldest (1)
    assert q.stats.replacements == 1
    assert q.get(timeout=0) == 2
    assert q.get(timeout=0) == 3
    assert q.get(timeout=0) is None
    q.close()
    assert not q.put(9)  # closed


def test_bounded_queue_drop_new():
    q = BoundedFrameQueue(capacity=1, policy="drop_new")
    assert q.put(1)
    assert not q.put(2)  # full, drop new
    assert q.stats.dropped == 1
    assert q.get(timeout=0) == 1
    q.close()


def test_queue_is_bounded():
    q = BoundedFrameQueue(capacity=3, policy="replace_oldest")
    for i in range(20):
        q.put(i)
    assert q.depth() <= 3
    assert q.stats.high_water == 3
    q.drain()
    assert q.depth() == 0
    q.close()
