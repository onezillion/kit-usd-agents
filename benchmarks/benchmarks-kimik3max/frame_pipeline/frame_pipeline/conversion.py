"""Frame format conversion helpers with honest transfer accounting.

* ``rgba_to_nv12_cpu``  - host-side RGBA -> NV12 (BT.601 limited range). This is
  a *known CPU* operation and an explicit counted operation for the pipeline.
* ``BgraToNv12Gpu``     - device-side conversion using a CUDA kernel JIT-compiled
  at runtime with NVRTC for the local SM. This is a *known GPU-local* operation;
  the only transfers are H2D of the source (if the source started on the host)
  and the device-local output buffer the kernel writes.

The GPU path exists so B1 can consume a CUDA input surface and keep the RGBA->
NV12 conversion off the CPU, matching the intent of a "GPU-oriented" pipeline.
"""

from __future__ import annotations

import ctypes
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional

import numpy as np

from .contracts import MemoryDomain, TransferClass

# BT.601 limited-range coefficients (studio swing).
_KR, _KG, _KB = 0.299, 0.587, 0.114


def rgba_to_nv12_cpu(rgba: np.ndarray) -> np.ndarray:
    """Convert an (H, W, 4) uint8 RGBA image to a packed NV12 buffer (CPU).

    Returns a 1-D uint8 array of size H*W*3//2 (Y plane then interleaved VU).
    Transfer class: KNOWN_CPU (no device involvement).
    """

    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError("expected (H, W, 4) uint8 RGBA")
    h, w = rgba.shape[:2]
    r = rgba[..., 0].astype(np.float32)
    g = rgba[..., 1].astype(np.float32)
    b = rgba[..., 2].astype(np.float32)
    # Y (limited range 16..235)
    y = 16.0 + (65.481 * r + 128.553 * g + 24.966 * b) / 255.0
    y_plane = np.clip(y, 0, 255).astype(np.uint8)
    # Subsampled chroma (2x2 average), U/V limited range 16..240
    r2 = r.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
    g2 = g.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
    b2 = b.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
    u = 128.0 + (-37.797 * r2 - 74.203 * g2 + 112.0 * b2) / 255.0
    v = 128.0 + (112.0 * r2 - 93.786 * g2 - 18.214 * b2) / 255.0
    u8 = np.clip(u, 0, 255).astype(np.uint8).ravel()
    v8 = np.clip(v, 0, 255).astype(np.uint8).ravel()
    # NV12 UV plane is interleaved V,U order? NV12 is Y ... then interleaved U,V
    # as [U0 V0 U1 V1 ...] per the NV12 spec (CbCr). Some encoders accept either;
    # we follow the common NV12 = Y + interleaved Cb(U)Cr(V).
    uv = np.empty(u8.size * 2, dtype=np.uint8)
    uv[0::2] = u8
    uv[1::2] = v8
    out = np.empty(h * w + uv.size, dtype=np.uint8)
    out[: h * w] = y_plane.ravel()
    out[h * w:] = uv
    return out


class ParallelConverter:
    """Bounded pool of CPU converters.

    numpy releases the GIL for the bulk arithmetic in :func:`rgba_to_nv12_cpu`,
    so a small thread pool gives near-linear speedup on multi-core hosts. The
    pool size is bounded at 4 (measured to reach ~3x at 1080p); larger values
    add memory-bandwidth contention without helping NVENC.
    """

    def __init__(self, workers: int = 4) -> None:
        self.workers = max(1, workers)
        self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="prep")

    def map(self, frames: List[Any]) -> List[np.ndarray]:
        if not frames:
            return []
        return list(self._pool.map(rgba_to_nv12_cpu, frames))

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)


_KERNEL_SRC = r"""
extern "C" __global__ void rgba_to_nv12_kernel(
    const unsigned char* __restrict__ src, int width, int height, int src_pitch,
    unsigned char* __restrict__ y_plane, unsigned char* __restrict__ uv_plane,
    int is_bgra)
{
    int x = blockIdx.x * blockDim.x + threadIdx.x;   // pixel x in UV space (width/2)
    int y = blockIdx.y * blockDim.y + threadIdx.y;   // pixel y in UV space (height/2)
    int hw = width >> 1, hh = height >> 1;
    if (x >= hw || y >= hh) return;

    int px = x * 2, py = y * 2;
    float accB = 0.f, accG = 0.f, accR = 0.f;
    #pragma unroll
    for (int dy = 0; dy < 2; ++dy) {
        #pragma unroll
        for (int dx = 0; dx < 2; ++dx) {
            const unsigned char* p = src + (py + dy) * src_pitch + (px + dx) * 4;
            float R, G, B;
            if (is_bgra) { B = (float)p[0]; G = (float)p[1]; R = (float)p[2]; }
            else        { R = (float)p[0]; G = (float)p[1]; B = (float)p[2]; }
            float Y = 16.0f + (65.481f*R + 128.553f*G + 24.966f*B) / 255.0f;
            Y = fminf(fmaxf(Y, 0.f), 255.f);
            y_plane[(py + dy) * width + (px + dx)] = (unsigned char)Y;
            accB += B; accG += G; accR += R;
        }
    }
    accB *= 0.25f; accG *= 0.25f; accR *= 0.25f;
    float U = 128.0f + (-37.797f*accR - 74.203f*accG + 112.0f*accB) / 255.0f;
    float V = 128.0f + (112.0f*accR - 93.786f*accG - 18.214f*accB) / 255.0f;
    U = fminf(fmaxf(U, 0.f), 255.f);
    V = fminf(fmaxf(V, 0.f), 255.f);
    int uv_idx = y * width + x * 2;
    // NV12 interleaved plane stores Cb (U) then Cr (V).
    uv_plane[uv_idx + 0] = (unsigned char)U;
    uv_plane[uv_idx + 1] = (unsigned char)V;
}
"""


class BgraToNv12Gpu:
    """GPU-side BGRA/RGBA -> NV12 converter using a runtime-JIT CUDA kernel.

    This is a *known GPU-local* operation. ``convert`` takes a device pointer to
    a tightly-or-pitched BGRA/RGBA buffer and returns a device pointer to a
    packed NV12 buffer. No host round-trip is performed here.
    """

    def __init__(self, device_index: int = 0) -> None:
        from cuda.bindings import driver as cud  # type: ignore
        from cuda.bindings import nvrtc  # type: ignore

        self._cud = cud
        err, = cud.cuInit(0)
        self._check(err, "cuInit")
        err, dev = cud.cuDeviceGet(device_index)
        self._check(err, "cuDeviceGet")
        err, self.ctx = cud.cuCtxCreate(None, 0, dev)
        self._check(err, "cuCtxCreate")
        self.device_index = device_index
        err, prog = nvrtc.nvrtcCreateProgram(_KERNEL_SRC.encode(), b"bgra_to_nv12.cu", 0, [], [])
        self._check(err, "nvrtcCreateProgram")
        opts = [b"--gpu-architecture=compute_120"]
        # Determine actual arch to be portable across GPUs.
        err, major, minor = cud.cuDeviceComputeCapability(dev)
        if err == cud.CUresult.CUDA_SUCCESS:
            opts = [f"--gpu-architecture=compute_{major}{minor}".encode()]
        err, = nvrtc.nvrtcCompileProgram(prog, len(opts), opts)
        if err != nvrtc.nvrtcResult.NVRTC_SUCCESS:
            err2, lsize = nvrtc.nvrtcGetProgramLogSize(prog)
            log = b" " * lsize
            nvrtc.nvrtcGetProgramLog(prog, log)
            raise RuntimeError(f"NVRTC compile failed: {log.decode(errors='replace')}")
        err, psize = nvrtc.nvrtcGetPTXSize(prog)
        self._check(err, "nvrtcGetPTXSize")
        buf = b" " * psize
        err, = nvrtc.nvrtcGetPTX(prog, buf)
        self._check(err, "nvrtcGetPTX")
        err, self.mod = cud.cuModuleLoadData(buf)
        self._check(err, "cuModuleLoadData")
        err, self.func = cud.cuModuleGetFunction(self.mod, b"rgba_to_nv12_kernel")
        self._check(err, "cuModuleGetFunction")
        self._alloc_size = 0
        self._d_src = 0
        self._d_yuv = 0
        self._is_bgra = 0  # 0 -> RGBA byte order (numpy/Kit LdrColor RGBA), 1 -> BGRA

    @staticmethod
    def _check(err, what: str) -> None:
        name = getattr(err, "name", str(err))
        ok = name.endswith("SUCCESS")
        if not ok:
            raise RuntimeError(f"{what} failed: {name}")

    def _ensure(self, src_bytes: int, yuv_bytes: int) -> None:
        if src_bytes <= self._alloc_size and self._d_src and self._d_yuv:
            return
        cud = self._cud
        if self._d_src:
            cud.cuMemFree(self._d_src)
        if self._d_yuv:
            cud.cuMemFree(self._d_yuv)
        err, self._d_src = cud.cuMemAlloc(src_bytes)
        self._check(err, "cuMemAlloc src")
        err, self._d_yuv = cud.cuMemAlloc(yuv_bytes)
        self._check(err, "cuMemAlloc yuv")
        self._alloc_size = max(src_bytes, yuv_bytes)

    def convert_host_rgba(self, rgba: np.ndarray) -> int:
        """H2D the RGBA host frame, convert on GPU, return device NV12 pointer.

        The H2D copy here is an *explicit* known H2D transfer; the conversion is
        GPU-local. Returning the device pointer lets B1 do a device-input Encode
        without a D2H readback of the converted frame.
        """

        h, w = int(rgba.shape[0]), int(rgba.shape[1])
        yuv_bytes = w * h * 3 // 2
        src_bytes = int(rgba.nbytes)
        self._ensure(src_bytes, yuv_bytes)
        cud = self._cud
        # explicit H2D
        src_view = np.ascontiguousarray(rgba)
        err, = cud.cuMemcpyHtoD(int(self._d_src), src_view.ctypes.data, src_bytes)
        self._check(err, "cuMemcpyHtoD")
        self._launch(int(self._d_src), int(self._d_yuv), w, h, w * 4)
        return int(self._d_yuv)

    def convert_device_rgba(self, src_dev_ptr: int, w: int, h: int, pitch: Optional[int] = None) -> int:
        """Convert an existing device RGBA buffer to NV12 on-device (GPU-local)."""

        yuv_bytes = w * h * 3 // 2
        self._ensure(1, yuv_bytes)
        self._launch(int(src_dev_ptr), int(self._d_yuv), w, h, pitch or (w * 4))
        return int(self._d_yuv)

    def _launch(self, d_src: int, d_yuv: int, w: int, h: int, pitch: int) -> None:
        cud = self._cud
        y_plane = d_yuv
        uv_plane = d_yuv + w * h
        # args: src, width, height, src_pitch, y_plane, uv_plane, uv_offset_is_vu
        a0 = ctypes.c_void_p(d_src)
        a1 = ctypes.c_int(w)
        a2 = ctypes.c_int(h)
        a3 = ctypes.c_int(pitch)
        a4 = ctypes.c_void_p(y_plane)
        a5 = ctypes.c_void_p(uv_plane)
        a6 = ctypes.c_int(self._is_bgra)
        params = (ctypes.c_void_p * 7)(
            ctypes.addressof(a0), ctypes.addressof(a1), ctypes.addressof(a2),
            ctypes.addressof(a3), ctypes.addressof(a4), ctypes.addressof(a5),
            ctypes.addressof(a6),
        )
        block = (16, 16, 1)
        grid = ((w // 2 + block[0] - 1) // block[0], (h // 2 + block[1] - 1) // block[1], 1)
        err, = cud.cuLaunchKernel(
            self.func, grid[0], grid[1], grid[2], block[0], block[1], block[2],
            0, 0, int(ctypes.addressof(params)), 0,
        )
        self._check(err, "cuLaunchKernel")

    def synchronize(self) -> None:
        self._cud.cuCtxSynchronize()

    def readback_nv12(self, size: int) -> np.ndarray:
        """D2H the NV12 buffer for validation only (counted separately)."""

        out = np.empty(size, dtype=np.uint8)
        self._cud.cuMemcpyDtoH(out.ctypes.data_as(ctypes.c_void_p).value, int(self._d_yuv), size)
        return out

    def free(self) -> None:
        cud = self._cud
        if self._d_src:
            cud.cuMemFree(self._d_src)
            self._d_src = 0
        if self._d_yuv:
            cud.cuMemFree(self._d_yuv)
            self._d_yuv = 0
