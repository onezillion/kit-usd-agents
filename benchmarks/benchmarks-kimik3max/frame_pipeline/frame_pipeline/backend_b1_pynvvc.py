"""B1 backend: NVIDIA PyNvVideoCodec (PyNvEncoder) -> hardware NVENC.

The "GPU-oriented" candidate. Frame preparation (RGBA->NV12 conversion) is
split from the actual hardware encode so the harness can benchmark each stage
independently and run them on different threads (the encode submit is only
~1.3 ms at 1080p30 while the CPU NV12 conversion is ~49 ms single-threaded).

Config keys actually honoured by PyNvVideoCodec 2.2.2 (verified empirically):
``preset``, ``tuning_info``, ``gop``, ``codec``, ``averageBitrate``. Keys
``fps`` / ``bf`` / ``profile`` / ``rc`` are silently ignored by this version
and are therefore *not* sent; the consequences are disclosed in the report
(GOP is configurable; profile defaults to High(100) because no profile key is
accepted by 2.2.2).

A software fallback is a benchmark failure: ``caps().hardware_nvenc`` is True
only after a real NVENC session is constructed, and the harness cross-checks
NVENC engine utilization.
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np

from .backend import EncoderBackend
from .contracts import (
    BackendCaps, CapturedFrame, EncodedAccessUnit, MemoryDomain,
    PixelFormat, TransferClass, now_ns,
)
from .conversion import BgraToNv12Gpu, rgba_to_nv12_cpu


class BackendB1(EncoderBackend):
    name = "b1_pynvvideocodec"

    def __init__(self, device_index: int = 0, input_mode: str = "host") -> None:
        if input_mode not in ("host", "device"):
            raise ValueError("input_mode must be 'host' or 'device'")
        self.device_index = device_index
        self.input_mode = input_mode
        self.width = self.height = self.fps = 0
        self.settings: dict = {}
        self._enc = None
        self._converter: Optional[BgraToNv12Gpu] = None
        self._transfer_log: list = []
        self._opened_ok = False
        self.hardware_proof = ""
        self.last_submit_ns = 0

    def configure(self, width: int, height: int, fps: int, settings: dict) -> None:
        self.width, self.height, self.fps = width, height, fps
        self.settings = dict(settings)

    def open(self) -> None:
        import PyNvVideoCodec as nvc  # type: ignore

        gop = int(self.settings.get("gop", self.fps * 2))
        # 2.2.2 honours only these keys; fps/bf/profile/rc are silently ignored.
        opts = {
            "preset": str(self.settings.get("preset", "P1")),
            "tuning_info": str(self.settings.get("tuning_info", "ultra_low_latency")),
            "codec": "h264",
            "gop": str(gop),
            "averageBitrate": str(self._bitrate_int(self.settings.get("bitrate", "10M"))),
        }
        use_cpu = self.input_mode == "host"
        try:
            self._enc = nvc.CreateEncoder(self.width, self.height, "NV12", use_cpu, **opts)
        except Exception as exc:
            raise RuntimeError(f"B1 NVENC encoder construction failed: {exc}") from exc
        self._opened_ok = True
        caps = nvc.GetEncoderCaps(self.device_index, "h264")
        self.hardware_proof = (
            f"PyNvVideoCodec supportedNvEncVersion={nvc.supportedNvEncVersion}; "
            f"NVENC session constructed (num_encoder_engines={caps.get('num_encoder_engines')}); "
            "engine utilization cross-checked by harness"
        )
        if self.input_mode == "device":
            self._converter = BgraToNv12Gpu(device_index=self.device_index)

    @staticmethod
    def _bitrate_int(b: Any) -> int:
        if isinstance(b, (int, float)):
            return int(b)
        s = str(b).strip().lower()
        mult = 1
        if s.endswith("k"):
            mult, s = 1000, s[:-1]
        elif s.endswith("m"):
            mult, s = 1000000, s[:-1]
        return int(float(s) * mult)

    def _log(self, cls: TransferClass, detail: str) -> None:
        self._transfer_log.append({"class": cls.value, "detail": detail})

    # -- preparation (separable stage) --------------------------------------
    def prepare(self, frames: list[CapturedFrame]) -> list[Any]:
        """Convert RGBA frames to ready-to-encode inputs. May be run off-thread."""

        if self.input_mode == "device" and self._converter is not None:
            self._log(TransferClass.KNOWN_GPU_LOCAL, "RGBA->NV12 on GPU (+H2D if host src)")
            out = []
            for f in frames:
                if f.memory_domain == MemoryDomain.CUDA and getattr(f, "cuda_dev_ptr", None):
                    ptr = self._converter.convert_device_rgba(
                        f.cuda_dev_ptr, f.width, f.height, f.pitch_bytes or f.width * 4)
                    self._converter.synchronize()
                    out.append(("dev", ptr))
                else:
                    ptr = self._converter.convert_host_rgba(np.asarray(f.buffer))
                    self._converter.synchronize()
                    out.append(("dev", ptr))
            return out
        # host path: explicit KNOWN_CPU conversion
        self._log(TransferClass.KNOWN_CPU, "RGBA->NV12 CPU conversion")
        return [("host", rgba_to_nv12_cpu(np.asarray(f.buffer))) for f in frames]

    # -- encode (raw prepared input) ----------------------------------------
    def encode_raw(self, raw: Any, frame: CapturedFrame) -> list[EncodedAccessUnit]:
        if not self._opened_ok:
            raise RuntimeError("backend not opened")
        t0 = now_ns()
        kind, payload = raw
        if kind == "host":
            pkt = self._enc.Encode(payload)
        else:
            pkt = self._enc.Encode(self._dev_frame(payload))
        self.last_submit_ns = now_ns() - t0
        return self._wrap(pkt, frame)

    def encode(self, frame: CapturedFrame) -> list[EncodedAccessUnit]:
        return self.encode_raw(self.prepare([frame])[0], frame)

    def _dev_frame(self, dev_ptr: int):
        class _Dev:
            pass
        _Dev.__cuda_array_interface__ = {
            "shape": (self.height * 3 // 2, self.width),
            "typestr": "|u1", "data": (int(dev_ptr), False), "version": 3, "strides": None,
        }
        return _Dev()

    def flush(self) -> list[EncodedAccessUnit]:
        if self._enc is None:
            return []
        pkt = self._enc.EndEncode() or []
        return [self._packet_to_au(p, -1, 0) for p in pkt]

    def _wrap(self, pkt: list, frame: CapturedFrame) -> list[EncodedAccessUnit]:
        return [self._packet_to_au(p, frame.frame_id, frame.capture_ts_ns) for p in pkt]

    def _packet_to_au(self, p: Any, frame_id: int, capture_ts_ns: int) -> EncodedAccessUnit:
        if isinstance(p, dict):
            data = bytes(p.get("data", b""))
            ts = p.get("timestamp")
            pict = p.get("picture_type", -1)
        else:
            data = bytes(p)
            ts = None
            pict = -1
        return EncodedAccessUnit(
            backend=self.name, frame_id=frame_id, capture_ts_ns=capture_ts_ns,
            encode_submit_ts_ns=now_ns(), encode_complete_ts_ns=now_ns(),
            pts=ts, dts=ts, codec="h264",
            profile=self.settings.get("profile", "auto"),
            is_keyframe=(pict == 0), size_bytes=len(data), bitstream=data,
        )

    def caps(self) -> BackendCaps:
        version = "unknown"
        try:
            import PyNvVideoCodec as nvc  # type: ignore
            version = str(getattr(nvc, "supportedNvEncVersion", "2.x"))
        except Exception:
            pass
        return BackendCaps(
            name=self.name, version=f"pynvvideocodec nvenc {version}",
            hardware_nvenc=bool(self._opened_ok), hardware_proof=self.hardware_proof,
            input_formats=(PixelFormat.NV12,) if self.input_mode == "host" else (PixelFormat.RGBA, PixelFormat.NV12),
            gpu_direct_input=(self.input_mode == "device"),
            notes=("host NV12 input; 'profile' key not honoured by 2.2.2 -> defaults to High(100)" if self.input_mode == "host"
                   else "device input via GPU NV12 converter"),
        )

    def close(self) -> None:
        self._enc = None
        if self._converter is not None:
            self._converter.free()
            self._converter = None
        self._opened_ok = False
