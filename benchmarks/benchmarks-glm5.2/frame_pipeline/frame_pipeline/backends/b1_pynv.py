"""B1 — NVIDIA GPU-oriented encode path via PyNvVideoCodec 2.2.2.

Two variants share this module:
  - B1-CPU  : usecpuinputbuffer=True, host NumPy NV12 input.
              Mirrors a fair equivalent of B0 but using the official NVIDIA Python
              video-codec binding instead of PyAV.  Swscale is NOT used (caller feeds
              NV12 directly), so the RGBA->NV12 conversion is an EXPLICIT GPU/CPU step
              rather than an API-hidden one.
  - B1-GPU  : usecpuinputbuffer=False, device-memory NV12 input via an object exposing
              `.cuda()` returning a list of per-plane __cuda_array_interface__ dicts.
              Uses cuda-python (cuda.bindings.driver) for device allocation + H2D copy.
              This is the GPU-resident input path.  Crucially it still requires an
              explicit H2D copy *if* the source frame originated on the host; whether a
              live-Kit GPU texture can avoid that copy is a SEPARATE capability question
              answered by the live test (currently capability-GATED).

NVENC profile caveat (disclosed in the report):
  PyNvVideoCodec 2.2.2 exposes NO documented `profile` knob for H.264; the encoder
  emits High profile (SPS profile_idc=0x64).  This is a MATERIAL equivalence mismatch
  with B0 (Main) and is disclosed in §5 and §9 rather than hidden.

NVENC async caveat:
  GetEncoderCaps()['async_encode_support'] == 0 on this Blackwell config, so we do NOT
  claim async NVENC encode.  Encode() is synchronous from the caller's perspective;
  application queueing is measured separately upstream of this call.
"""
from __future__ import annotations

import time
from typing import List, Optional

from ..contracts import (CapturedFrame, EncodedAccessUnit,
                          DOMAIN_HOST_NUMPY, DOMAIN_GPU_DEVICE, DOMAIN_API_HIDDEN)
from .base import BackendSettings, EncoderBackend, BackendUnavailable


class _B1Base(EncoderBackend):
    name = "b1_pynv_base"
    accept_pixel_format = "NV12"

    def __init__(self, settings: BackendSettings, use_cpu_input: bool):
        super().__init__(settings)
        import PyNvVideoCodec as nvc
        self._nvc = nvc
        self._use_cpu_input = use_cpu_input
        self.accept_memory_domain = DOMAIN_HOST_NUMPY if use_cpu_input else DOMAIN_GPU_DEVICE
        caps = nvc.GetEncoderCaps()
        # async_encode_support is the actual NVENC async capability flag.
        self._async_supported = bool(caps.get("async_encode_support", 0)) if isinstance(caps, dict) else False
        s = self.settings
        # CreateEncoder(width, height, fmt, usecpuinputbuffer, **kwargs) — cudacontext/cudastream
        # live INSIDE the kwargs dict (CreateEncoder pulls them out itself), so we must NOT also
        # pass them as explicit keyword arguments.
        kw = dict(
            codec="h264", preset=s.preset, tuning_info=s.tuning,
            rc=s.rate_control, bitrate=s.bitrate, maxbitrate=s.max_bitrate,
            vbvbufsize=max(s.bitrate, s.max_bitrate),
            gop=s.gop, idrperiod=s.idr_period,
            bf=s.bf, lookahead=s.lookahead, aq=s.aq, temporalaq=s.temporalaq,
            repeatspspps=s.repeatspspps,
        )
        if not use_cpu_input:
            # allocate a CUDA context + stream via cuda-python
            from cuda import cuda
            self._cuda = cuda
            cuda.cuInit(0)
            err, dev = cuda.cuDeviceGet(s.gpu_id)
            err, ctx = cuda.cuCtxCreate(0, dev)
            err, stream = cuda.cuStreamCreate(0)
            self._cu_ctx = int(ctx)
            self._cu_stream = int(stream)
            kw["cudacontext"] = self._cu_ctx
            kw["cudastream"] = self._cu_stream
        else:
            self._cuda = None
            self._cu_ctx = 0
            self._cu_stream = 0
        try:
            self._enc = nvc.CreateEncoder(s.width, s.height, "NV12",
                                           bool(use_cpu_input), **kw)
        except Exception as e:
            raise BackendUnavailable("PyNvVideoCodec CreateEncoder failed: %s" % e) from e
        self._pending_prep = None  # carried to the next flushed AU for H2D accounting

    # ------------------------------------------------------------------
    def verify_hardware(self) -> dict:
        # PyNvVideoCodec raises on init if NVENC is unavailable; reaching here means the
        # hardware encoder was instantiated.  SPS profile_idc byte is independent proof.
        sps = self._enc.GetSequenceParams()
        profile_idc = sps[5] if len(sps) > 5 else 0
        return {"hardware": True,
                "evidence": "PyNvVideoCodec.PyNvEncoder instantiated without exception; "
                            "GetSequenceParams() valid; SPS profile_idc=0x%02x" % profile_idc,
                "sps_len": len(sps),
                "sps_profile_idc": profile_idc,
                "async_encode_supported": self._async_supported}

    def sps_pps(self) -> bytes:
        return self._enc.GetSequenceParams()

    def _profile_label(self) -> str:
        sps = self._enc.GetSequenceParams()
        pid = sps[5] if len(sps) > 5 else 0
        return {0x42: "baseline", 0x4d: "main", 0x64: "high"}.get(pid, "unknown(0x%02x)" % pid)

    def _aus_from_packets(self, packets, cf: Optional[CapturedFrame],
                          submit_ts: float, completion_ts: float) -> List[EncodedAccessUnit]:
        from ..h264_verify import split_nals, nal_type
        out = []
        for p in packets:
            data = bytes(p["data"])
            ptype = p.get("picture_type", "")
            # NVENC picture_type is a numeric string; the reliable IDR signal is the
            # presence of an IDR NAL (type 5) in the AU.  Scan NALs directly.
            is_idr = False
            try:
                nals = split_nals(data)
                if any(nal_type(n) == 5 for n in nals):
                    is_idr = True
            except Exception:
                pass
            ts = p.get("timestamp")
            au = EncodedAccessUnit(
                frame_id=cf.frame_id if cf is not None else -1,
                source_capture_ts=cf.capture_ts if cf is not None else 0.0,
                encode_submit_ts=submit_ts,
                encode_completion_ts=completion_ts,
                pts=int(ts) if ts is not None else None,
                dts=int(ts) if ts is not None else None,
                codec="h264",
                profile=self._profile_label(),
                is_keyframe=is_idr,
                encoded_size_bytes=len(data),
                backend=self.name,
                backend_metadata={
                    "pynv_picture_type": str(ptype),
                    "input_memory": self.accept_memory_domain,
                    "async_encode_supported": self._async_supported,
                    "profile_note": "PyNvVideoCodec 2.2.2 has no profile knob; High emitted",
                },
                data=data,
            )
            out.append(au)
        return out

    def encode_frame(self, cf: CapturedFrame) -> List[EncodedAccessUnit]:
        frame_obj, prep = self.prepare(cf)
        submit_ts = time.perf_counter()
        packets = self._enc.Encode(frame_obj)
        completion_ts = time.perf_counter()
        aus = self._aus_from_packets(packets, cf, submit_ts, completion_ts)
        # PyNvVideoCodec pipelines: Encode() may return 0 queued AUs and only emit them
        # at EndEncode(); we still want the prep_summary (incl. H2D copy count) to land on
        # the AU that ultimately corresponds to this input frame.  Stash it on the backend
        # and stamp it onto the next AU produced by either Encode() or flush().
        if aus:
            aus[0].backend_metadata["prep_summary"] = prep
        else:
            self._pending_prep = prep
        return aus

    def flush(self) -> List[EncodedAccessUnit]:
        submit_ts = time.perf_counter()
        packets = self._enc.EndEncode()
        completion_ts = time.perf_counter()
        aus = self._aus_from_packets(packets, None, submit_ts, completion_ts)
        # stamp any pending prep summary onto the first flushed AU so the H2D copy
        # accounting is preserved across the pipeline.
        if aus and getattr(self, "_pending_prep", None) is not None:
            aus[0].backend_metadata["prep_summary"] = self._pending_prep
            self._pending_prep = None
        return aus

    def close(self):
        if not self._closed:
            # NOTE: do NOT call cuCtxDestroy here — the encoder may hold internal refs and
            # double-free has been observed.  Process teardown is the safe cleanup for now;
            # the harness wraps each run in a subprocess so this is bounded.
            try:
                del self._enc
            except Exception:
                pass
        super().close()


class B1PynvCpu(_B1Base):
    name = "b1_pynv_cpu"

    def __init__(self, settings: BackendSettings):
        super().__init__(settings, use_cpu_input=True)

    def prepare(self, cf: CapturedFrame) -> tuple[object, dict]:
        """cf.frame_obj is a host numpy NV12 buffer (H+W planes concatenated).

        No prep copy needed — we hand the numpy directly to Encode().  Any required
        NVENC H2D upload is API-hidden inside Encode() (and disclosed in metadata).
        """
        t0 = time.perf_counter()
        nv12 = cf.frame_obj  # (3H/2, W) uint8 host
        t1 = time.perf_counter()
        return nv12, {
            "capture_cb_duration_s": 0.0,
            "color_convert_duration_s": 0.0,  # conversion done by source, timed there
            "frame_prep_duration_s": t1 - t0,
            "copies": {"d2d": 0, "d2h": 0, "h2d": 0},  # H2D upload is API-hidden in Encode()
            "memory_domain_in": cf.memory_domain,
            "memory_domain_out": DOMAIN_HOST_NUMPY,
            "note": "NVENC H2D upload is API-hidden inside PyNvVideoCodec.Encode()",
        }


class _CaiPlane:
    """A single plane exposing __cuda_array_interface__ v3, backed by a device pointer."""
    __slots__ = ("_ptr", "_shape", "_strides", "_typestr")

    def __init__(self, ptr, shape, strides, typestr):
        self._ptr = ptr
        self._shape = shape
        self._strides = strides
        self._typestr = typestr

    @property
    def __cuda_array_interface__(self):
        return {"shape": tuple(int(x) for x in self._shape),
                "strides": tuple(int(x) for x in self._strides),
                "data": (int(self._ptr), False),
                "typestr": self._typestr, "version": 3}


class _GpuNv12Frame:
    """Object fed to PyNvVideoCodec.Encode() in GPU-input mode.  `.cuda()` returns the
    list of per-plane CAI dicts: [luma (H,W,1) stride (W,1,1), chroma (H/2,W/2,2) stride (W,2,1)].
    """
    # No __slots__: we attach `_owned_dptr` to keep the device buffer alive across the
    # synchronous Encode() call (the source's ring slot is reused for the next frame).
    def __init__(self, luma_ptr, chroma_ptr, w, h):
        self._luma_ptr = luma_ptr
        self._chroma_ptr = chroma_ptr
        self._w = w
        self._h = h

    def cuda(self):
        luma = _CaiPlane(self._luma_ptr, (self._h, self._w, 1), (self._w, 1, 1), "|u1")
        chroma = _CaiPlane(self._chroma_ptr, (self._h // 2, self._w // 2, 2), (self._w, 2, 1), "|u1")
        return [luma, chroma]


class B1PynvGpu(_B1Base):
    name = "b1_pynv_gpu"

    def __init__(self, settings: BackendSettings):
        super().__init__(settings, use_cpu_input=False)

    def prepare(self, cf: CapturedFrame) -> tuple[object, dict]:
        """cf.frame_obj is expected to be an already-allocated device NV12 buffer handed
        in by the source (cf.memory_domain == gpu_device).

        If the source provided host NV12 (memory_domain == host_numpy), we perform an
        EXPLICIT H2D copy here (counted) — this is the honest representation of the GPU
        path when no proven GPU->GPU handoff exists.
        """
        t0 = time.perf_counter()
        cuda = self._cuda
        s = self.settings
        w, h = s.width, s.height
        luma_size = w * h
        chroma_size = w * (h // 2)  # interleaved UV pairs, W bytes/row, H/2 rows
        copies = {"d2d": 0, "d2h": 0, "h2d": 0}
        if cf.memory_domain == DOMAIN_GPU_DEVICE and isinstance(cf.frame_obj, _GpuNv12Frame):
            # already-resident device frame, NO copy — proven GPU-resident input.
            frame_obj = cf.frame_obj
            domain_out = DOMAIN_GPU_DEVICE
        else:
            # host NV12 numpy -> explicit H2D into a fresh device buffer
            host_nv12 = cf.frame_obj  # (3H/2, W) uint8
            err, dptr = cuda.cuMemAlloc(luma_size + chroma_size)
            err, = cuda.cuMemcpyHtoD(dptr, host_nv12.ctypes.data, luma_size + chroma_size)
            copies["h2d"] = 1
            frame_obj = _GpuNv12Frame(dptr, int(dptr) + luma_size, w, h)
            domain_out = DOMAIN_GPU_DEVICE
            # we cannot safely free dptr until Encode() consumes it; lifetime is bounded
            # by the synchronous Encode() call below — keep a reference on the frame obj.
            frame_obj._owned_dptr = dptr
        t1 = time.perf_counter()
        return frame_obj, {
            "capture_cb_duration_s": 0.0,
            "color_convert_duration_s": 0.0,
            "frame_prep_duration_s": t1 - t0,
            "copies": copies,
            "memory_domain_in": cf.memory_domain,
            "memory_domain_out": domain_out,
            "note": "explicit H2D when source is host; no copy when source is GPU-resident",
        }
