"""Linux Kit lifecycle by persistent environment identity, never parentage or saved PID."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .client import KitLabClient, KitLabClientError


KIT_ID = "nchc-kit-dev-main"
REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_CONFIG = Path.home() / "kit-ai/lab/lifecycle.json"
MAX_ENVIRONMENT_BYTES = 4 * 1024 * 1024
Progress = Callable[[float, float | None, str], Awaitable[None]]


def operation_progress(callback: Progress | None) -> Progress | None:
    if callback is None:
        return None
    started = time.monotonic()
    async def report(elapsed: float, total: float | None, message: str):
        # One MCP request can pass through stop, force, launch and readiness phases.
        # Keep protocol progress increasing across phase-local clocks and deadlines.
        await callback(time.monotonic() - started, None, message)
    return report


class LifecycleError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class LifecycleConfig:
    kit_id: str = KIT_ID
    launcher: str = "/home/ubuntu/kit-sdk-110.1.3/nchc.khl.editor.full.sh"
    cwd: str = str(REPO_ROOT)
    kit_executable: str = "/home/ubuntu/kit-sdk-110.1.3/kit"
    arguments: tuple[str, ...] = ()
    log_roots: tuple[str, ...] = ("/home/ubuntu/.nvidia-omniverse/logs",)
    capture_root: str = str(Path.home() / "kit-ai/lab/launches")
    bridge_url: str = "http://127.0.0.1:8011"
    readiness_timeout: float = 180
    shutdown_timeout: float = 30
    force_timeout: float = 5
    probe_timeout: float = 2
    poll_interval: float = 1

    def validate(self) -> None:
        if self.kit_id != KIT_ID:
            raise LifecycleError("INVALID_CONFIG", f"Only {KIT_ID} is supported")
        for name in ("launcher", "cwd", "kit_executable", "capture_root"):
            value = getattr(self, name)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise LifecycleError("INVALID_CONFIG", f"{name} must be an absolute path")
        for name in ("launcher", "kit_executable"):
            path = Path(getattr(self, name))
            if not path.is_file() or not os.access(path, os.R_OK | os.X_OK):
                raise LifecycleError("INVALID_CONFIG", f"{name} must be a readable executable file")
        if not Path(self.cwd).is_dir() or not os.access(self.cwd, os.X_OK):
            raise LifecycleError("INVALID_CONFIG", "cwd must be an accessible directory")
        if not isinstance(self.arguments, (list, tuple)) or any(
            not isinstance(a, str) or "\0" in a for a in self.arguments
        ):
            raise LifecycleError("INVALID_CONFIG", "arguments must be an array of strings")
        if not isinstance(self.log_roots, (list, tuple)) or not self.log_roots or any(
            not isinstance(p, str) or not Path(p).is_absolute() for p in self.log_roots
        ):
            raise LifecycleError("INVALID_CONFIG", "log_roots must contain absolute paths")
        if Path(self.capture_root).resolve().is_relative_to(REPO_ROOT):
            raise LifecycleError("INVALID_CONFIG", "Launch output must remain outside the repository")
        for name in ("readiness_timeout", "shutdown_timeout", "force_timeout", "probe_timeout", "poll_interval"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 3600:
                raise LifecycleError("INVALID_CONFIG", f"{name} must be in (0, 3600] seconds")
        # Lifecycle never follows the generic client's remote-target opt-in.
        from urllib.parse import urlsplit
        url = urlsplit(self.bridge_url)
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1", "localhost"}
                or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}):
            raise LifecycleError("INVALID_CONFIG", "bridge_url must be a plain loopback HTTP origin")
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise LifecycleError("UNSUPPORTED_PLATFORM", "Linux pidfd support is required for safe stop")


def config_path() -> Path:
    return Path(os.environ.get("KIT_LAB_LIFECYCLE_CONFIG", str(DEFAULT_CONFIG))).expanduser()


def load_config(path: Path | None = None) -> LifecycleConfig:
    path = path or config_path()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            metadata = os.fstat(stream.fileno())
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
                raise LifecycleError("UNSAFE_CONFIG", "Lifecycle config must be user-owned and not group/world writable")
            data = json.load(stream)
        if not isinstance(data, dict) or data.pop("schema_version", None) != 1:
            raise ValueError("Unsupported config schema")
        config = LifecycleConfig(**data)
        config.validate()
        return config
    except FileNotFoundError as exc:
        raise LifecycleError("NOT_CONFIGURED", "Run lifecycle-user-local.sh setup once") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise LifecycleError("INVALID_CONFIG", f"Cannot load lifecycle config: {type(exc).__name__}") from exc


def save_config(config: LifecycleConfig, path: Path | None = None) -> None:
    config.validate()
    path = path or config_path()
    if not path.is_absolute() or path.resolve().is_relative_to(REPO_ROOT):
        raise LifecycleError("INVALID_CONFIG", "Machine configuration must be absolute and outside Git")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".kit-lifecycle-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"schema_version": 1, **asdict(config)}, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class KitProcess:
    pid: int
    uid: int
    start_ticks: int
    executable: str


@dataclass
class Discovery:
    matches: list[KitProcess]
    untagged: list[int]
    denied: list[int]

    def require_inspection(self) -> None:
        if self.denied:
            raise LifecycleError("INSPECTION_DENIED", f"Cannot inspect candidate Kit environments: {self.denied}")
        if len(self.matches) > 1:
            raise LifecycleError("AMBIGUOUS", f"Multiple Kit processes carry {KIT_ID}; no process selected")


class LinuxProcesses:
    def __init__(self, executable: str, proc_root: Path = Path("/proc")):
        self.executable = str(Path(executable).resolve())
        self.proc_root = proc_root

    def inspect(self, pid: int) -> tuple[KitProcess, bool] | None:
        path = self.proc_root / str(pid)
        try:
            executable = os.readlink(path / "exe")
        except PermissionError:
            # Reading comm only selects candidates when exe is inaccessible; never identifies Kit.
            if (path / "comm").read_text().strip() == Path(self.executable).name:
                raise
            return None
        if executable != self.executable:
            return None
        before = (path / "stat").read_text().rsplit(")", 1)[1].split()
        if before[0] == "Z":
            return None
        with (path / "environ").open("rb") as stream:
            raw = stream.read(MAX_ENVIRONMENT_BYTES + 1)
        if len(raw) > MAX_ENVIRONMENT_BYTES:
            raise PermissionError("Candidate environment exceeds inspection bound")
        # Do not build, expose, persist, or log the environment dictionary.
        identity = b"KHL_KIT_ID=" + KIT_ID.encode()
        tagged = identity in raw.split(b"\0")
        metadata = KitProcess(pid, path.stat().st_uid, int(before[19]), executable)
        after = (path / "stat").read_text().rsplit(")", 1)[1].split()
        if after[0] == "Z" or after[19] != before[19] or os.readlink(path / "exe") != executable:
            raise ProcessLookupError("Candidate changed during inspection")
        return metadata, tagged

    def discover(self) -> Discovery:
        matches, untagged, denied = [], [], []
        for path in self.proc_root.iterdir():
            if not path.name.isdecimal():
                continue
            pid = int(path.name)
            try:
                found = self.inspect(pid)
                if found:
                    (matches if found[1] else untagged).append(found[0] if found[1] else pid)
            except (FileNotFoundError, ProcessLookupError):
                continue  # A process can legitimately exit during enumeration.
            except (PermissionError, OSError):
                denied.append(pid)
        return Discovery(matches, untagged, denied)

    def validate(self, process: KitProcess) -> None:
        discovery = self.discover()
        discovery.require_inspection()
        if discovery.matches != [process]:
            raise LifecycleError("PROCESS_CHANGED", "Kit identity changed; refusing to signal the previous process")

    def open_handle(self, process: KitProcess) -> int:
        self.validate(process)
        if process.uid != os.getuid():
            raise LifecycleError("PROCESS_PERMISSION", "Refusing to signal a Kit running under another account")
        handle = os.pidfd_open(process.pid)
        try:
            self.validate(process)
            return handle
        except BaseException:
            os.close(handle)
            raise

    def send_signal(self, handle: int, process: KitProcess, sig: int) -> None:
        self.validate(process)
        signal.pidfd_send_signal(handle, sig)

    def exited(self, handle: int) -> bool:
        return bool(select.select([handle], [], [], 0)[0])

    def age(self, process: KitProcess) -> float:
        uptime = float((self.proc_root / "uptime").read_text().split()[0])
        return max(0, uptime - process.start_ticks / os.sysconf("SC_CLK_TCK"))

    def open_log_paths(self, process: KitProcess, config: LifecycleConfig) -> dict[str, Any]:
        self.validate(process)
        candidates, captured = set(), set()
        fd_root = self.proc_root / str(process.pid) / "fd"
        try:
            for entry in fd_root.iterdir():
                try:
                    target = os.readlink(entry)
                except FileNotFoundError:
                    continue
                allowed = permitted_path(target, config.log_roots)
                if allowed and Path(allowed).name.startswith("kit_") and allowed.endswith(".log"):
                    candidates.add(allowed)
                capture = permitted_path(target, (config.capture_root,))
                if entry.name in {"1", "2"} and capture:
                    captured.add(capture)
        except PermissionError as exc:
            raise LifecycleError("LOG_ASSOCIATION_DENIED", "Cannot inspect Kit's open log descriptors") from exc
        self.validate(process)
        return {"native_candidates": sorted(candidates), "launch_output_paths": sorted(captured)}


def permitted_path(value: Any, roots: tuple[str, ...]) -> str | None:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        return None
    if value.endswith(" (deleted)"):
        return None
    path = Path(value).resolve()
    return str(path) if any(path.is_relative_to(Path(root).resolve()) for root in roots) else None


@contextlib.contextmanager
def mutation_lock():
    # Advisory serialization only. No PID, ownership or process registry is persisted.
    directory = Path(tempfile.gettempdir()) / f"khl-kit-lifecycle-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    metadata = directory.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077):
        raise LifecycleError("UNSAFE_LOCK", "Lifecycle lock directory must be private and user-owned")
    fd = os.open(directory / f"{KIT_ID}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LifecycleError("OPERATION_IN_PROGRESS", "Another lifecycle operation is in progress") from exc
        yield
    finally:
        os.close(fd)


class KitLifecycle:
    def __init__(self, config: LifecycleConfig, *, processes=None, client=None):
        self.config = config
        self.processes = processes or LinuxProcesses(config.kit_executable)
        self.client = client or KitLabClient(config.bridge_url, config.probe_timeout)
        self._children: list[subprocess.Popen] = []  # Reaping only; never discovery/authorization.

    async def configuration(self) -> dict[str, Any]:
        discovery = await asyncio.to_thread(self.processes.discover)
        return {"configured": True, "kit_id": KIT_ID, "config": asdict(self.config),
                "inspection": {"account_uid": os.getuid(), "denied_candidates": discovery.denied,
                               "identified_count": len(discovery.matches), "untagged_kit_pids": discovery.untagged}}

    async def status(self) -> dict[str, Any]:
        discovery = await asyncio.to_thread(self.processes.discover)
        result: dict[str, Any] = {"kit_id": KIT_ID, "identified_count": len(discovery.matches),
                                  "untagged_kit_pids": discovery.untagged}
        if discovery.denied:
            return {**result, "state": "INSPECTION_DENIED", "denied_candidates": discovery.denied}
        if len(discovery.matches) > 1:
            return {**result, "state": "AMBIGUOUS", "candidate_pids": [p.pid for p in discovery.matches]}
        if not discovery.matches:
            return {**result, "state": "STOPPED", "process": None}
        process = discovery.matches[0]
        result["process"] = asdict(process)
        try:
            response = await self.client.get("/khl/lab/runtime/identity")
            identity = response.get("result", {})
            matching = (response.get("ok") is True and identity.get("kit_id") == KIT_ID
                        and identity.get("pid") == process.pid
                        and identity.get("start_ticks") == process.start_ticks)
            await asyncio.to_thread(self.processes.validate, process)
            if not matching:
                return {**result, "state": "UNRESPONSIVE", "reason": "BRIDGE_IDENTITY_MISMATCH"}
            if identity.get("ready") is True:
                return {**result, "state": "READY", "bridge_api": identity.get("api_version"),
                        "native_log_path": permitted_path(identity.get("native_log_path"), self.config.log_roots)}
            reason = "APP_NOT_READY"
        except KitLabClientError:
            reason = "BRIDGE_UNAVAILABLE"
        except LifecycleError:
            return {**result, "state": "PROCESS_CHANGED", "reason": "Rediscover Kit; process changed during probe"}
        try:
            await asyncio.to_thread(self.processes.validate, process)
            age = await asyncio.to_thread(self.processes.age, process)
        except (LifecycleError, ProcessLookupError, FileNotFoundError):
            return {**result, "state": "PROCESS_CHANGED", "reason": "Process changed during probe"}
        return {**result, "state": "STARTING" if age < self.config.readiness_timeout else "UNRESPONSIVE",
                "reason": reason, "age_seconds": round(age, 2)}

    async def log_paths(self) -> dict[str, Any]:
        status = await self.status()
        result = {**status, "native_log_path": None, "native_candidates": [],
                  "launch_output_paths": [], "association": "unavailable", "contents_read": False}
        if "process" not in status or not status["process"]:
            return result
        process = KitProcess(**status["process"])
        try:
            paths = await asyncio.to_thread(self.processes.open_log_paths, process, self.config)
            result.update(paths)
        except LifecycleError as exc:
            result["descriptor_error"] = exc.code
            if exc.code != "LOG_ASSOCIATION_DENIED":
                return {**result, "state": exc.code}
        except (ProcessLookupError, FileNotFoundError):
            return {**result, "state": "PROCESS_CHANGED"}
        try:
            await asyncio.to_thread(self.processes.validate, process)
        except LifecycleError as exc:
            return {**result, "state": exc.code, "native_candidates": [], "launch_output_paths": []}
        if status.get("native_log_path"):
            result.update(native_log_path=status["native_log_path"], association="matching_bridge_identity")
            result["open_descriptor_confirmed"] = status["native_log_path"] in result["native_candidates"]
        elif len(result["native_candidates"]) == 1:
            result.update(native_log_path=result["native_candidates"][0], association="identified_process_open_descriptor")
        elif len(result["native_candidates"]) > 1:
            result["association"] = "ambiguous_open_descriptors"
        return result

    async def _progress(self, callback: Progress | None, elapsed: float, total: float, state: str):
        if callback:
            await callback(min(elapsed, total), total, f"{KIT_ID}: {state} ({elapsed:.0f}/{total:g}s)")

    async def _wait_ready(self, timeout: float, progress: Progress | None,
                          expected: KitProcess | None = None) -> dict[str, Any]:
        started = time.monotonic()
        while True:
            status = await self.status()
            if expected and status.get("process") and status["process"] != asdict(expected):
                return {**status, "state": "PROCESS_CHANGED", "reason": "Kit changed during readiness wait"}
            elapsed = time.monotonic() - started
            await self._progress(progress, elapsed, timeout, status["state"])
            if status["state"] in {"READY", "AMBIGUOUS", "INSPECTION_DENIED", "STOPPED", "PROCESS_CHANGED"}:
                return status
            if elapsed >= timeout:
                return {**status, "state": "UNRESPONSIVE", "readiness_timed_out": True,
                        "process_preserved": True}
            await asyncio.sleep(min(self.config.poll_interval, timeout - elapsed))

    def _launch(self) -> str:
        # Reap completed children without using their PID as Kit's persistent identity.
        self._children = [child for child in self._children if child.poll() is None]
        root = Path(self.config.capture_root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        fd, path = tempfile.mkstemp(prefix=f"{KIT_ID}-{stamp}-", suffix=".log", dir=root)
        env = dict(os.environ)
        # Never inject the MCP virtualenv's modules or service credentials into Kit.
        for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "NVIDIA_API_KEY"):
            env.pop(key, None)
        env["KHL_KIT_ID"] = KIT_ID
        try:
            with os.fdopen(fd, "wb") as output:
                child = subprocess.Popen([self.config.launcher, *self.config.arguments], cwd=self.config.cwd,
                                         env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                         start_new_session=True)
            self._children.append(child)
        except OSError as exc:
            raise LifecycleError("LAUNCH_FAILED", f"Kit launcher failed: {type(exc).__name__}") from exc
        return path

    async def _start(self, timeout: float, progress: Progress | None) -> dict[str, Any]:
        self.config.validate()
        discovery = await asyncio.to_thread(self.processes.discover)
        discovery.require_inspection()
        if discovery.matches:
            return {**(await self.status()), "started": False, "reason": "ALREADY_RUNNING"}
        if discovery.untagged:
            raise LifecycleError("UNTAGGED_KIT", "An untagged Kit is running; restart it through the identity-aware launcher manually")
        # A listening old/unrelated bridge must not be displaced by a new launch.
        try:
            await self.client.get("/khl/lab/status")
        except KitLabClientError:
            pass
        else:
            raise LifecycleError("UNIDENTIFIED_BRIDGE", "A Kit bridge responds without a matching OS identity")
        # Recheck after the HTTP preflight. Manual launchers do not share our advisory lock.
        discovery = await asyncio.to_thread(self.processes.discover)
        discovery.require_inspection()
        if discovery.matches:
            return {**(await self.status()), "started": False, "reason": "ALREADY_RUNNING"}
        if discovery.untagged:
            raise LifecycleError("UNTAGGED_KIT", "An untagged Kit appeared during launch preflight")
        # Keep resource acquisition synchronous so task cancellation cannot release the lock
        # while a background launch or pidfd acquisition is still running.
        output_path = self._launch()
        started = time.monotonic()
        # Give the approved foreground launcher time to exec Kit, without treating its PID as Kit identity.
        while True:
            discovery = await asyncio.to_thread(self.processes.discover)
            discovery.require_inspection()
            if discovery.matches:
                break
            elapsed = time.monotonic() - started
            await self._progress(progress, elapsed, timeout, "STARTING")
            if self._children[-1].poll() is not None:
                return {"kit_id": KIT_ID, "state": "STOPPED", "started": False,
                        "reason": "LAUNCH_EXITED_WITHOUT_KIT", "launcher_exit_code": self._children[-1].returncode,
                        "launch_output_path": output_path}
            if elapsed >= timeout:
                return {"kit_id": KIT_ID, "state": "STARTING", "started": True,
                        "reason": "KIT_NOT_IDENTIFIED_YET", "launch_output_path": output_path,
                        "readiness_timed_out": True, "process_preserved": True}
            await asyncio.sleep(min(self.config.poll_interval, timeout - elapsed))
        result = await self._wait_ready(max(0.001, timeout - (time.monotonic() - started)), progress,
                                        discovery.matches[0])
        return {**result, "started": True, "launch_output_path": output_path}

    async def start(self, timeout: float | None = None, progress: Progress | None = None) -> dict[str, Any]:
        with mutation_lock():
            return await self._start(self._timeout(timeout), operation_progress(progress))

    def _timeout(self, timeout: float | None) -> float:
        timeout = self.config.readiness_timeout if timeout is None else timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
            raise LifecycleError("INVALID_TIMEOUT", "Readiness timeout must be in (0, 3600] seconds")
        return timeout

    async def _stop(self, force: bool, progress: Progress | None) -> dict[str, Any]:
        discovery = await asyncio.to_thread(self.processes.discover)
        discovery.require_inspection()
        if not discovery.matches:
            return {"kit_id": KIT_ID, "state": "STOPPED", "stopped": False,
                    "untagged_kit_pids": discovery.untagged}
        process = discovery.matches[0]
        handle = self.processes.open_handle(process)
        forced = False
        try:
            self.processes.send_signal(handle, process, signal.SIGTERM)
            for sig, timeout in ((signal.SIGTERM, self.config.shutdown_timeout), (signal.SIGKILL, self.config.force_timeout)):
                if sig == signal.SIGKILL:
                    if not force:
                        return {"kit_id": KIT_ID, "state": "UNRESPONSIVE", "shutdown_timed_out": True,
                                "process_preserved": True, "process": asdict(process), "stopped": False}
                    self.processes.send_signal(handle, process, sig)
                    forced = True
                started = time.monotonic()
                while not self.processes.exited(handle):
                    elapsed = time.monotonic() - started
                    if elapsed >= timeout:
                        break
                    await self._progress(progress, elapsed, timeout, "STOPPING")
                    await asyncio.sleep(min(self.config.poll_interval, timeout - elapsed))
                if self.processes.exited(handle):
                    remaining = await asyncio.to_thread(self.processes.discover)
                    remaining.require_inspection()
                    if remaining.matches:
                        return {"kit_id": KIT_ID, "state": "PROCESS_CHANGED", "stopped": True,
                                "reason": "Another matching Kit appeared; it was not signalled"}
                    return {"kit_id": KIT_ID, "state": "STOPPED", "stopped": True, "forced": forced}
            return {"kit_id": KIT_ID, "state": "UNRESPONSIVE", "stopped": False, "forced": forced,
                    "shutdown_timed_out": True}
        finally:
            os.close(handle)

    async def stop(self, force: bool = False, progress: Progress | None = None) -> dict[str, Any]:
        with mutation_lock():
            return await self._stop(force, operation_progress(progress))

    async def restart(self, force: bool = False, timeout: float | None = None,
                      progress: Progress | None = None) -> dict[str, Any]:
        with mutation_lock():
            progress = operation_progress(progress)
            readiness = self._timeout(timeout)
            stopped = await self._stop(force, progress)
            if stopped["state"] != "STOPPED":
                return {**stopped, "restarted": False}
            return {**(await self._start(readiness, progress)), "previous_stop": stopped, "restarted": True}
