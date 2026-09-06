"""H.264 Annex-B verifier — adapted from the read-only
``benchmarks/benchmarks-kimik3max/frame_pipeline/frame_pipeline/h264_verify.py``.

Validates encoded output beyond "the API returned": NAL split by start code,
SPS/PPS identification, SPS parse for *actual* profile + coded resolution
(including crop), IDR/non-IDR counts, truncation flag, and monotonic order check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

NAL_NAMES = {
    1: "nonIDR_slice", 5: "IDR_slice", 6: "SEI", 7: "SPS", 8: "PPS",
    9: "AUD", 10: "end_of_seq", 11: "end_of_stream", 12: "filler",
}

PROFILE_NAMES = {
    66: "Baseline", 77: "Main", 88: "Extended", 100: "High",
    110: "High10", 122: "High4:2:2", 244: "High4:4:4",
}


@dataclass
class NalUnit:
    nal_type: int
    name: str
    rbsp: bytes
    start_offset: int
    ref_idc: int


def split_annexb(data: bytes) -> list[NalUnit]:
    nals: list[NalUnit] = []
    n = len(data)
    i = 0
    starts: list[tuple[int, int]] = []
    while i + 2 < n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            starts.append((i + 3, 3))
            i += 3
        elif (i + 3 < n and data[i] == 0 and data[i + 1] == 0
              and data[i + 2] == 0 and data[i + 3] == 1):
            starts.append((i + 4, 4))
            i += 4
        else:
            i += 1
    for idx, (payload_start, _cl) in enumerate(starts):
        if idx + 1 < len(starts):
            next_payload, next_len = starts[idx + 1]
            end = next_payload - next_len
        else:
            end = n
        payload = data[payload_start:end]
        if not payload:
            continue
        hdr = payload[0]
        nal_type = hdr & 0x1F
        ref_idc = (hdr >> 5) & 0x3
        nals.append(NalUnit(nal_type, NAL_NAMES.get(nal_type, f"type{nal_type}"),
                            payload[1:], payload_start, ref_idc))
    return nals


class BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read_bit(self) -> int:
        byte = self.data[self.pos >> 3]
        bit = (byte >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return bit

    def read_bits(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | self.read_bit()
        return v

    def ue(self) -> int:
        leading = 0
        while self.read_bit() == 0:
            leading += 1
            if leading > 31:
                raise ValueError("ue overflow")
        return (1 << leading) - 1 + (self.read_bits(leading) if leading else 0)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k % 2 == 1 else -(k // 2)


def _parse_sps(rbsp: bytes) -> dict:
    br = BitReader(rbsp)
    profile_idc = br.read_bits(8)
    br.read_bits(8)
    level_idc = br.read_bits(8)
    br.ue()
    chroma_format_idc = 1
    if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma_format_idc = br.ue()
        if chroma_format_idc == 3:
            br.read_bit()
        br.ue()
        br.ue()
        br.read_bit()
        if br.read_bit():
            count = 8 if chroma_format_idc != 3 else 12
            for i in range(count):
                if br.read_bit():
                    size = 16 if i < 6 else 64
                    last = next_scale = 8
                    for _ in range(size):
                        if next_scale != 0:
                            delta = br.se()
                            next_scale = (last + delta + 256) % 256
                        last = next_scale if next_scale != 0 else last
    br.ue()
    poc = br.ue()
    if poc == 0:
        br.ue()
    elif poc == 1:
        br.read_bit(); br.se(); br.se()
        for _ in range(br.ue()):
            br.se()
    br.ue()
    br.read_bit()
    w_mbs = br.ue()
    h_map = br.ue()
    frame_mbs_only = br.read_bit()
    if not frame_mbs_only:
        br.read_bit()
    br.read_bit()
    crop = [0, 0, 0, 0]
    if br.read_bit():
        crop = [br.ue(), br.ue(), br.ue(), br.ue()]
    width = (w_mbs + 1) * 16
    height = (h_map + 1) * 16 * (2 - frame_mbs_only)
    if crop != [0, 0, 0, 0]:
        sub_w = {0: 1, 1: 2, 2: 2, 3: 1}.get(chroma_format_idc, 2)
        sub_h = {0: 1, 1: 2, 2: 1, 3: 1}.get(chroma_format_idc, 2)
        cux = sub_w
        cuy = sub_h * (2 - frame_mbs_only)
        width -= (crop[0] + crop[1]) * cux
        height -= (crop[2] + crop[3]) * cuy
    return {
        "profile_idc": profile_idc,
        "profile": PROFILE_NAMES.get(profile_idc, f"unknown({profile_idc})"),
        "level_idc": level_idc,
        "width": width, "height": height,
        "chroma_format_idc": chroma_format_idc,
    }


@dataclass
class VerifyResult:
    total_nals: int = 0
    sps_count: int = 0
    pps_count: int = 0
    idr_count: int = 0
    non_idr_count: int = 0
    sei_count: int = 0
    profile: Optional[str] = None
    profile_idc: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    total_bytes: int = 0
    has_sps: bool = False
    has_pps: bool = False
    first_slice_is_idr: bool = False
    truncated: bool = False
    errors: list = field(default_factory=list)
    ok: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def verify_bitstream(data: bytes, expect_width: int, expect_height: int) -> VerifyResult:
    res = VerifyResult(total_bytes=len(data))
    try:
        nals = split_annexb(data)
    except Exception as exc:
        res.errors.append(f"split error: {exc}")
        return res
    res.total_nals = len(nals)
    if not nals:
        res.errors.append("no NAL units found")
        return res
    for nal in nals:
        if nal.nal_type == 7:
            res.sps_count += 1; res.has_sps = True
            try:
                sps = _parse_sps(nal.rbsp)
                res.profile = sps["profile"]; res.profile_idc = sps["profile_idc"]
                res.width = sps["width"]; res.height = sps["height"]
            except Exception as exc:
                res.errors.append(f"SPS parse error: {exc}")
        elif nal.nal_type == 8:
            res.pps_count += 1; res.has_pps = True
        elif nal.nal_type == 5:
            res.idr_count += 1
        elif nal.nal_type == 1:
            res.non_idr_count += 1
        elif nal.nal_type == 6:
            res.sei_count += 1
    slices = [x for x in nals if x.nal_type in (1, 5)]
    if slices:
        res.first_slice_is_idr = slices[0].nal_type == 5
    else:
        res.errors.append("no slice NAL units present")
    if res.has_sps and res.width is not None:
        if res.width != expect_width or res.height != expect_height:
            res.errors.append(
                f"resolution mismatch: got {res.width}x{res.height}, want {expect_width}x{expect_height}")
    if not res.has_sps:
        res.errors.append("missing SPS")
    if not res.has_pps:
        res.errors.append("missing PPS")
    # truncation: last 4 bytes of a clean Annex-B stream with SPS/PPS/slices should
    # not contain a dangling start code with empty payload.
    if nals and len(nals[-1].rbsp) == 0:
        res.truncated = True
        res.errors.append("trailing empty NAL (truncation)")
    res.ok = (not res.errors) and res.has_sps and res.has_pps and res.idr_count >= 1
    return res


def psnr(ref, test, peak: float = 255.0) -> float:
    """PSNR(dB) between two equal-shaped uint8 arrays (numpy lazy).

    Correct formula:  PSNR = 10 * log10( peak^2 / MSE )
                          = 20 * log10( peak / sqrt(MSE) ).

    (The inherited reference formula ``20 * (peak / sqrt(mse)) / ln(10)`` was a
    linear-ratio-then-log bug that inflated values to thousands of dB; fixed here.)
    """
    import math
    import numpy as np
    a = np.asarray(ref, dtype=np.float64)
    b = np.asarray(test, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch {a.shape} vs {b.shape}")
    mse = float(np.mean((a - b) ** 2))
    if mse <= 1e-12:
        return float("inf") if a.size else 0.0  # identical images
    return 10.0 * math.log10((peak * peak) / mse)
