"""Unit tests for the H.264 Annex B verifier on synthetic bitstreams."""

from frame_pipeline.h264_verify import (
    FrameOrder, _parse_sps, split_annexb, verify_bitstream,
)


def _build_min_sps(width_mbs: int = 120, height_mapu: int = 68, crop_bottom: int = 0) -> bytes:
    """Hand-build a minimal Main-profile SPS RBSP (profile 77)."""

    bits = []

    def put(v, n):
        for i in range(n - 1, -1, -1):
            bits.append((v >> i) & 1)

    def ue(v):
        v += 1
        n = v.bit_length() - 1
        put(0, n)
        put(v, n + 1)

    def se(v):
        ue(-2 * v if v <= 0 else 2 * v - 1)

    # profile_idc(8)=77, constraint(8)=0, level(8)=40, sps_id ue(0)
    put(77, 8); put(0, 8); put(40, 8); ue(0)
    ue(0)                    # log2_max_frame_num_minus4
    ue(0)                    # pic_order_cnt_type
    ue(0)                    # log2_max_pic_order_cnt_lsb_minus4 (poc type 0)
    ue(1)                    # max_num_ref_frames
    put(0, 1)                # gaps_in_frame_num_value_allowed_flag
    ue(width_mbs - 1)        # pic_width_in_mbs_minus1
    ue(height_mapu - 1)      # pic_height_in_map_units_minus1
    put(1, 1)                # frame_mbs_only_flag
    put(0, 1)                # direct_8x8_inference_flag
    put(1 if crop_bottom else 0, 1)  # frame_cropping_flag
    if crop_bottom:
        ue(0); ue(0); ue(0); ue(crop_bottom)
    put(0, 1)                # vui_parameters_present_flag (0 for minimal)
    # rbsp_stop_one_bit
    bits.append(1)
    while len(bits) % 8 != 0:
        bits.append(0)
    out = bytearray()
    for i in range(0, len(bits), 8):
        b = 0
        for bit in bits[i:i + 8]:
            b = (b << 1) | bit
        out.append(b)
    return bytes(out)


def _annexb(nals: list[bytes]) -> bytes:
    out = b""
    for nal in nals:
        out += b"\x00\x00\x00\x01" + nal
    return out


def test_split_annexb_counts_nals():
    sps_payload = bytes([0x67]) + _build_min_sps()
    pps_payload = bytes([0x68, 0xCE, 0x3C, 0x80])
    idr_payload = bytes([0x65, 0x88, 0x80, 0x21])
    bs = _annexb([sps_payload, pps_payload, idr_payload])
    nals = split_annexb(bs)
    assert len(nals) == 3
    assert nals[0].nal_type == 7 and nals[1].nal_type == 8 and nals[2].nal_type == 5


def test_verify_bitstream_profile_and_resolution():
    # 68 map-units x16 = 1088 coded; crop_bottom=4 removes (4*2)=8 rows -> 1080 visible.
    sps_payload = bytes([0x67]) + _build_min_sps(120, 68, crop_bottom=4)
    pps_payload = bytes([0x68, 0xCE, 0x3C, 0x80])
    idr_payload = bytes([0x65, 0x88, 0x80, 0x21])
    bs = _annexb([sps_payload, pps_payload, idr_payload])
    res = verify_bitstream(bs, 1920, 1080)
    assert res.has_sps and res.has_pps
    assert res.profile == "Main", res.profile
    assert res.width == 1920 and res.height == 1080
    assert res.idr_count == 1
    assert res.first_nal_is_keyframe
    assert res.ok, res.errors


def test_frame_order_detects_regression():
    fo = FrameOrder()
    fo.check(100, 0)
    fo.check(200, 3000)
    fo.check(150, 6000)  # capture went backwards
    assert fo.out_of_order == 1
