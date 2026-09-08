import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from khl_kit_lab_mcp.client import KitLabClientError
from khl_kit_lab_mcp.lifecycle import (
    KIT_ID, Discovery, KitLifecycle, KitProcess, LifecycleConfig, LifecycleError,
    LinuxProcesses, load_config, mutation_lock, permitted_path, save_config,
)


class IdentityClient:
    def __init__(self, processes, native_log=None):
        self.processes = processes
        self.native_log = native_log
        self.respond = True
        self.wrong_pid = False

    async def get(self, path):
        if path == "/khl/lab/status" or not self.respond:
            raise KitLabClientError("fixture has no HTTP server")
        matches = self.processes.discover().matches
        if not matches:
            raise KitLabClientError("fixture stopped")
        process = matches[0]
        return {"ok": True, "result": {"kit_id": KIT_ID, "pid": -1 if self.wrong_pid else process.pid,
                "start_ticks": process.start_ticks, "ready": True, "api_version": "0.4.0",
                "native_log_path": self.native_log}}


class ProcessDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.processes = LinuxProcesses("/fixture/kit", self.root)

    def candidate(self, pid, identity=KIT_ID, executable="/fixture/kit", start=20):
        path = self.root / str(pid)
        path.mkdir()
        (path / "exe").symlink_to(executable)
        (path / "comm").write_text("kit\n")
        (path / "stat").write_text(f"{pid} (kit name) S " + "0 " * 18 + str(start))
        (path / "environ").write_bytes(b"PRIVATE_FIXTURE=not-output\0" +
                                      (f"KHL_KIT_ID={identity}\0".encode() if identity else b""))
        return path

    def test_zero_one_multiple_and_unrelated_candidates(self):
        self.assertEqual(self.processes.discover().matches, [])
        self.candidate(10)
        self.candidate(11, identity=None)
        self.candidate(12, identity=KIT_ID + "-other")
        self.candidate(13, executable="/fixture/python")  # Inherited ID alone is not enough.
        found = self.processes.discover()
        self.assertEqual([p.pid for p in found.matches], [10])
        self.assertEqual(set(found.untagged), {11, 12})
        self.assertNotIn("PRIVATE_FIXTURE", repr(found))
        self.candidate(14)
        with self.assertRaisesRegex(LifecycleError, "Multiple Kit"):
            self.processes.discover().require_inspection()

    def test_permission_denial_is_never_stopped(self):
        self.candidate(10)
        with patch.object(self.processes, "inspect", side_effect=PermissionError()):
            found = self.processes.discover()
        self.assertEqual(found.denied, [10])
        with self.assertRaises(LifecycleError) as error:
            found.require_inspection()
        self.assertEqual(error.exception.code, "INSPECTION_DENIED")

    def test_pid_reuse_and_identity_loss_are_rejected(self):
        path = self.candidate(10)
        process = self.processes.discover().matches[0]
        (path / "stat").write_text("10 (kit) S " + "0 " * 18 + "21")
        with self.assertRaises(LifecycleError):
            self.processes.validate(process)
        (path / "environ").write_bytes(b"KHL_KIT_ID=someone-else\0")
        with self.assertRaises(LifecycleError):
            self.processes.validate(process)

    def test_open_log_descriptors_associate_paths_without_reading_contents(self):
        path = self.candidate(10)
        native = self.root / "native/kit_20260908.log"
        capture = self.root / "capture/output.log"
        (path / "fd").mkdir()
        (path / "fd/15").symlink_to(native)
        (path / "fd/1").symlink_to(capture)
        (path / "fd/2").symlink_to(capture)
        (path / "fd/20").symlink_to("/elsewhere/kit_wrong.log")
        config = LifecycleConfig(log_roots=(str(native.parent),), capture_root=str(capture.parent))
        result = self.processes.open_log_paths(self.processes.discover().matches[0], config)
        self.assertEqual(result["native_candidates"], [str(native)])
        self.assertEqual(result["launch_output_paths"], [str(capture)])
        self.assertFalse(native.exists())  # No content/existence requirement or mtime selection.

    def test_symlink_path_cannot_escape_log_root(self):
        (self.root / "escape").symlink_to("/outside")
        self.assertIsNone(permitted_path(str(self.root / "escape/kit.log"), (str(self.root),)))


class LifecycleProcessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.exe = self.root / "kit-fixture"
        shutil.copyfile("/bin/sleep", self.exe)
        self.exe.chmod(0o700)
        self.launcher = self.root / "launcher.sh"
        self.launcher.write_text(f'#!/bin/sh\nexec "{self.exe}" "$@"\n')
        self.launcher.chmod(0o700)
        self.config = LifecycleConfig(launcher=str(self.launcher), cwd=str(self.root),
            kit_executable=str(self.exe), arguments=("60",), capture_root=str(self.root / "capture"),
            log_roots=(str(self.root / "native"),), readiness_timeout=.15,
            shutdown_timeout=.05, force_timeout=.1, probe_timeout=.01, poll_interval=.005)
        self.proc = LinuxProcesses(str(self.exe))
        self.client = IdentityClient(self.proc, str(self.root / "native/kit_fixture.log"))
        self.lifecycle = KitLifecycle(self.config, processes=self.proc, client=self.client)
        self.manual_children = []

    def tearDown(self):
        for process in self.proc.discover().matches:
            try:
                os.kill(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for child in self.manual_children + self.lifecycle._children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
        self.tmp.cleanup()

    def manual_start(self, tagged=True):
        env = dict(os.environ)
        env.pop("KHL_KIT_ID", None)
        if tagged:
            env["KHL_KIT_ID"] = KIT_ID
        child = subprocess.Popen([str(self.exe), "60"], env=env)
        self.manual_children.append(child)
        return child

    async def test_start_injects_id_structured_arguments_and_preserves_existing(self):
        result = await self.lifecycle.start()
        self.assertEqual(result["state"], "READY")
        found = self.proc.discover().matches
        self.assertEqual(len(found), 1)
        self.assertTrue(Path(result["launch_output_path"]).is_file())
        self.assertEqual(Path(result["launch_output_path"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual((await self.lifecycle.start())["started"], False)
        self.assertEqual(self.proc.discover().matches, found)

    async def test_kit_before_mcp_and_mcp_before_kit(self):
        self.assertEqual((await self.lifecycle.status())["state"], "STOPPED")
        child = self.manual_start()  # MCP object already exists.
        self.assertEqual((await self.lifecycle.status())["process"]["pid"], child.pid)
        new_adapter = KitLifecycle(self.config, processes=self.proc, client=self.client)
        self.assertEqual((await new_adapter.status())["state"], "READY")  # Kit predates this MCP.
        self.assertEqual(new_adapter._children, [])
        self.assertTrue((await new_adapter.stop())["stopped"])

    async def test_manual_wrapper_restart_changes_pid_not_identity(self):
        self.launcher.write_text(f'#!/bin/sh\nexport KHL_KIT_ID="{KIT_ID}"\nexec "{self.exe}" 60\n')
        first = subprocess.Popen([str(self.launcher)])
        self.manual_children.append(first)
        await asyncio.sleep(.02)
        first_status = await self.lifecycle.status()
        self.assertEqual(first_status["state"], "READY")
        first.terminate()
        first.wait(timeout=2)
        second = subprocess.Popen([str(self.launcher)])
        self.manual_children.append(second)
        await asyncio.sleep(.02)
        second_status = await self.lifecycle.status()
        self.assertEqual(second_status["state"], "READY")
        self.assertNotEqual(first_status["process"]["pid"], second_status["process"]["pid"])
        self.assertEqual(first_status["kit_id"], second_status["kit_id"])
        self.assertEqual(self.lifecycle._children, [])  # Discovery never required launching either Kit.

    async def test_unresponsive_process_is_identifiable_timeout_preserves_it(self):
        self.client.respond = False
        progress = AsyncMock()
        result = await self.lifecycle.start(timeout=.04, progress=progress)
        self.assertEqual(result["state"], "UNRESPONSIVE")
        self.assertTrue(result["readiness_timed_out"])
        self.assertTrue(result["process_preserved"])
        self.assertEqual(len(self.proc.discover().matches), 1)
        self.assertGreater(progress.await_count, 0)
        with patch.object(self.proc, "age", return_value=999):
            self.assertEqual((await self.lifecycle.status())["state"], "UNRESPONSIVE")
        self.assertTrue((await self.lifecycle.stop())["stopped"])  # Stop requires no HTTP.

    async def test_ambiguity_and_unrelated_process_refusal(self):
        untagged = self.manual_start(tagged=False)
        self.assertEqual((await self.lifecycle.status())["state"], "STOPPED")
        self.assertFalse((await self.lifecycle.stop())["stopped"])
        self.assertIsNone(untagged.poll())
        with self.assertRaises(LifecycleError) as error:
            await self.lifecycle.start()
        self.assertEqual(error.exception.code, "UNTAGGED_KIT")
        self.manual_start()
        self.manual_start()
        self.assertEqual((await self.lifecycle.status())["state"], "AMBIGUOUS")
        for operation in (self.lifecycle.start, self.lifecycle.stop, self.lifecycle.restart):
            with self.assertRaises(LifecycleError) as error:
                await operation()
            self.assertEqual(error.exception.code, "AMBIGUOUS")
        self.assertTrue(all(p.poll() is None for p in self.manual_children))

    async def test_bridge_pid_mismatch_is_not_ready(self):
        self.manual_start()
        self.client.wrong_pid = True
        result = await self.lifecycle.status()
        self.assertEqual(result["state"], "UNRESPONSIVE")
        self.assertEqual(result["reason"], "BRIDGE_IDENTITY_MISMATCH")

    async def test_process_exit_during_failed_probe_and_log_association(self):
        child = self.manual_start()
        async def exit_during_probe(path):
            child.terminate()
            child.wait(timeout=2)
            raise KitLabClientError("process exited")
        with patch.object(self.client, "get", side_effect=exit_during_probe):
            self.assertEqual((await self.lifecycle.status())["state"], "PROCESS_CHANGED")
        self.manual_start()
        with patch.object(self.proc, "open_log_paths", side_effect=LifecycleError("PROCESS_CHANGED", "fixture")):
            result = await self.lifecycle.log_paths()
        self.assertEqual(result["state"], "PROCESS_CHANGED")
        self.assertIsNone(result["native_log_path"])

    async def test_failed_launcher_does_not_claim_running_kit(self):
        self.launcher.write_text('#!/bin/sh\nexit 7\n')
        result = await self.lifecycle.start()
        self.assertEqual(result["state"], "STOPPED")
        self.assertEqual(result["launcher_exit_code"], 7)
        self.assertFalse(result["started"])

    async def test_manual_launch_during_preflight_does_not_create_second_kit(self):
        async def preflight(path):
            if path == "/khl/lab/status":
                self.manual_start()
                raise KitLabClientError("bridge starting")
            return await IdentityClient.get(self.client, path)
        with patch.object(self.client, "get", side_effect=preflight):
            result = await self.lifecycle.start()
        self.assertFalse(result["started"])
        self.assertEqual(len(self.proc.discover().matches), 1)
        self.assertEqual(self.lifecycle._children, [])

    async def test_replacement_during_readiness_is_explicit(self):
        first = self.manual_start()
        expected = self.proc.discover().matches[0]
        first.terminate()
        first.wait(timeout=2)
        self.manual_start()
        result = await self.lifecycle._wait_ready(.1, None, expected)
        self.assertEqual(result["state"], "PROCESS_CHANGED")

    async def test_restart_confirms_exit_and_preserves_stable_id(self):
        first = await self.lifecycle.start()
        progress = AsyncMock()
        second = await self.lifecycle.restart(progress=progress)
        self.assertEqual(second["state"], "READY")
        self.assertTrue(second["previous_stop"]["stopped"])
        self.assertNotEqual(first["process"]["pid"], second["process"]["pid"])
        self.assertEqual(first["kit_id"], second["kit_id"])
        positions = [call.args[0] for call in progress.await_args_list]
        self.assertGreater(len(positions), 1)
        self.assertEqual(positions, sorted(set(positions)))
        self.assertTrue(all(call.args[1] is None for call in progress.await_args_list))

    async def test_graceful_timeout_requires_explicit_force_and_blocks_restart(self):
        self.launcher.write_text(f'#!/bin/sh\ntrap "" TERM\nexec "{self.exe}" "$@"\n')
        await self.lifecycle.start()
        process = self.proc.discover().matches[0]
        result = await self.lifecycle.restart()
        self.assertFalse(result["restarted"])
        self.assertTrue(result["process_preserved"])
        self.assertEqual(self.proc.discover().matches, [process])
        result = await self.lifecycle.stop(force=True)
        self.assertEqual(result["state"], "STOPPED")
        self.assertTrue(result["forced"])

    async def test_native_log_paths_use_matching_process_and_stdout_descriptor(self):
        await self.lifecycle.start()
        result = await self.lifecycle.log_paths()
        self.assertEqual(result["association"], "matching_bridge_identity")
        self.assertEqual(result["native_log_path"], self.client.native_log)
        self.assertEqual(len(result["launch_output_paths"]), 1)
        self.assertFalse(result["contents_read"])
        self.client.native_log = "/outside/kit_other.log"
        result = await self.lifecycle.log_paths()
        self.assertIsNone(result["native_log_path"])

    async def test_identity_change_before_signal_is_refused(self):
        await self.lifecycle.start()
        with patch.object(self.proc, "validate", side_effect=LifecycleError("PROCESS_CHANGED", "fixture")), \
                patch("signal.pidfd_send_signal") as signal_call:
            with self.assertRaises(LifecycleError):
                await self.lifecycle.stop()
        signal_call.assert_not_called()

    async def test_read_only_status_surfaces_process_permission_failure(self):
        with patch.object(self.proc, "discover", return_value=Discovery([], [], [123])):
            self.assertEqual((await self.lifecycle.status())["state"], "INSPECTION_DENIED")
            with self.assertRaises(LifecycleError):
                await self.lifecycle.stop()

    def test_setup_round_trip_and_no_pid_registry(self):
        path = self.root / "machine.json"
        save_config(self.config, path)
        loaded = load_config(path)
        self.assertEqual(loaded.kit_id, KIT_ID)
        self.assertEqual(loaded.arguments, ["60"])
        self.assertNotIn("pid", path.read_text().lower())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(LifecycleError):
            replace(self.config, arguments="--unsafe shell").validate()
        with self.assertRaises(LifecycleError):
            replace(self.config, bridge_url="http://example.com").validate()

    def test_mutations_are_serialized_across_independent_controllers(self):
        with mutation_lock():
            with self.assertRaises(LifecycleError) as error:
                with mutation_lock():
                    pass
            self.assertEqual(error.exception.code, "OPERATION_IN_PROGRESS")


if __name__ == "__main__":
    unittest.main()
