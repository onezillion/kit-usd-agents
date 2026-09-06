"""Environment / capability probe for the KHL Frame Pipeline Benchmark.

Captures authoritative provenance: Kit version, GPU, driver, CUDA, Python, PyAV, NVENC,
PyNvVideoCodec, and the live capability facts that the report depends on:

  - h264_nvenc present in PyAV's codecs_available (B0 prerequisite);
  - PyNvVideoCodec importable + CreateEncoder works (B1 prerequisite);
  - NVENC GetEncoderCaps: async_encode_support, num_encoder_engines, supported_ratecontrol_modes;
  - whether a CUDA toolkit (nvcc) is installed (pycuda build prerequisite; not required here);
  - cuda-python driver access.

Writes a JSON file under the run dir.  Idempotent and side-effect-free except for creating
one throwaway NVENC encoder (closed immediately) to prove hardware availability.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from pathlib import Path


def _safe(cmd, timeout=3.0):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip() if p.returncode == 0 else ("ERR:%s" % p.stderr.strip()[:200])
    except Exception as e:
        return "EXC:%s" % e


def probe_all() -> dict:
    out = {"captured_at": time.time()}
    # GPU + driver
    out["gpu"] = _safe(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,compute_cap",
                        "--format=csv,noheader,nounits"])
    out["nvcc"] = _safe(["nvcc", "--version"]).replace("\n", " | ")
    # Python
    out["python_version"] = platform.python_version()
    out["python_executable"] = platform.python_prefix()
    # PyAV
    try:
        import av
        out["pyav_version"] = av.__version__
        out["pyav_h264_codecs"] = [c for c in av.codecs_available
                                   if "h264" in c.lower()]
    except Exception as e:
        out["pyav_error"] = "%s: %s" % (type(e).__name__, e)
    # PyNvVideoCodec
    try:
        import PyNvVideoCodec as nvc
        out["pynv_version"] = nvc.__version__
        caps = nvc.GetEncoderCaps()
        out["pynv_caps"] = caps if isinstance(caps, dict) else {}
        out["pynv_async_encode_support"] = caps.get("async_encode_support") if isinstance(caps, dict) else None
        out["pynv_num_encoder_engines"] = caps.get("num_encoder_engines") if isinstance(caps, dict) else None
        out["pynv_supported_ratecontrol_modes"] = caps.get("supported_ratecontrol_modes") if isinstance(caps, dict) else None
        out["pynv_mb_per_sec_max"] = caps.get("mb_per_sec_max") if isinstance(caps, dict) else None
        # create one throwaway encoder to prove HW availability
        try:
            enc = nvc.CreateEncoder(1920, 1080, "NV12", True, codec="h264",
                                     preset="P1", tuning_info="ultra_low_latency",
                                     rc="cbr", bitrate=10000000, maxbitrate=10000000,
                                     vbvbufsize=10000000, gop=60, idrperiod=60,
                                     bf=0, lookahead=0, aq=0, temporalaq=0, repeatspspps=1)
            sps = enc.GetSequenceParams()
            out["pynv_probe_sps_len"] = len(sps)
            out["pynv_probe_sps_profile_idc"] = sps[5] if len(sps) > 5 else None
            out["pynv_probe_ok"] = True
            del enc
        except Exception as e:
            out["pynv_probe_ok"] = False
            out["pynv_probe_error"] = "%s: %s" % (type(e).__name__, e)
    except Exception as e:
        out["pynv_error"] = "%s: %s" % (type(e).__name__, e)
    # cuda-python
    try:
        from cuda import cuda
        cuda.cuInit(0)
        err, n = cuda.cuDeviceGetCount()
        out["cuda_python_ok"] = True
        out["cuda_python_ndevices"] = n
    except Exception as e:
        out["cuda_python_error"] = "%s: %s" % (type(e).__name__, e)
    # NVENC library presence (libnvidia-encode.so)
    out["libnvidia_encode"] = _safe(["bash", "-c",
        "ls /usr/lib/x86_64-linux-gnu/libnvidia-encode.so* 2>/dev/null | head -3"])
    return out


def write_probe(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    data = probe_all()
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return data


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "env_probe.json"
    d = write_probe(p)
    print(json.dumps(d, indent=2, default=str))
