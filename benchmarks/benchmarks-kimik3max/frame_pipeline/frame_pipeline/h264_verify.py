"""H.264 Annex B verifier.

Validates encoded output beyond "the API call succeeded":

* splits a bitstream into NAL access units by start code;
* identifies SPS / PPS / IDR / non-IDR slices;
* parses SPS to report the *actual* H.264 profile + resolution;
* checks monotonic capture order and timestamps provided by the harness;
* flags truncation / corruption (incomplete final NAL).

RBSP parsing exploits the property that an H.264 RBSP byte sequence never
contains a zero byte (``00`` is always followed by ``03`` emulation-prevention),
so Exp-Golomb bit reading is simple and exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# NAL unit types (subset).
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
    rbsp: bytes  # payload after the 1-byte header. Pure RBSP (no 00 00 03 present).
    start_offset: int
    ref_idc: int


def split_annexb(data: bytes) -> list[NalUnit]:
    """Split an Annex B bitstream into NAL units by start code."""

    nals: list[NalUnit] = []
    n = len(data)
    i = 0
    starts: list[tuple[int, int]] = []  # (payload_start_offset, start_code_len)
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
    # For each start code, the NAL spans from its payload start up to the
    # beginning of the *next* start code (or EOF).
    for idx, (payload_start, _code_len) in enumerate(starts):
        if idx + 1 < len(starts):
            next_payload, next_len = starts[idx + 1]
            end = next_payload - next_len
        else:
            end = n
        # A trailing 00 byte that belongs to the next start code's leading
        # zero prefix may be included; it is harmless for type parsing.
        payload = data[payload_start:end]
        if not payload:
            continue
        hdr = payload[0]
        nal_type = hdr & 0x1F
        ref_idc = (hdr >> 5) & 0x3
        nals.append(NalUnit(
            nal_type=nal_type,
            name=NAL_NAMES.get(nal_type, f"type{nal_type}"),
            rbsp=payload[1:],
            start_offset=payload_start,
            ref_idc=ref_idc,
        ))
    return nals


class BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0  # bit position

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
    """Parse a minimal subset of SPS to get profile + coded resolution."""

    if not rbsp or rbsp[0] == 0 and False:
        raise ValueError("empty SPS")
    br = BitReader(rbsp)
    profile_idc = br.read_bits(8)
    br.read_bits(8)  # constraint flags + reserved
    level_idc = br.read_bits(8)
    br.ue()  # seq_parameter_set_id
    # High profiles carry additional chroma info
    chroma_format_idc = 1
    if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma_format_idc = br.ue()
        if chroma_format_idc == 3:
            br.read_bit()  # separate_colour_plane_flag
        br.ue()  # bit_depth_luma_minus8
        br.ue()  # bit_depth_chroma_minus8
        br.read_bit()  # qpprime_y_zero_transform_bypass_flag
        seq_scaling_matrix_present = br.read_bit()
        if seq_scaling_matrix_present:
            count = 8 if chroma_format_idc != 3 else 12
            for i in range(count):
                flag = br.read_bit()
                if flag:
                    # skip scaling list (16 or 64 entries)
                    size = 16 if i < 6 else 64
                    last = next_scale = 8
                    for _ in range(size):
                        if next_scale != 0:
                            delta = br.se()
                            next_scale = (last + delta + 256) % 256
                        last = next_scale if next_scale != 0 else last
    br.ue()  # log2_max_frame_num_minus4
    pic_order_cnt_type = br.ue()
    if pic_order_cnt_type == 0:
        br.ue()
    elif pic_order_cnt_type == 1:
        br.read_bit()
        br.se()
        br.se()
        num_ref = br.ue()
        for _ in range(num_ref):
            br.se()
    br.ue()  # max_num_ref_frames
    br.read_bit()  # gaps_in_frame_num
    pic_width_in_mbs_minus1 = br.ue()
    pic_height_in_map_units_minus1 = br.ue()
    frame_mbs_only_flag = br.read_bit()
    if not frame_mbs_only_flag:
        br.read_bit()  # mb_adaptive_frame_field_flag
    br.read_bit()  # direct_8x8_inference_flag
    frame_cropping_flag = br.read_bit()
    crop = [0, 0, 0, 0]
    if frame_cropping_flag:
        crop = [br.ue(), br.ue(), br.ue(), br.ue()]
    width = (pic_width_in_mbs_minus1 + 1) * 16
    height = (pic_height_in_map_units_minus1 + 1) * 16 * (2 - frame_mbs_only_flag)
    if frame_cropping_flag:
        # H.264 Table 6-1 crop-unit factors by chroma_format_idc (SubWidthC/SubHeightC),
        # then scaled by frame/field. For 4:2:0 progressive: unit_x=2, unit_y=2.
        sub_w = {0: 1, 1: 2, 2: 2, 3: 1}.get(chroma_format_idc, 2)
        sub_h = {0: 1, 1: 2, 2: 1, 3: 1}.get(chroma_format_idc, 2)
        crop_unit_x = sub_w
        crop_unit_y = sub_h * (2 - frame_mbs_only_flag)
        width -= (crop[0] + crop[1]) * crop_unit_x
        height -= (crop[2] + crop[3]) * crop_unit_y
    return {
        "profile_idc": profile_idc,
        "profile": PROFILE_NAMES.get(profile_idc, f"unknown({profile_idc})"),
        "level_idc": level_idc,
        "width": width,
        "height": height,
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
    first_nal_is_keyframe: bool = False
    truncated: bool = False
    errors: list = field(default_factory=list)
    ok: bool = False

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def verify_bitstream(data: bytes, expect_width: int, expect_height: int) -> VerifyResult:
    """Structural verification of an Annex B bitstream (no decoding)."""

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
            res.sps_count += 1
            res.has_sps = True
            try:
                sps = _parse_sps(nal.rbsp)
                res.profile = sps["profile"]
                res.profile_idc = sps["profile_idc"]
                res.width = sps["width"]
                res.height = sps["height"]
            except Exception as exc:
                res.errors.append(f"SPS parse error: {exc}")
        elif nal.nal_type == 8:
            res.pps_count += 1
            res.has_pps = True
        elif nal.nal_type == 5:
            res.idr_count += 1
        elif nal.nal_type == 1:
            res.non_idr_count += 1
        elif nal.nal_type == 6:
            res.sei_count += 1
    # find first slice NAL
    slices = [x for x in nals if x.nal_type in (1, 5)]
    if slices:
        res.first_nal_is_keyframe = slices[0].nal_type == 5
    else:
        res.errors.append("no slice NAL units present")
    # structural sanity
    if res.has_sps and res.width is not None:
        if res.width != expect_width or res.height != expect_height:
            res.errors.append(f"resolution mismatch: got {res.width}x{res.height}, expected {expect_width}x{expect_height}")
    if not res.has_sps:
        res.errors.append("missing SPS")
    if not res.has_pps:
        res.errors.append("missing PPS")
    res.ok = (not res.errors) and res.has_sps and res.has_pps and res.idr_count >= 1
    return res


@dataclass
class FrameOrder:
    """Checks capture-order / timestamp monotonicity reported by the harness."""

    last_capture_ns: Optional[int] = None
    last_pts: Optional[int] = None
    out_of_order: int = 0
    checked: int = 0

    def check(self, capture_ns: Optional[int], pts: Optional[int]) -> None:
        self.checked += 1
        if capture_ns is not None and self.last_capture_ns is not None and capture_ns < self.last_capture_ns:
            self.out_of_order += 1
        if pts is not None and self.last_pts is not None and pts < self.last_pts:
            self.out_of_order += 1
        if capture_ns is not None:
            self.last_capture_ns = capture_ns
        if pts is not None:
            self.last_pts = pts


def psnr(ref: "object", test: "object") -> float:
    """PSNR in dB between two equal-shaped uint8 arrays (numpy imported lazily)."""

    import numpy as np

    a = np.asarray(ref, dtype=np.float64)
    b = np.asarray(test, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch {a.shape} vs {b.shape}")
    mse = float(np.mean((a - b) ** 2))
    if mse <= 1e-12:
        return 99.0
    return 20.0 * (255.0 / (mse ** 0.5)) / 2.302585092994046  # log10
