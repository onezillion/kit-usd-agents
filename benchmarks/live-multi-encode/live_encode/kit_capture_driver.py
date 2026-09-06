"""Kit-side multi-camera live capture driver — runs INSIDE the Kit process.

This module is the reference implementation of the producer half of Architecture B.
It is injected into Kit's embedded Python via the Kit Lab MCP (an orchestrator reads
this file and ``exec``s a small runner in Kit). It does NOT import the external
benchmark venv; it only uses Kit modules + stdlib + multiprocessing.shared_memory.

Per stream i in [0, N):
  - benchmark camera  /World/KhlLiveEncode/cam_i        (deterministic pose+motion)
  - hidden ViewportWidget(resolution=(w,h)) -> viewport_api.camera_path = cam_i
       (yielding an INDEPENDENT hydra render product for that camera; production-
        proven in omni.khl.webrtc_stream)
  - per-product ByteCapture(aov_name="LdrColor") on that viewport_api.schedule_capture
  - on capture: PyCapsule_GetPointer -> ctypes.memmove -> ProducerRing SHM slot
    -> put_nowait(slot_idx) on a bounded signal mp.Queue (drop-on-full -> counter)

Design constraints honored:
  * No long blocking on Kit's event loop: the capture callback does ONE memmove +
    one non-blocking queue put. The periodic scheduler is driven by an async task
    that yields (asyncio.sleep) between frames.
  * Torn-frame safe: per-slot frame_id+CRC header (see shm_ring).
  * Clean lifecycle: stop captures, destroy viewports, remove render products and
    the /World/KhlLiveEncode namespace, close+unlink SHM.
  * Separated counters per stream.

NOTE: The ``exec``-in-Kit runner keeps instances on the Kit Lab namespace
(``_khl_lme``) so state survives across MCP calls until explicitly stopped.
"""

from __future__ import annotations

RUNNER_SOURCE = r'''
import asyncio, ctypes, time, json, traceback, socket, struct
import numpy as np
import carb
import omni.usd
from pxr import Gf, Sdf, UsdGeom, Usd
from multiprocessing import shared_memory
from omni.kit.widget.viewport import ViewportWidget
from omni.kit.widget.viewport.capture import ByteCapture
import omni.kit.app

# --- slot ring (self-contained copy of shm_ring.ProducerRing header logic) ----
import struct, zlib
_HDR = struct.Struct("<QQQII")
SLOT_HEADER_BYTES = 32
MAGIC = 0x4B484C5F4C4D4531

def _slot_stride(w, h):
    return SLOT_HEADER_BYTES + w * h * 4

class _Ring:
    def __init__(self, name, w, h, nslots, create=True):
        self.w, self.h = w, h
        self.nslots = max(2, nslots)
        self.payload = w * h * 4
        self.stride = _slot_stride(w, h)
        self.total = self.stride * self.nslots
        self.shm = shared_memory.SharedMemory(name=name, create=create, size=self.total)
        self.name = self.shm.name
        self.buf = self.shm.buf
        self._base = ctypes.addressof(ctypes.c_char.from_buffer(self.buf))
        self._fid = 0
        self._next = 0
    def write_from_addr(self, src, cts):
        if not src:
            return None, None
        slot = self._next
        off = slot * self.stride
        poff = off + SLOT_HEADER_BYTES
        ctypes.memmove(self._base + poff, src, self.payload)
        crc = zlib.crc32(self.buf[poff:poff + self.payload]) & 0xFFFFFFFF
        fid = self._fid
        _HDR.pack_into(self.buf, off, MAGIC, fid, cts, crc, self.payload)
        self._fid += 1
        self._next = (slot + 1) % self.nslots
        return slot, fid
    def close(self, unlink=True):
        try:
            self.shm.close()
        finally:
            if unlink:
                try: self.shm.unlink()
                except FileNotFoundError: pass

# --- capsule helpers ----------------------------------------------------------
_GetPtr = ctypes.pythonapi.PyCapsule_GetPointer
_GetPtr.restype = ctypes.c_void_p
_GetPtr.argtypes = (ctypes.py_object, ctypes.c_char_p)
_GetName = ctypes.pythonapi.PyCapsule_GetName
_GetName.restype = ctypes.c_char_p
_GetName.argtypes = (ctypes.py_object,)

def _now_ns():
    return time.perf_counter_ns()


_SIG = struct.Struct("<iI")  # (slot:int32, frame_id:uint32)

class StreamProducer:
    """One independent live camera -> render product -> capture -> SHM ring.
    Signals slot availability to the encoder subprocess via a unix datagram socket."""
    def __init__(self, idx, w, h, nslots, sock_path, cam_path, motion, period):
        self.idx = idx
        self.sid = f"S{idx}"
        self.w, self.h = w, h
        self.sock_path = sock_path    # unix dgram socket the encoder subprocess binds
        self.cam_path = cam_path
        self.motion = motion
        self.period = period
        self.ring = _Ring(None, w, h, nslots)
        self.viewport = None
        self._sock = None
        self._capture = None
        self._capsule_name = None
        self._running = False
        self._pending = False
        self._pending_t = 0.0
        self._next_t = 0.0
        # separated counters
        self.capture_requested = 0
        self.capture_completed = 0
        self.frames_transferred = 0
        self.frames_enqueued = 0
        self.frames_replaced = 0
        self.frames_dropped = 0
        self.capture_overrun = 0
        self.cap_dur = []       # capture callback duration (s)
        self.memmove_dur = []   # host->shm copy duration (s)
        self.completed_ts = []  # capture-complete times for independence scatter
        self._task = None

    def setup_viewport(self, stage):
        self.viewport = ViewportWidget(resolution=(int(self.w), int(self.h)))
        self.viewport.viewport_api.camera_path = self.cam_path
        self.viewport.visible = False
        self.viewport.viewport_api.updates_enabled = True
        self.viewport.updates_enabled = False
        # render product identification (independence proof)
        try:
            self.render_product_path = str(self.viewport.viewport_api.render_product_path)
        except Exception:
            self.render_product_path = None
        # production-equivalent per-render-product settings (omni.khl.webrtc_stream):
        # async hydra render, no async-low-latency, 60Hz tick, no gizmos, DLSS on.
        try:
            tex_name = self.render_product_path.rstrip("/").split("/")[-1] if self.render_product_path else None
            if tex_name:
                ss = carb.settings.get_settings()
                ss.set_bool(f"/exts/omni.kit.hydra_texture/{tex_name}/async", True)
                ss.set_bool(f"/exts/omni.kit.hydra_texture/{tex_name}/asyncLowLatency", False)
                ss.set_int(f"/exts/omni.kit.hydra_texture/{tex_name}/hydraTickRate", 60)
                ss.set_bool(f"/exts/omni.kit.hydra_texture/{tex_name}/gizmos/enabled", False)
        except Exception:
            pass
        try:
            tex_prim = stage.GetPrimAtPath(self.render_product_path)
            if tex_prim and tex_prim.IsValid():
                attr = tex_prim.GetAttribute("omni:rtx:pt:dlss:enabled")
                if attr:
                    attr.Set(True)
        except Exception:
            pass

    def prime_capsule_name(self):
        # one-shot capture to learn the capsule buffer name
        got = {"done": False}
        def _probe(buffer, size, w, h, format):
            try:
                self._capsule_name = _GetName(buffer)
            except Exception:
                pass
            got["done"] = True
        try:
            self.viewport.viewport_api.schedule_capture(ByteCapture(_probe, aov_name="LdrColor"))
        except Exception:
            pass

    def _on_capture(self, buffer, size, w, h, format):
        t0 = time.perf_counter()
        self._pending = False
        self.capture_completed += 1
        if not self._running:
            return
        if self._capsule_name is None:
            self._capsule_name = _GetName(buffer)
        ptr = _GetPtr(buffer, self._capsule_name)
        if not ptr:
            self.frames_dropped += 1
            return
        cts = _now_ns()
        t_m0 = time.perf_counter()
        slot, fid = self.ring.write_from_addr(ptr, cts)
        t_m1 = time.perf_counter()
        if slot is None:
            self.frames_dropped += 1
            return
        self.frames_transferred += 1
        # signal the encoder subprocess (non-blocking datagram; drop counted by
        # the consumer as a gap if the socket buffer is full)
        try:
            self._sock.sendto(_SIG.pack(slot, fid & 0xFFFFFFFF), self.sock_path)
            self.frames_enqueued += 1
        except (BlockingIOError, OSError):
            self.frames_dropped += 1
        self.cap_dur.append(t_m1 - t0)
        self.memmove_dur.append(t_m1 - t_m0)
        self.completed_ts.append(cts)

    async def run(self):
        self._running = True
        self._capture = ByteCapture(self._on_capture, aov_name="LdrColor")
        # unix datagram socket, non-blocking, for slot signals
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._next_t = time.perf_counter()
        while self._running:
            now = time.perf_counter()
            if self._pending:
                if now - self._pending_t > 1.0:
                    self.capture_overrun += 1
                    self._pending = False
                else:
                    await asyncio.sleep(min(self.period, 0.005))
                    continue
            self._pending = True
            self._pending_t = now
            self.capture_requested += 1
            try:
                self.viewport.viewport_api.schedule_capture(self._capture)
            except Exception:
                self._pending = False
                self.frames_dropped += 1
            self._next_t += self.period
            delay = self._next_t - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                self._next_t = time.perf_counter()
                await asyncio.sleep(0)

    def start(self):
        self._task = asyncio.get_event_loop().create_task(self.run())

    async def stop(self):
        self._running = False
        if self._task is not None:
            try:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            except Exception:
                pass
            self._task = None
        if self._sock is not None:
            try:
                # sentinel: tell the encoder subprocess to stop
                self._sock.sendto(_SIG.pack(-1, 0), self.sock_path)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def counters(self):
        def _stats(v):
            if not v:
                return {"p50":0,"p95":0,"p99":0,"max":0,"mean":0}
            s = sorted(v)
            def q(p): return s[max(0,min(len(s)-1,int(round(p/100*(len(s)-1)))))]
            return {"p50":q(50)*1e3,"p95":q(95)*1e3,"p99":q(99)*1e3,
                    "max":max(v)*1e3,"mean":(sum(v)/len(v))*1e3}
        return {
            "stream_id": self.sid,
            "camera_path": str(self.cam_path),
            "render_product_path": self.render_product_path,
            "shm_name": self.ring.name,
            "capture_requested": self.capture_requested,
            "capture_completed": self.capture_completed,
            "frames_transferred": self.frames_transferred,
            "frames_enqueued": self.frames_enqueued,
            "frames_dropped": self.frames_dropped,
            "capture_overrun": self.capture_overrun,
            "first_frame_id": 0,
            "last_frame_id": self.ring._fid - 1,
            "cap_cb_ms": _stats(self.cap_dur),
            "memmove_ms": _stats(self.memmove_dur),
            # inter-completion interval stats (decorrelation + jitter evidence)
            "completion_interval_ms": _stats(self._intervals()),
            # full per-stream completion timestamps retained for offline decorrelation
            "completed_ts_full": list(self.completed_ts),
            "n_completed_ts": len(self.completed_ts),
        }

    def _intervals(self):
        ts = self.completed_ts
        if len(ts) < 2:
            return []
        return [(ts[i+1] - ts[i]) / 1e9 for i in range(len(ts) - 1)]


class MultiCamDriver:
    """Owns N StreamProducers + benchmark cameras under /World/KhlLiveEncode."""
    ROOT = "/World/KhlLiveEncode"
    def __init__(self, n, w, h, fps, motion, nslots, sock_paths, meters_per_unit=1.0):
        self.n = n
        self.w, self.h, self.fps = w, h, fps
        self.motion = motion
        self.nslots = nslots
        self.sock_paths = sock_paths  # list[str] length n, one per stream
        self.meters_per_unit = meters_per_unit
        self.period = 1.0 / fps
        self.producers = []
        self.stage = None
        self._motion_task = None
        self._running = False
        self.centre_units = None     # set in setup() from scene bbox (scene units)
        self.floor_z_units = None
        self.yaw_offset = 0.0

    def _lookat_zup(self, eye, target):
        """Return a Gf.Matrix4d camera transform (Z-up world) looking eye->target.
        USD camera looks down its -Z axis with +Y up in camera space; for a Z-up
        world we build a look-at basis and orient accordingly."""
        from pxr import Gf
        import math
        e = Gf.Vec3d(*eye)
        t = Gf.Vec3d(*target)
        fwd = (t - e)
        if fwd.GetLength() < 1e-6:
            fwd = Gf.Vec3d(0, 0, -1)
        fwd.Normalize()
        up_world = Gf.Vec3d(0, 0, 1)   # Z-up world
        right = Gf.Cross(fwd, up_world)
        if right.GetLength() < 1e-6:
            right = Gf.Vec3d(1, 0, 0)
        right.Normalize()
        up = Gf.Cross(right, fwd)
        up.Normalize()
        # camera -Z is fwd, +X is right, +Y is up
        rot = Gf.Matrix3d(
            right[0], up[0], -fwd[0],
            right[1], up[1], -fwd[1],
            right[2], up[2], -fwd[2])
        m = Gf.Matrix4d(rot, e)
        return m

    def _place_cameras(self):
        from pxr import UsdGeom, Gf
        import math
        mpu = max(self.meters_per_unit, 1e-6)   # scene units per meter (0.01 => cm)
        # scene-space center/extent handed in by orchestrator (computed live)
        cx, cy, cz = self.centre_units
        # ring radius ~ 6 m, camera height ~ 2.5-4 m above floor
        r = 6.0 / mpu
        for i in range(self.n):
            ang = (2.0 * math.pi * i) / max(1, self.n) + self.yaw_offset
            ex = cx + r * math.cos(ang)
            ey = cy + r * math.sin(ang)
            ez = self.floor_z_units + (2.5 + 0.5 * (i % 3)) / mpu
            cam_path = f"{self.ROOT}/cam_{i}"
            cam = UsdGeom.Camera.Define(self.stage, cam_path)
            mat = self._lookat_zup((ex, ey, ez), (cx, cy, self.floor_z_units + 1.5 / mpu))
            cam.AddTransformOp().Set(mat)   # creates xformOp:transform (double)
            cam.GetFStopAttr().Set(0.0)
            cam.GetFocusDistanceAttr().Set(400.0 / mpu)
            cam.GetClippingRangeAttr().Set(Gf.Vec2f(0.3 / mpu, 200.0 / mpu))

    async def setup(self):
        usd = omni.usd.get_context()
        self.stage = usd.get_stage()
        # author into the SESSION layer (anonymous), never the Nucleus root
        try:
            self.stage.SetEditTarget(self.stage.GetSessionLayer())
        except Exception:
            pass
        # derive scene centre/extent from /World if not provided
        from pxr import UsdGeom
        if getattr(self, "centre_units", None) is None:
            try:
                cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
                bb = cache.ComputeWorldBound(self.stage.GetPrimAtPath("/World"))
                rng = bb.ComputeAlignedRange()
                mn, mx = rng.GetMin(), rng.GetMax()
                self.centre_units = ((mn[0]+mx[0])/2, (mn[1]+mx[1])/2, (mn[2]+mx[2])/2)
                self.floor_z_units = mn[2]
            except Exception:
                self.centre_units = (0.0, 0.0, 0.0)
                self.floor_z_units = 0.0
        # clean any previous bench namespace
        if self.stage.GetPrimAtPath(self.ROOT):
            self.stage.RemovePrim(self.ROOT)
        self.stage.DefinePrim(self.ROOT, "Xform")
        self._place_cameras()
        for i in range(self.n):
            p = StreamProducer(i, self.w, self.h, self.nslots, self.sock_paths[i],
                               f"{self.ROOT}/cam_{i}", self.motion, self.period)
            p.setup_viewport(self.stage)
            self.producers.append(p)
        # let viewports actually build render products
        app = omni.kit.app.get_app()
        for _ in range(6):
            await app.next_update_async()
        for p in self.producers:
            p.prime_capsule_name()
        for _ in range(4):
            await app.next_update_async()

    async def start(self):
        self._running = True
        for p in self.producers:
            p.start()
        if self.motion:
            self._motion_task = asyncio.get_event_loop().create_task(self._motion_loop())

    async def _motion_loop(self):
        # small deterministic yaw oscillation around the camera's own position
        # (Z-up world) that PROVABLY changes the camera transform over time.
        import math
        from pxr import UsdGeom, Gf
        t0 = time.perf_counter()
        cams = [UsdGeom.Camera.Get(self.stage, f"{self.ROOT}/cam_{i}") for i in range(self.n)]
        base = []
        for c in cams:
            try:
                base.append(Gf.Matrix4d(c.GetLocalTransformation()))
            except Exception:
                base.append(None)
        while self._running:
            t = time.perf_counter() - t0
            for i, c in enumerate(cams):
                if base[i] is None:
                    continue
                # yaw oscillation about the world-Z axis through the camera origin
                yaw = math.radians(8.0 * math.sin(2.0 * math.pi * 0.2 * t + i * 1.3))
                origin = base[i].ExtractTranslation()
                to_origin = Gf.Matrix4d().SetTranslate(-origin)
                rot = Gf.Matrix4d(Gf.Rotation(Gf.Vec3d(0, 0, 1), math.degrees(yaw)), Gf.Vec3d(0, 0, 0))
                back = Gf.Matrix4d().SetTranslate(origin)
                try:
                    c.GetPrim().GetAttribute("xformOp:transform").Set(back * rot * to_origin * base[i])
                except Exception:
                    pass
            await asyncio.sleep(1.0 / 30.0)

    async def stop(self):
        self._running = False
        for p in self.producers:
            await p.stop()
        if self._motion_task is not None:
            self._motion_task.cancel()
            await asyncio.gather(self._motion_task, return_exceptions=True)
            self._motion_task = None

    def snapshot(self):
        return {
            "n_streams": self.n,
            "resolution": [self.w, self.h],
            "fps_target": self.fps,
            "motion": self.motion,
            "producers": [p.counters() for p in self.producers],
        }

    async def cleanup(self):
        await self.stop()
        removed_rps = []
        # 1) per-producer: deactivate viewport + destroy widget + record its RP path
        for p in self.producers:
            try:
                rp = getattr(p, "render_product_path", None)
                if p.viewport is not None:
                    vpa = p.viewport.viewport_api
                    try: vpa.updates_enabled = False
                    except Exception: pass
                    try: vpa.render_product_path = ""
                    except Exception: pass
                    try: vpa.camera_path = ""
                    except Exception: pass
                    try:
                        p.viewport.updates_enabled = False
                        p.viewport.visible = False
                        p.viewport.destroy()
                    except Exception: pass
                    p.viewport = None
                if rp:
                    removed_rps.append(rp)
            except Exception:
                pass
            try:
                p.ring.close(unlink=True)
            except Exception:
                pass
        # 2) remove render-product prims (recorded path first, then sweep any
        #    ViewportTexture_* in the session layer that is not the main viewport's)
        try:
            for rp in removed_rps:
                if rp and self.stage.GetPrimAtPath(rp).IsValid():
                    try: self.stage.RemovePrim(rp)
                    except Exception: pass
        except Exception:
            pass
        # 3) remove the benchmark camera namespace entirely
        if self.stage and self.stage.GetPrimAtPath(self.ROOT):
            try: self.stage.RemovePrim(self.ROOT)
            except Exception: pass
        self.producers = []


def sweep_benchmark_render_products(stage, keep_main=True):
    """Remove leftover benchmark viewport render products + the KhlLiveEncode
    namespace from the session layer. Keeps the primary active viewport's own
    texture (ViewportTexture_0) when keep_main is True. Returns counts."""
    import re
    removed_rps = 0
    try:
        if stage.GetPrimAtPath("/World/KhlLiveEncode"):
            stage.RemovePrim("/World/KhlLiveEncode")
    except Exception:
        pass
    for prim in list(stage.Traverse()):
        try:
            if prim.GetTypeName() != "RenderProduct":
                continue
            path = str(prim.GetPath())
            m = re.search(r"ViewportTexture_(\d+)$", path)
            if not m:
                continue
            if keep_main and int(m.group(1)) == 0:
                continue
            stage.RemovePrim(path)
            removed_rps += 1
        except Exception:
            pass
    return {"removed_rps": removed_rps}


# module-level holder so state survives across MCP exec calls
LME = {"driver": None}
'''
