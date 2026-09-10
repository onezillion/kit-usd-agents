import asyncio
import importlib.util
import re
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


STAGE_E = (Path(__file__).resolve().parents[3] / "extensions/omni.khl.kit_lab"
           / "omni/khl/kit_lab/stage_e.py")
spec = importlib.util.spec_from_file_location("isolated_stage_e", STAGE_E)
stage_e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage_e)


def split_id(value):
    match = re.fullmatch(r"(.+)-(\d+(?:\.\d+){0,3})", value)
    return (match.group(1), match.group(2)) if match else (value, "")


class FakeManager:
    def __init__(self):
        self.entries = [
            {"id": "omni.khl.kit_lab-0.5.0", "name": "omni.khl.kit_lab", "version": (0, 5, 0), "path": "/repo/lab"},
            {"id": "omni.services.core-1.0.0", "name": "omni.services.core", "version": (1, 0, 0), "path": "/kit/core"},
            {"id": "demo.tool-1.0.0", "name": "demo.tool", "version": (1, 0, 0), "path": "/kit/demo"},
            {"id": "demo.tool-beta-2.0.0", "name": "demo.tool-beta", "version": (2, 0, 0), "path": "/kit/beta"},
        ]
        self.enabled = {
            "omni.khl.kit_lab": "omni.khl.kit_lab-0.5.0",
            "omni.services.core": "omni.services.core-1.0.0",
        }
        self.configs = {
            "omni.khl.kit_lab-0.5.0": self.config("omni.khl.kit_lab", ["omni.services.core-1.0.0"]),
            "omni.services.core-1.0.0": self.config("omni.services.core", []),
            "demo.tool-1.0.0": self.config("demo.tool", []),
            "demo.tool-beta-2.0.0": self.config("demo.tool-beta", []),
        }
        self.calls = []
        self.solver_calls = []

    @staticmethod
    def config(name, dependencies, reloadable=True):
        return {
            "package": {"name": name, "toggleable": True},
            "state": {"dependencies": tuple(dependencies), "reloadable": reloadable, "failed": False},
            "dependencies": {},
        }

    def get_extensions(self):
        return tuple({**entry, "enabled": entry["name"] in self.enabled} for entry in self.entries)

    def get_extension_dict(self, extension_id):
        return self.configs.get(extension_id)

    def get_enabled_extension_id(self, name):
        return self.enabled.get(name)

    def is_extension_enabled(self, name):
        return name in self.enabled

    def solve_extensions(self, names, add_enabled=False, return_only_disabled=False):
        self.solver_calls.append((tuple(names), add_enabled, return_only_disabled))
        target = names[0]
        matches = [entry for entry in self.entries if entry["name"] == target]
        return bool(matches), matches, "" if matches else "missing"

    def set_extension_enabled_immediate(self, extension_id, enabled):
        self.calls.append((extension_id, enabled))
        entry = next(item for item in self.entries if item["id"] == extension_id)
        if enabled:
            self.enabled[entry["name"]] = extension_id
        else:
            self.enabled.pop(entry["name"], None)
        return True


def kit_modules(manager, app=None):
    omni = ModuleType("omni")
    omni.__path__ = []
    ext = ModuleType("omni.ext")
    ext.get_extension_name_and_version = split_id
    kit = ModuleType("omni.kit")
    kit.__path__ = []
    app_module = ModuleType("omni.kit.app")
    app = app or SimpleNamespace(
        get_extension_manager=lambda: manager,
        next_update_async=lambda: asyncio.sleep(0),
    )
    app_module.get_app = lambda: app
    omni.ext = ext
    omni.kit = kit
    kit.app = app_module
    return {"omni": omni, "omni.ext": ext, "omni.kit": kit, "omni.kit.app": app_module}


class ExtensionControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stage_e._capture_operations.clear()

    async def test_tag_parsing_local_resolution_noops_and_reload(self):
        manager = FakeManager()
        with patch.dict(sys.modules, kit_modules(manager)):
            tagged = stage_e.inspect_extension_impl(manager, "demo.tool-beta")
            self.assertEqual(tagged["selected_id"], "demo.tool-beta-2.0.0")
            self.assertEqual(manager.solver_calls[-1], (("demo.tool-beta",), True, False))
            enabled = await stage_e.extension_mutation("enable", "demo.tool")
            self.assertTrue(enabled["ok"])
            no_op = await stage_e.extension_mutation("enable", "demo.tool")
            self.assertTrue(no_op["result"]["no_op"])
            reloaded = await stage_e.extension_mutation("reload", "demo.tool")
            self.assertTrue(reloaded["ok"])
            self.assertEqual(manager.calls[-2:], [("demo.tool-1.0.0", False), ("demo.tool-1.0.0", True)])

    async def test_versioned_self_active_dependent_and_nonreloadable_refusals_have_no_side_effects(self):
        manager = FakeManager()
        manager.enabled["demo.tool"] = "demo.tool-1.0.0"
        manager.entries.append({"id": "demo.user-1.0.0", "name": "demo.user", "version": (1, 0, 0), "path": "/kit/user"})
        manager.configs["demo.user-1.0.0"] = manager.config("demo.user", ["demo.tool-1.0.0"])
        manager.enabled["demo.user"] = "demo.user-1.0.0"
        with patch.dict(sys.modules, kit_modules(manager)):
            invalid = await stage_e.extension_mutation("enable", "demo.tool-1.0.0")
            self.assertEqual(invalid["error"]["code"], "INVALID_EXTENSION_TARGET")
            dependent = await stage_e.extension_mutation("disable", "demo.tool")
            self.assertEqual(dependent["error"]["code"], "ACTIVE_DEPENDENTS")
            self_reload = await stage_e.extension_mutation("reload", "omni.khl.kit_lab")
            self.assertEqual(self_reload["error"]["code"], "SELF_RELOAD_UNSUPPORTED")
        self.assertEqual(manager.calls, [])

    async def test_incomplete_graph_and_nonreloadable_are_refused(self):
        manager = FakeManager()
        manager.enabled["demo.tool"] = "demo.tool-1.0.0"
        manager.configs["demo.tool-1.0.0"]["state"]["reloadable"] = False
        with patch.dict(sys.modules, kit_modules(manager)):
            refusal = await stage_e.extension_mutation("disable", "demo.tool")
            self.assertEqual(refusal["error"]["code"], "NON_RELOADABLE")
            del manager.configs["omni.services.core-1.0.0"]
            inspect = stage_e.inspect_extension_impl(manager, "demo.tool")
            self.assertFalse(inspect["graph_complete"])

    async def test_native_empty_dictionary_is_a_complete_leaf_dependency_state(self):
        manager = FakeManager()
        manager.configs["omni.services.core-1.0.0"]["state"]["dependencies"] = {}
        with patch.dict(sys.modules, kit_modules(manager)):
            inspected = stage_e.inspect_extension_impl(manager, "demo.tool")
        self.assertTrue(inspected["graph_complete"], inspected)

    async def test_absent_ambiguous_and_disabled_reload_are_refused_without_mutation(self):
        manager = FakeManager()
        manager.entries.append({
            "id": "demo.tool-2.0.0", "name": "demo.tool", "version": (2, 0, 0),
            "path": "/kit/demo2",
        })
        manager.configs["demo.tool-2.0.0"] = manager.config("demo.tool", [])
        with patch.dict(sys.modules, kit_modules(manager)):
            absent = await stage_e.extension_mutation("enable", "demo.absent")
            ambiguous = await stage_e.extension_mutation("enable", "demo.tool")
            reload_disabled = await stage_e.extension_mutation("reload", "demo.tool-beta")
        self.assertEqual(absent["error"]["code"], "EXTENSION_ABSENT")
        self.assertEqual(ambiguous["error"]["code"], "AMBIGUOUS_EXTENSION")
        self.assertEqual(reload_disabled["error"]["code"], "RELOAD_REQUIRES_ENABLED")
        self.assertEqual(manager.calls, [])

    async def test_reload_rediscovery_can_select_a_new_local_version(self):
        class RediscoveringManager(FakeManager):
            def set_extension_enabled_immediate(self, extension_id, enabled):
                result = super().set_extension_enabled_immediate(extension_id, enabled)
                if extension_id == "demo.tool-1.0.0" and not enabled:
                    self.entries = [item for item in self.entries if item["id"] != extension_id]
                    self.configs.pop(extension_id)
                    self.entries.append({
                        "id": "demo.tool-2.0.0", "name": "demo.tool",
                        "version": (2, 0, 0), "path": "/kit/demo2",
                    })
                    self.configs["demo.tool-2.0.0"] = self.config("demo.tool", [])
                return result

        manager = RediscoveringManager()
        manager.enabled["demo.tool"] = "demo.tool-1.0.0"
        with patch.dict(sys.modules, kit_modules(manager)):
            result = await stage_e.extension_mutation("reload", "demo.tool")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["before"]["enabled_id"], "demo.tool-1.0.0")
        self.assertEqual(result["result"]["after"]["enabled_id"], "demo.tool-2.0.0")

    async def test_reload_disappearing_candidate_restores_owned_disable(self):
        class DisappearingManager(FakeManager):
            def __init__(self):
                super().__init__()
                self.disabled_solves = 0

            def solve_extensions(self, names, add_enabled=False, return_only_disabled=False):
                if names == ["demo.tool"] and "demo.tool" not in self.enabled:
                    self.disabled_solves += 1
                    if self.disabled_solves == 2:
                        self.solver_calls.append((tuple(names), add_enabled, return_only_disabled))
                        return False, [], "candidate disappeared"
                return super().solve_extensions(names, add_enabled, return_only_disabled)

        manager = DisappearingManager()
        manager.enabled["demo.tool"] = "demo.tool-1.0.0"
        with patch.dict(sys.modules, kit_modules(manager)):
            result = await stage_e.extension_mutation("reload", "demo.tool")
        self.assertFalse(result["ok"])
        self.assertEqual(manager.enabled["demo.tool"], "demo.tool-1.0.0")
        self.assertTrue(result["result"]["rollback"]["complete"])

    async def test_reload_interference_restores_target_and_preserves_external_change(self):
        manager = FakeManager()
        manager.enabled["demo.tool"] = "demo.tool-1.0.0"
        updates = 0

        async def update():
            nonlocal updates
            updates += 1
            if updates == 1:
                manager.enabled["demo.tool-beta"] = "demo.tool-beta-2.0.0"
            await asyncio.sleep(0)

        app = SimpleNamespace(get_extension_manager=lambda: manager, next_update_async=update)
        with patch.dict(sys.modules, kit_modules(manager, app)):
            result = await stage_e.extension_mutation("reload", "demo.tool")
        self.assertFalse(result["ok"])
        self.assertEqual(manager.enabled["demo.tool"], "demo.tool-1.0.0")
        self.assertEqual(manager.enabled["demo.tool-beta"], "demo.tool-beta-2.0.0")
        self.assertTrue(result["result"]["rollback"]["complete"])

    async def test_startup_failure_rolls_back_only_owned_state(self):
        class FailingManager(FakeManager):
            def set_extension_enabled_immediate(self, extension_id, enabled):
                result = super().set_extension_enabled_immediate(extension_id, enabled)
                if extension_id == "demo.tool-1.0.0" and enabled:
                    self.configs[extension_id]["state"]["failed"] = True
                return result

        manager = FailingManager()
        with patch.dict(sys.modules, kit_modules(manager)):
            result = await stage_e.extension_mutation("enable", "demo.tool")
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"]["rollback"]["complete"], True)
        self.assertNotIn("demo.tool", manager.enabled)
        self.assertIn("omni.services.core", manager.enabled)

    async def test_concurrent_operations_are_serialized(self):
        manager = FakeManager()
        active = 0
        maximum = 0

        async def update():
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.005)
            active -= 1

        app = SimpleNamespace(
            get_extension_manager=lambda: manager,
            next_update_async=update,
        )
        with patch.dict(sys.modules, kit_modules(manager, app)):
            first, second = await asyncio.gather(
                stage_e.extension_mutation("enable", "demo.tool"),
                stage_e.extension_mutation("enable", "demo.tool-beta"),
            )
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(maximum, 1)

    async def test_within_update_interference_preserves_external_change_and_rolls_back_owned_target(self):
        manager = FakeManager()
        updates = 0

        async def update():
            nonlocal updates
            updates += 1
            if updates == 1:
                manager.enabled["demo.tool-beta"] = "demo.tool-beta-2.0.0"
            await asyncio.sleep(0)

        app = SimpleNamespace(get_extension_manager=lambda: manager, next_update_async=update)
        with patch.dict(sys.modules, kit_modules(manager, app)):
            result = await stage_e.extension_mutation("enable", "demo.tool")
        self.assertFalse(result["ok"])
        self.assertNotIn("demo.tool", manager.enabled)
        self.assertEqual(manager.enabled["demo.tool-beta"], "demo.tool-beta-2.0.0")
        rollback = result["result"]["rollback"]
        self.assertTrue(rollback["complete"], rollback)
        self.assertIn("demo.tool-beta", rollback["external_changes_preserved"])


class FakeSettings:
    def __init__(self, save=False):
        self.values = {"/plugins/carb.profiler-cpu.plugin/saveProfile": save}

    def get(self, path):
        return self.values.get(path)

    def get_settings_dictionary(self, path):
        return object() if path in self.values else None

    def set(self, path, value):
        self.values[path] = value

    def destroy_item(self, path):
        self.values.pop(path, None)


class FakeEvents:
    def __init__(self, profiler):
        self.profiler = profiler

    def get_profile_thread_ids(self):
        return (11,)

    def get_main_thread_id(self):
        return 11

    def get_profile_events(self, thread_id=0):
        return tuple(self.profiler.events)


class FakeMonitor:
    def __init__(self, profiler):
        self.profiler = profiler
        self.marked = False

    def mark_frame_end(self):
        self.marked = True

    def get_last_profile_events(self):
        return FakeEvents(self.profiler)


class FakeProfiler:
    def __init__(self):
        self.mask = 0
        self.python = False
        self.events = []

    def get_capture_mask(self): return self.mask
    def set_capture_mask(self, value): self.mask = value
    def is_python_profiling_enabled(self): return self.python
    def set_python_profiling_enabled(self, value): self.python = value
    def ensure_thread(self): pass
    def begin(self, mask, name): self.events.append({"name": name, "duration": 0.1, "indent": 0})
    def end(self, mask): pass


class ProfilerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stage_e._capture_operations.clear()

    def test_association_scans_non_main_threads_with_global_bounds(self):
        marker = "KHL_STAGE_E_non_main"

        class ThreadedEvents:
            rows = {
                11: tuple({"name": f"main-{index}"} for index in range(stage_e.MAX_CAPTURE_EVENTS)),
                22: ({"name": marker, "duration": 0.25},),
            }

            def get_profile_thread_ids(self):
                return (11, 22)

            def get_main_thread_id(self):
                return 11

            def get_profile_events(self, thread_id=0):
                return self.rows[thread_id]

        result = stage_e._collect_events(ThreadedEvents(), marker)
        self.assertTrue(result["marker_found"])
        self.assertEqual(result["marker_thread_id"], 22)
        self.assertEqual(len(result["events"]), stage_e.MAX_CAPTURE_EVENTS)
        self.assertEqual(result["events"][-1]["name"], marker)
        self.assertLessEqual(result["events_visited"], stage_e.MAX_NATIVE_EVENTS_VISITED)
        self.assertEqual([row["thread_id"] for row in result["threads_scanned"]], [11, 22])

    async def test_bounded_marker_capture_and_exact_restoration(self):
        manager = FakeManager()
        settings = FakeSettings()
        profiler = FakeProfiler()
        carb = ModuleType("carb")
        carb.__path__ = []
        carb.settings = SimpleNamespace(get_settings=lambda: settings)
        profiler_module = ModuleType("carb.profiler")
        profiler_module.IProfiler = type("IProfiler", (), {"set_python_profiling_enabled": lambda *_: None})
        profiler_module.is_profiler_active = lambda: True
        profiler_module.acquire_profiler_interface = lambda *, plugin_name: (
            profiler if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        monitor = FakeMonitor(profiler)
        profiler_module.acquire_profile_monitor_interface = lambda *, plugin_name: (
            monitor if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        profiler_module.begin = lambda mask, name: profiler.events.append({"name": name, "duration": 0.1, "indent": 0})
        profiler_module.end = lambda mask: None
        carb.profiler = profiler_module
        modules = {**kit_modules(manager), "carb": carb, "carb.profiler": profiler_module}
        request = stage_e.ProfilerCaptureRequest(capture_id="1" * 32, duration_seconds=0.01, python_profile=True)
        with patch.dict(sys.modules, modules):
            result = await stage_e.profiler_capture(request)
            status = stage_e.profiler_status_impl()
        self.assertTrue(result["ok"], result)
        capture = result["result"]
        self.assertTrue(capture["marker_found"])
        self.assertEqual(
            status["capabilities"]["bounded_capture"],
            "available_requires_capture_association",
        )
        self.assertEqual(profiler.mask, 0)
        self.assertFalse(profiler.python)
        self.assertTrue(capture["restoration"]["complete"])
        self.assertTrue(monitor.marked)

    async def test_status_is_read_only_when_profiler_module_is_absent(self):
        manager = FakeManager()
        settings = FakeSettings()
        carb = ModuleType("carb")
        carb.__path__ = []
        carb.settings = SimpleNamespace(get_settings=lambda: settings)
        modules = kit_modules(manager)
        with patch.dict(sys.modules, modules), patch.dict(sys.modules, {"carb": carb}):
            sys.modules.pop("carb.profiler", None)
            result = stage_e.profiler_status_impl()
        self.assertFalse(result["module_loaded"])
        self.assertFalse(result["mutated"])
        self.assertEqual(manager.calls, [])

    async def test_capture_failure_restores_baseline_and_preserves_absent_settings(self):
        manager = FakeManager()
        settings = FakeSettings()
        profiler = FakeProfiler()
        carb = ModuleType("carb")
        carb.__path__ = []
        carb.settings = SimpleNamespace(get_settings=lambda: settings)
        profiler_module = ModuleType("carb.profiler")
        profiler_module.is_profiler_active = lambda: True
        profiler_module.acquire_profiler_interface = lambda *, plugin_name: (
            profiler if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        profiler_module.acquire_profile_monitor_interface = lambda *, plugin_name: (_ for _ in ()).throw(
            RuntimeError("monitor unavailable"))
        carb.profiler = profiler_module
        modules = {**kit_modules(manager), "carb": carb, "carb.profiler": profiler_module}
        request = stage_e.ProfilerCaptureRequest(
            capture_id="3" * 32, duration_seconds=0.01, python_profile=True)
        with patch.dict(sys.modules, modules):
            result = await stage_e.profiler_capture(request)
        self.assertFalse(result["ok"])
        self.assertEqual(profiler.mask, 0)
        self.assertFalse(profiler.python)
        self.assertNotIn("/plugins/carb.profiler-cpu.plugin/compressProfile", settings.values)
        self.assertNotIn("/plugins/carb.profiler-cpu.plugin/filePath", settings.values)

    async def test_native_completion_failure_still_restores_profiler(self):
        manager = FakeManager()
        settings = FakeSettings()
        profiler = FakeProfiler()
        monitor = FakeMonitor(profiler)
        monitor.mark_frame_end = lambda: (_ for _ in ()).throw(RuntimeError("stop failed"))
        carb = ModuleType("carb")
        carb.__path__ = []
        carb.settings = SimpleNamespace(get_settings=lambda: settings)
        profiler_module = ModuleType("carb.profiler")
        profiler_module.is_profiler_active = lambda: True
        profiler_module.acquire_profiler_interface = lambda *, plugin_name: (
            profiler if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        profiler_module.acquire_profile_monitor_interface = lambda *, plugin_name: (
            monitor if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        carb.profiler = profiler_module
        modules = {**kit_modules(manager), "carb": carb, "carb.profiler": profiler_module}
        request = stage_e.ProfilerCaptureRequest(capture_id="6" * 32, duration_seconds=0.01)
        with patch.dict(sys.modules, modules):
            result = await stage_e.profiler_capture(request)
        self.assertFalse(result["ok"])
        self.assertEqual(profiler.mask, 0)
        self.assertFalse(profiler.python)
        self.assertTrue(result["result"]["restoration"]["complete"])

    async def test_duplicate_capture_id_and_concurrent_setting_change_are_truthful(self):
        manager = FakeManager()
        settings = FakeSettings()
        profiler = FakeProfiler()
        carb = ModuleType("carb")
        carb.__path__ = []
        carb.settings = SimpleNamespace(get_settings=lambda: settings)
        profiler_module = ModuleType("carb.profiler")
        profiler_module.IProfiler = type(
            "IProfiler", (), {"set_python_profiling_enabled": lambda *_: None})
        profiler_module.is_profiler_active = lambda: True
        profiler_module.acquire_profiler_interface = lambda *, plugin_name: (
            profiler if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        profiler_module.acquire_profile_monitor_interface = lambda *, plugin_name: (
            FakeMonitor(profiler) if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        profiler_module.begin = lambda mask, name: profiler.events.append({"name": name})
        profiler_module.end = lambda mask: None
        calls = 0

        async def update():
            nonlocal calls
            calls += 1
            if calls == 1:
                settings.set("/plugins/carb.profiler-cpu.plugin/compressProfile", False)
            await asyncio.sleep(0)

        carb.profiler = profiler_module
        app = SimpleNamespace(get_extension_manager=lambda: manager, next_update_async=update)
        modules = {**kit_modules(manager, app), "carb": carb, "carb.profiler": profiler_module}
        request = stage_e.ProfilerCaptureRequest(capture_id="4" * 32, duration_seconds=0.01)
        with patch.dict(sys.modules, modules):
            result = await stage_e.profiler_capture(request)
            duplicate = await stage_e.profiler_capture(request)
        self.assertFalse(result["ok"])
        self.assertIn("setting_changed_concurrently", " ".join(
            result["result"]["restoration"]["conflicts"]))
        self.assertEqual(duplicate["error"]["code"], "DUPLICATE_CAPTURE_ID")
        self.assertIs(settings.get("/plugins/carb.profiler-cpu.plugin/compressProfile"), False)
        self.assertFalse(profiler.python)

    async def test_preexisting_capture_is_refused_without_profiler_mutation(self):
        manager = FakeManager()
        settings = FakeSettings(save=True)
        profiler = FakeProfiler()
        carb = ModuleType("carb")
        carb.__path__ = []
        carb.settings = SimpleNamespace(get_settings=lambda: settings)
        profiler_module = ModuleType("carb.profiler")
        profiler_module.is_profiler_active = lambda: True
        profiler_module.acquire_profiler_interface = lambda *, plugin_name: (
            profiler if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        profiler_module.acquire_profile_monitor_interface = lambda *, plugin_name: (
            FakeMonitor(profiler) if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        carb.profiler = profiler_module
        modules = {**kit_modules(manager), "carb": carb, "carb.profiler": profiler_module}
        request = stage_e.ProfilerCaptureRequest(capture_id="2" * 32, duration_seconds=0.01)
        with patch.dict(sys.modules, modules):
            result = await stage_e.profiler_capture(request)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "PREEXISTING_CAPTURE")
        self.assertEqual(profiler.mask, 0)

    async def test_nonzero_mask_is_treated_as_preexisting_capture(self):
        manager = FakeManager()
        settings = FakeSettings()
        profiler = FakeProfiler()
        profiler.mask = 4
        carb = ModuleType("carb")
        carb.__path__ = []
        carb.settings = SimpleNamespace(get_settings=lambda: settings)
        profiler_module = ModuleType("carb.profiler")
        profiler_module.is_profiler_active = lambda: True
        profiler_module.acquire_profiler_interface = lambda *, plugin_name: (
            profiler if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        profiler_module.acquire_profile_monitor_interface = lambda *, plugin_name: (
            FakeMonitor(profiler) if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        carb.profiler = profiler_module
        modules = {**kit_modules(manager), "carb": carb, "carb.profiler": profiler_module}
        request = stage_e.ProfilerCaptureRequest(capture_id="5" * 32, duration_seconds=0.01)
        with patch.dict(sys.modules, modules):
            result = await stage_e.profiler_capture(request)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "PREEXISTING_CAPTURE")
        self.assertEqual(profiler.mask, 4)


if __name__ == "__main__":
    unittest.main()
