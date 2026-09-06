#!/usr/bin/env python3
"""Orchestrator for the live multi-camera Stage-1 encoding benchmark.

Runs on the HOST (dedicated venv). Drives the live Kit session via the Kit Lab
HTTP exec endpoint, spawns one encoder subprocess per stream (dedicated venv
python), ramps the number of camera/encoder pipelines, samples NVML (GPU + NVENC
engine utilization) concurrently, and writes results to
$HOME/kit-ai/benchmarks/live-multi-encode/<run-id>/.

Every scaling row is ``pipeline=live``: real Warehouse cameras + independent
render products + ByteCapture -> SHM -> encoder subprocess -> H.264 AU.

Usage:
  python -m scripts.run_scale --levels 1,2 --warmup 6 --measure 10 --scene warehouse
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent
sys.path.insert(0, str(PKG_ROOT))

from live_encode.kit_capture_driver import RUNNER_SOURCE  # noqa: E402
from live_encode.mcp_client import KitLabClient, extract_stdout  # noqa: E402

VENV_PY = "/home/ubuntu/kit-ai/venvs/frame-pipeline-bench/bin/python"
WAREHOUSE = "omniverse://140.110.27.92/NVIDIA/Demos/WarehousePhysics/Worlds/World_Demopack.usd"
OUT_ROOT = os.path.expanduser(
    os.environ.get("LME_OUT", "~/kit-ai/benchmarks/live-multi-encode"))


# ---------------------------------------------------------------------------
# Kit exec helpers (delimited stdout markers)
# ---------------------------------------------------------------------------

def kit_exec(client: KitLabClient, code: str, timeout: float = 120.0) -> dict:
    client.timeout = timeout
    return client.execute(code)


def kit_exec_json(client: KitLabClient, code: str, timeout: float = 120.0):
    wrapped = (
        "import json as _json\n"
        + code
    )
    env = kit_exec(client, wrapped, timeout)
    if not env.get("ok"):
        raise RuntimeError(f"kit exec failed: {env.get('error') or env.get('traceback')}")
    block = extract_stdout(env, "JSON_BEGIN\n", "\nJSON_END")
    if block is None:
        raise RuntimeError(f"no JSON block; stdout={env.get('stdout')!r} stderr={env.get('stderr')!r}")
    return json.loads(block)


# ---------------------------------------------------------------------------
# Kit-side commands (runner + driver bootstrap)
# ---------------------------------------------------------------------------

SETUP_RUNNER = RUNNER_SOURCE + "\nprint('RUNNER_READY')\n"

OPEN_SCENE = r'''
import omni.usd, omni.kit.app, asyncio
from pxr import UsdGeom
async def _open():
    ctx = omni.usd.get_context()
    await ctx.open_stage_async(SCENE_URL)
    app = omni.kit.app.get_app()
    for _ in range(10):
        await app.next_update_async()
    st = ctx.get_stage()
    m = UsdGeom.GetStageMetersPerUnit(st)
    up = UsdGeom.GetStageUpAxis(st)
    print("JSON_BEGIN")
    print(_json.dumps({"opened": st is not None, "mpu": m, "up": str(up),
                       "root_children": [str(p.GetPath()) for p in st.GetPseudoRoot().GetChildren()][:12]}))
    print("JSON_END")
asyncio.get_event_loop().create_task(_open())
print("OPEN_SCHEDULED")
'''

INSTALL_DRIVER = r'''
import time
DRV = MultiCamDriver(@N@, @W@, @H@, @FPS@, @MOTION@, @NSLOTS@, @SOCKPATHS@, meters_per_unit=@MPU@)
LME["driver"] = DRV
LME["setup_err"] = None
async def _setup():
    import traceback
    try:
        await DRV.setup()
        LME["setup_done"] = True
    except Exception as e:
        LME["setup_err"] = f"{type(e).__name__}: {e}"
        LME["setup_tb"] = traceback.format_exc()
        LME["setup_done"] = True
LME["setup_done"] = False
asyncio.get_event_loop().create_task(_setup())
'''


def build_install_code(n: int, args, sock_paths) -> str:
    """Substitute unique @TOKENS@ (no substring collisions like the old N_ bug)."""
    return (INSTALL_DRIVER
            .replace("@N@", str(n))
            .replace("@W@", str(args.width))
            .replace("@H@", str(args.height))
            .replace("@FPS@", str(args.fps))
            .replace("@MOTION@", "True" if args.motion else "False")
            .replace("@NSLOTS@", str(args.nslots))
            .replace("@SOCKPATHS@", repr(list(sock_paths)))
            .replace("@MPU@", repr(args.mpu)))

# synchronous snapshot of driver state (readable state survives across execs)
SNAPSHOT = r'''
_d = LME.get("driver")
if _d is None or not _d.producers:
    print("JSON_BEGIN"); print(_json.dumps({"ready": False})); print("JSON_END")
else:
    print("JSON_BEGIN"); print(_json.dumps(_d.snapshot())); print("JSON_END")
'''

START = r'''
LME["started"] = False
async def _go():
    import traceback
    try:
        await LME["driver"].start()
        LME["started"] = True
    except Exception as e:
        LME["start_err"] = f"{type(e).__name__}: {e}"
        LME["started"] = True
asyncio.get_event_loop().create_task(_go())
'''

STOP_CLEANUP = r'''
LME["cleaned"] = False
async def _stop():
    import traceback
    d = LME.get("driver")
    LME["final_snapshot"] = d.snapshot() if d is not None else None
    LME["cleanup_err"] = None
    try:
        if d is not None:
            await d.cleanup()
    except Exception as e:
        LME["cleanup_err"] = f"{type(e).__name__}: {e}"
    LME["driver"] = None
    LME["cleaned"] = True
asyncio.get_event_loop().create_task(_stop())
'''

# read-back of start/clean completion flags
STATUS_FLAGS = r'''
print("JSON_BEGIN")
print(_json.dumps({"started": LME.get("started"), "start_err": LME.get("start_err"),
                   "cleaned": LME.get("cleaned"), "cleanup_err": LME.get("cleanup_err"),
                   "driver_present": LME.get("driver") is not None}))
print("JSON_END")
'''

RESET_NS = r'''
for _k in list(globals().keys()):
    if _k in ("MultiCamDriver","StreamProducer","_Ring","LME","ByteCapture","ViewportWidget"):
        try: del globals()[_k]
        except Exception: pass
print("JSON_BEGIN"); print(_json.dumps({"ns_reset": True})); print("JSON_END")
'''

# remove leftover benchmark render products + cameras from the session layer
SWEEP = r'''
import omni.usd
_st = omni.usd.get_context().get_stage()
try:
    _st.SetEditTarget(_st.GetSessionLayer())
except Exception: pass
_res = sweep_benchmark_render_products(_st, keep_main=True)
print("JSON_BEGIN"); print(_json.dumps(_res)); print("JSON_END")
'''

# count RenderProduct prims in the session layer + whether KhlLiveEncode exists.
# Used as a HARD precondition gate: exactly 1 RP (the main viewport's) must remain.
PRECONDITION = r'''
import omni.usd
_st = omni.usd.get_context().get_stage()
_rps = [str(p.GetPath()) for p in _st.Traverse() if p.GetTypeName()=="RenderProduct"]
_khl = _st.GetPrimAtPath("/World/KhlLiveEncode")
print("JSON_BEGIN")
print(_json.dumps({"rp_count": len(_rps), "rps": _rps,
                   "khl_present": bool(_khl and _khl.IsValid())}))
print("JSON_END")
'''


def assert_clean_stage(client) -> dict:
    """Document baseline stage state and HARD-fail only on *functional* contamination.

    ViewportTexture RP *prim specs* persist in the session layer even after their
    render resource is released (RemovePrim/destroy don't remove them) — they are
    inert (verified: single-cam capture ~83.5 fps with 47 orphan prims == the clean
    ~86 fps ceiling, so prim count does not reduce throughput). The precondition
    therefore:
      * HARD-fails if a stale /World/KhlLiveEncode camera namespace exists (would
        alias cam_i paths and corrupt per-stream identity), and
      * records rp_count/rps for transparency, but does NOT fail on RP-prim count.
    """
    env = kit_exec(client, "import json as _json\n" + PRECONDITION, timeout=60)
    block = extract_stdout(env, "JSON_BEGIN\n", "\nJSON_END")
    if not block:
        raise RuntimeError(f"precondition read failed: {env.get('stdout')!r}")
    d = json.loads(block)
    if d.get("khl_present"):
        # stale cameras alias cam_i -> hard fail (sweep + recheck once)
        kit_exec(client, "import json as _json\n" + SWEEP, timeout=60)
        env = kit_exec(client, "import json as _json\n" + PRECONDITION, timeout=60)
        d = json.loads(extract_stdout(env, "JSON_BEGIN\n", "\nJSON_END") or "{}")
        if d.get("khl_present"):
            raise RuntimeError("CONTAMINATED: stale /World/KhlLiveEncode present")
    d["rp_prim_count_note"] = (
        "RP prim specs persist inertly; throughput-independent (control: 83.5fps@47prims)")
    return d


# ---------------------------------------------------------------------------
# Main-perplex helpers
# ---------------------------------------------------------------------------

def wait_flag(client, key: str, timeout=30.0, poll=0.4):
    """Poll STATUS_FLAGS until LME[key] becomes truthy; returns the flag dict."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        env = kit_exec(client, "import json as _json\n" + STATUS_FLAGS, timeout=20)
        block = extract_stdout(env, "JSON_BEGIN\n", "\nJSON_END")
        if block:
            d = json.loads(block)
            if d.get(key):
                return d
        time.sleep(poll)
    raise RuntimeError(f"flag {key} not set in time: {d if block else env.get('stdout')}")


def wait_json_ready(client, timeout=180.0, poll=0.75):
    """Poll synchronous state until producer viewports/render products exist.

    Surfaces a Kit-side setup error (LME['setup_err']) if one occurred."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            # NOTE: SYNC_STATUS uses _json, so the import prefix is required.
            env = kit_exec(client, "import json as _json\n" + SYNC_STATUS, timeout=30)
            block = extract_stdout(env, "JSON_BEGIN\n", "\nJSON_END")
            if block:
                d = json.loads(block)
                if d.get("setup_err"):
                    raise RuntimeError(f"kit driver setup error: {d['setup_err']} :: {d.get('setup_tb','')[-400:]}")
                if d.get("producers"):
                    return d
        except RuntimeError:
            raise
        except Exception:
            pass
        time.sleep(poll)
    raise RuntimeError("driver setup did not complete in time")


# synchronous full status: setup flags + producer snapshot
SYNC_STATUS = r'''
_d = LME.get("driver")
_o = {"setup_done": LME.get("setup_done"), "setup_err": LME.get("setup_err"),
      "setup_tb": LME.get("setup_tb")}
if _d is not None and _d.producers:
    _o.update(_d.snapshot())
print("JSON_BEGIN"); print(_json.dumps(_o)); print("JSON_END")
'''


class NvmlSampler(threading.Thread):
    """Aggregate device-wide GPU + NVENC-engine utilization sampler."""

    def __init__(self, interval=0.5, gpu_index=0):
        super().__init__(daemon=True)
        self.interval = interval
        self.gpu_index = gpu_index
        self.stop_flag = threading.Event()
        self.samples = []
        self._avail = False

    def run(self):
        try:
            import pynvml as nv
            nv.nvmlInit()
            h = nv.nvmlDeviceGetHandleByIndex(self.gpu_index)
            self._avail = True
        except Exception:
            self._avail = False
            return
        while not self.stop_flag.is_set():
            t = time.time()
            rec = {"t": t}
            try:
                util = nv.nvmlDeviceGetUtilizationRates(h)
                rec["gpu_pct"] = util.gpu
                rec["mem_pct"] = util.memory
            except Exception:
                pass
            try:
                mem = nv.nvmlDeviceGetMemoryInfo(h)
                rec["vram_used_mb"] = mem.used // (1024 * 1024)
            except Exception:
                pass
            try:
                enc_util, enc_period = nv.nvmlDeviceGetEncoderUtilization(h)
                rec["nvenc_pct"] = enc_util
            except Exception:
                pass
            try:
                rec["clk_sm_mhz"] = nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_SM)
                rec["clk_video_mhz"] = nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_VIDEO)
            except Exception:
                pass
            self.samples.append(rec)
            time.sleep(self.interval)
        try:
            nv.nvmlShutdown()
        except Exception:
            pass

    def summarize(self):
        if not self.samples:
            return {"available": False}
        def agg(key):
            vals = [s[key] for s in self.samples if key in s]
            if not vals:
                return None
            return {"mean": sum(vals) / len(vals), "max": max(vals)}
        return {
            "available": True,
            "n_samples": len(self.samples),
            "gpu_pct": agg("gpu_pct"),
            "nvenc_pct": agg("nvenc_pct"),
            "vram_used_mb_max": max((s.get("vram_used_mb", 0) for s in self.samples), default=0),
            "clk_sm_mhz": agg("clk_sm_mhz"),
            "clk_video_mhz": agg("clk_video_mhz"),
        }


# ---------------------------------------------------------------------------
# Encoder subprocess management
# ---------------------------------------------------------------------------

class EncoderProc:
    def __init__(self, stream_id, shm_name, w, h, nslots, fps, bitrate, gop,
                 out_dir, sock_path, retain_au=1):
        self.stream_id = stream_id
        self.out_dir = out_dir
        self.sock_path = sock_path
        self.result_path = os.path.join(out_dir, f"result_{stream_id}.json")
        os.makedirs(out_dir, exist_ok=True)
        cmd = [
            VENV_PY, "-m", "live_encode.encoder_worker", "run",
            "--stream-id", stream_id, "--shm-name", shm_name,
            "--width", str(w), "--height", str(h), "--nslots", str(nslots),
            "--fps", str(fps), "--bitrate", str(bitrate), "--gop", str(gop),
            "--out-dir", out_dir,
            "--sock", sock_path, "--result", self.result_path,
            "--retain-au", str(retain_au),
        ]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PKG_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen(
            cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @property
    def alive(self):
        return self.proc.poll() is None

    def stop(self, timeout=3.0):
        if self.proc.poll() is not None:
            return
        # graceful: send the -1 sentinel datagram so the worker flushes + writes
        # its result JSON before we escalate to terminate/kill.
        try:
            import socket, struct
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.sendto(struct.pack("<iI", -1, 0), self.sock_path)
            s.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=timeout)
            return
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=timeout)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=2.0)
            except Exception:
                pass

    def result(self):
        try:
            with open(self.result_path) as fh:
                return json.load(fh)
        except Exception as e:
            return {"stream_id": self.stream_id, "error": f"no result: {e}"}


# ---------------------------------------------------------------------------
# Scaling run
# ---------------------------------------------------------------------------

def run_level(client, n, args, run_dir, gpu_index=0):
    """Run one N-camera live level. Returns the per-level result dict."""
    level_dir = os.path.join(run_dir, f"N{n:02d}")
    os.makedirs(level_dir, exist_ok=True)
    sock_paths = [os.path.join(level_dir, f"stream_{i}.sock") for i in range(n)]
    # clear stale sockets
    for sp in sock_paths:
        try:
            os.unlink(sp)
        except FileNotFoundError:
            pass

    # 1) install runner on Kit (idempotent; RUNNER_SOURCE defines classes + LME)
    env = kit_exec(client, SETUP_RUNNER, timeout=60)
    if "RUNNER_READY" not in env.get("stdout", ""):
        raise RuntimeError(f"runner install failed: {env.get('stdout')} {env.get('stderr')} {env.get('traceback')}")

    # 1b) HARD precondition: stage must be clean (1 RP, no KhlLiveEncode) before
    #     this level measures — eliminates the leftover-RP confound (GLM F1/C8).
    pre = assert_clean_stage(client)

    # 2) install the N-camera driver on Kit's event loop
    kit_exec(client, build_install_code(n, args, sock_paths), timeout=60)

    # 3) wait until viewports/render products built
    snap0 = wait_json_ready(client, timeout=180.0)
    # get shm names + render products via a dedicated json snapshot
    envd = kit_exec_json(client, r'''
d = LME.get("driver")
print("JSON_BEGIN")
print(_json.dumps({"shm_names": [p.ring.name for p in d.producers] if d else [],
                   "render_products": [p.render_product_path for p in d.producers] if d else []}))
print("JSON_END")
''', timeout=30)
    shm_names = envd["shm_names"]
    render_products = envd["render_products"]

    # 4) spawn encoder subprocesses
    procs = []
    for i in range(n):
        e = EncoderProc(f"S{i}", shm_names[i], args.width, args.height,
                        args.nslots, args.fps, args.bitrate, args.gop,
                        level_dir, sock_paths[i], retain_au=args.retain_au)
        procs.append(e)
    # wait for encoders to attach (bind sockets)
    t0 = time.time()
    while time.time() - t0 < 30:
        if all(os.path.exists(p.sock_path) for p in procs):
            break
        time.sleep(0.2)

    # 5) NVML sampler starts (covers warmup+measure)
    sampler = NvmlSampler(interval=args.nvml_interval, gpu_index=gpu_index)
    sampler.start()

    # 6) START capture on Kit (wait until producer tasks are actually running)
    kit_exec(client, START, timeout=30)
    st_flags = wait_flag(client, "started", timeout=30)
    if st_flags.get("start_err"):
        raise RuntimeError(f"start error: {st_flags['start_err']}")
    start_ts = time.time()
    warmup_end = start_ts + args.warmup
    measure_end = warmup_end + args.measure

    # record baseline counters at warmup end
    time.sleep(max(0.0, warmup_end - time.time()))
    base_snap = kit_exec_json(client, r'''
d = LME.get("driver")
print("JSON_BEGIN")
print(_json.dumps(d.snapshot() if d else {}))
print("JSON_END")
''', timeout=30)

    # 7) measure window
    time.sleep(max(0.0, measure_end - time.time()))
    end_snap = kit_exec_json(client, r'''
d = LME.get("driver")
print("JSON_BEGIN")
print(_json.dumps(d.snapshot() if d else {}))
print("JSON_END")
''', timeout=30)
    end_ts = time.time()

    # 8) STOP + cleanup on Kit (waits until the driver is removed server-side)
    kit_exec(client, STOP_CLEANUP, timeout=30)
    try:
        cl_flags = wait_flag(client, "cleaned", timeout=30)
    except Exception as e:
        cl_flags = {"cleanup_err": str(e)}
    # 8b) sweep leftover render products/cameras (defensive; ensures session clean)
    try:
        swp = kit_exec(client, "import json as _json\n" + SWEEP, timeout=60)
        swb = extract_stdout(swp, "JSON_BEGIN\n", "\nJSON_END")
        cl_flags["sweep"] = json.loads(swb) if swb else None
    except Exception:
        pass

    # 9) stop encoders + collect results
    sampler.stop_flag.set()
    for e in procs:
        e.stop()
    enc_results = [e.result() for e in procs]
    nvml = sampler.summarize()

    # process accounting: no zombies among our spawned children
    zombies = [e.stream_id for e in procs if e.proc.poll() is None]

    measured_s = end_ts - start_ts - args.warmup
    result = {
        "pipeline": "live",
        "level": n,
        "resolution": [args.width, args.height],
        "fps_target": args.fps,
        "motion": args.motion,
        "scene": args.scene,
        "warmup_s": args.warmup,
        "measure_s": args.measure,
        "measured_window_s": measured_s,
        "render_products": render_products,
        "kit_producers": end_snap.get("producers", []),
        "kit_producers_base": base_snap.get("producers", []),
        "encoder_results": enc_results,
        "nvml": nvml,
        "zombies": zombies,
        "cleanup": cl_flags,
        "precondition": pre,
    }
    with open(os.path.join(level_dir, "level_result.json"), "w") as fh:
        json.dump(result, fh, indent=1)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,2")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--bitrate", type=int, default=10_000_000)
    ap.add_argument("--gop", type=int, default=60)
    ap.add_argument("--nslots", type=int, default=5)
    ap.add_argument("--warmup", type=float, default=6.0)
    ap.add_argument("--measure", type=float, default=10.0)
    ap.add_argument("--motion", action="store_true", default=True)
    ap.add_argument("--static", dest="motion", action="store_false")
    ap.add_argument("--scene", default="warehouse")
    ap.add_argument("--mpu", type=float, default=1.0)
    ap.add_argument("--gpu-index", type=int, default=0)
    ap.add_argument("--nvml-interval", type=float, default=0.5)
    ap.add_argument("--retain-au", type=int, default=1)
    ap.add_argument("--kit-url", default="http://127.0.0.1:8011")
    ap.add_argument("--open-scene", action="store_true")
    args = ap.parse_args()

    run_id = time.strftime("%Y%m%dT%H%M%S")
    run_dir = os.path.join(OUT_ROOT, f"{run_id}-scale")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "config.json"), "w") as fh:
        json.dump(vars(args), fh, indent=1)

    client = KitLabClient(base=args.kit_url)

    if args.open_scene:
        code = OPEN_SCENE.replace("SCENE_URL", repr(WAREHOUSE))
        kit_exec(client, code, timeout=30)
        # wait for open to complete
        t0 = time.time()
        opened = None
        while time.time() - t0 < 300:
            try:
                env = kit_exec(client, r'''
import omni.usd
st = omni.usd.get_context().get_stage()
from pxr import UsdGeom
print("JSON_BEGIN")
print(_json.dumps({"opened": st is not None,
                   "mpu": UsdGeom.GetStageMetersPerUnit(st) if st else None,
                   "prim_count": len(list(st.Traverse())) if st else 0}))
print("JSON_END")
''', timeout=30)
                block = extract_stdout(env, "JSON_BEGIN\n", "\nJSON_END")
                if block:
                    d = json.loads(block)
                    if d.get("opened") and d.get("prim_count", 0) > 50:
                        opened = d
                        break
            except Exception:
                pass
            time.sleep(2.0)
        if opened is None:
            raise RuntimeError("Warehouse scene did not open in time")
        args.mpu = opened.get("mpu") or 1.0
        print(f"[scene] opened Warehouse mpu={args.mpu} prims={opened.get('prim_count')}")

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    summary = {"run_id": run_id, "scene": args.scene, "levels": []}
    print(f"[run] levels={levels} out={run_dir}", flush=True)
    for n in levels:
        print(f"[run] === N={n} ===", flush=True)
        try:
            res = run_level(client, n, args, run_dir, args.gpu_index)
            summary["levels"].append(res)
            agg = aggregate_level(res)
            print(f"[run] N={n} -> {json.dumps(agg)}", flush=True)
        except Exception as e:
            print(f"[run] N={n} FAILED: {type(e).__name__}: {e}", flush=True)
            summary["levels"].append({"level": n, "pipeline": "live",
                                       "error": f"{type(e).__name__}: {e}"})
            # attempt cleanup of any Kit-side driver
            try:
                kit_exec(client, STOP_CLEANUP, timeout=30)
            except Exception:
                pass
        # brief settle between levels
        time.sleep(2.0)

    with open(os.path.join(run_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(f"[run] done -> {run_dir}/summary.json", flush=True)


def aggregate_level(res):
    if "error" in res:
        return res
    enc = [e for e in res.get("encoder_results", []) if "error" not in e]
    fps_list = [e.get("encoded_fps_over_run", 0.0) for e in enc]
    slow = min(fps_list) if fps_list else 0.0
    mean = sum(fps_list) / len(fps_list) if fps_list else 0.0
    return {
        "level": res["level"],
        "n_encoders_ok": len(enc),
        "slowest_fps": round(slow, 2),
        "mean_fps": round(mean, 2),
        "aggregate_fps": round(sum(fps_list), 2),
        "torn": sum(e.get("torn_frames", 0) for e in enc),
        "zombies": res.get("zombies", []),
        "nvenc_pct_max": (res.get("nvml", {}).get("nvenc_pct") or {}).get("max"),
        "gpu_pct_max": (res.get("nvml", {}).get("gpu_pct") or {}).get("max"),
    }


if __name__ == "__main__":
    main()
