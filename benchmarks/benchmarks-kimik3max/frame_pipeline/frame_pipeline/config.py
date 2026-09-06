"""Machine-readable benchmark case configuration.

Kept deliberately small and serializable so a case can be reproduced from this
config plus the source. Common encoder settings target equivalence across
backends; per-backend mismatches are disclosed in the report.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class EncoderIntent:
    codec: str = "h264"
    profile: str = "main"
    preset_b1: str = "P1"          # PyNvVideoCodec preset naming
    preset_b0: str = "p1"          # ffmpeg h264_nvenc preset naming
    tuning_info_b1: str = "ultra_low_latency"
    tune_b0: str = "ll"            # ffmpeg low latency
    rc: str = "cbr"
    b_frames: int = 0
    lookahead: int = 0
    aq: str = "disabled"
    gop_seconds: float = 2.0       # reproducible GOP: 2*fps (documented in report)

    def common(self, fps: int) -> dict:
        return {
            "codec": self.codec,
            "profile": self.profile,
            "rc": self.rc,
            "gop": int(round(self.gop_seconds * fps)),
            "bitrate": "10M" if fps <= 30 else "20M",
            "b_frames": self.b_frames,
            "lookahead": self.lookahead,
            "aq": self.aq,
        }

    def for_b1(self, fps: int) -> dict:
        c = self.common(fps)
        c.update({"preset": self.preset_b1, "tuning_info": self.tuning_info_b1})
        return c

    def for_b0(self, fps: int) -> dict:
        c = self.common(fps)
        c.update({"preset": self.preset_b0, "tune": self.tune_b0})
        return c


@dataclass
class Case:
    name: str
    backend: str            # "b0" | "b1_host" | "b1_device"
    mode: str               # "static" | "motion"
    fps: int
    width: int = 1920
    height: int = 1080
    n_streams: int = 1
    independent_sources: bool = False
    warmup_s: float = 10.0
    measured_s: float = 20.0
    queue_capacity: int = 4
    queue_policy: str = "replace_oldest"
    encoder_intent: EncoderIntent = field(default_factory=EncoderIntent)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["gop"] = self.encoder_intent.common(self.fps)["gop"]
        d["bitrate"] = self.encoder_intent.common(self.fps)["bitrate"]
        return d


# POC case matrix (bounded; the full production matrix is out of scope).
# measured_s counts the steady-state producer-paced interval; warmup is separate.
POC_SINGLE = [
    Case(name="single_b0_static_30", backend="b0", mode="static", fps=30, measured_s=30.0, warmup_s=10.0),
    Case(name="single_b0_motion_30", backend="b0", mode="motion", fps=30, measured_s=30.0, warmup_s=10.0),
    Case(name="single_b0_static_60", backend="b0", mode="static", fps=60, measured_s=30.0, warmup_s=10.0),
    Case(name="single_b0_motion_60", backend="b0", mode="motion", fps=60, measured_s=30.0, warmup_s=10.0),
    Case(name="single_b1h_static_30", backend="b1_host", mode="static", fps=30, measured_s=30.0, warmup_s=10.0),
    Case(name="single_b1h_motion_30", backend="b1_host", mode="motion", fps=30, measured_s=30.0, warmup_s=10.0),
    Case(name="single_b1h_static_60", backend="b1_host", mode="static", fps=60, measured_s=30.0, warmup_s=10.0),
    Case(name="single_b1h_motion_60", backend="b1_host", mode="motion", fps=60, measured_s=30.0, warmup_s=10.0),
]

POC_MULTI = [
    Case(name="multi_b1h_fanout_2_1080p30", backend="b1_host", mode="motion", fps=30,
         n_streams=2, independent_sources=False, measured_s=30.0, warmup_s=10.0),
    Case(name="multi_b1h_fanout_4_1080p30", backend="b1_host", mode="motion", fps=30,
         n_streams=4, independent_sources=False, measured_s=30.0, warmup_s=10.0),
    Case(name="multi_b1h_indep_2_1080p30", backend="b1_host", mode="motion", fps=30,
         n_streams=2, independent_sources=True, measured_s=30.0, warmup_s=10.0),
    Case(name="multi_b1h_indep_1_1080p60", backend="b1_host", mode="motion", fps=60,
         n_streams=1, measured_s=30.0, warmup_s=10.0),
    Case(name="multi_b1h_indep_2_1080p60", backend="b1_host", mode="motion", fps=60,
         n_streams=2, independent_sources=True, measured_s=30.0, warmup_s=10.0),
]
