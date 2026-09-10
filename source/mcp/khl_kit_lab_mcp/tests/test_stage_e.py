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
    """Per-thread snapshot matching the installed ProfileEvents contract.

    Deterministic shape only: a mapping from thread_id to that frame's rows, with a
    browser-style main thread. No cross-frame accumulation happens here.
    """

    def __init__(self, rows_by_thread, main_thread_id=11):
        self.rows = {int(t): tuple(r) for t, r in rows_by_thread.items()}
        self.main = int(main_thread_id)

    def get_profile_thread_ids(self):
        return tuple(self.rows)

    def get_main_thread_id(self):
        return self.main

    def get_profile_events(self, thread_id=0):
        return tuple(self.rows.get(thread_id, ()))


class FakeProfiler:
    """Configurable profiler fake modeling measured Kit 110.1.3 application contracts.

    Configurable rather than universal so the tests can express exactly which
    measured surface each case relies on:
      zones_visible_when_python: begin/end zones land in the completed-frame
        snapshot under Python instrumentation (False = measured suppression);
      instants_visible: instant events land in that snapshot (True = measured);
    Per-frame: mark_frame_end() publishes only the current frame's events and
    never re-publishes them.
    """

    def __init__(self, *, zones_visible_when_python=False, instants_visible=True):
        self.mask = 0
        self.python = False
        self.zones_visible_when_python = zones_visible_when_python
        self.instants_visible = instants_visible
        self.frame_events = []
        self.published = ()
        self.main_thread = 11

    def get_capture_mask(self): return self.mask
    def set_capture_mask(self, value): self.mask = value
    def is_python_profiling_enabled(self): return self.python
    def set_python_profiling_enabled(self, value): self.python = value
    def ensure_thread(self): pass

    def begin(self, mask, name):
        if (not self.python) or self.zones_visible_when_python:
            self.frame_events.append({"name": name, "duration": 0.1, "indent": 0, "threadId": self.main_thread})

    def end(self, mask): pass

    def instant(self, mask, instant_type, name):
        if self.instants_visible:
            self.frame_events.append({"name": name, "duration": 0.0, "indent": 0, "threadId": self.main_thread})

    def mark_frame_end(self):
        self.published = tuple(self.frame_events)
        self.frame_events = []

    def last_events(self):
        return self.published

    @property
    def events(self):
        return list(self.published) + list(self.frame_events)


class FakeMonitor:
    def __init__(self, profiler):
        self.profiler = profiler
        self.marked = False

    def mark_frame_end(self):
        self.marked = True
        self.profiler.mark_frame_end()

    def get_last_profile_events(self):
        rows = {profiler.main_thread: profiler.last_events()} if (profiler := self.profiler) else {}
        return FakeEvents(rows, main_thread_id=self.profiler.main_thread)


class ProfilerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stage_e._capture_operations.clear()
        stage_e._capture_tasks.clear()

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
        self.assertTrue(result["association_found"])
        self.assertEqual(result["association_thread_id"], 22)
        self.assertEqual(len(result["events"]), stage_e.MAX_CAPTURE_EVENTS)
        # The dedicated association token is preserved first, not an arbitrary event.
        self.assertEqual(result["events"][0]["name"], marker)
        # The main thread must not monopolize all 512 slots; thread 22 keeps representation.
        self.assertTrue(any(row.get("profile_thread_id") == 22 for row in result["events"]))
        self.assertLessEqual(result["events_visited"], stage_e.MAX_NATIVE_EVENTS_VISITED)
        self.assertEqual([row["thread_id"] for row in result["threads_scanned"]], [11, 22])

    def test_association_exact_match_rejects_collisions(self):
        class Ev:
            rows = {11: ({"name": "KHL_CAPTURE_ASSOC_aaaa_extra"},)}

            def get_profile_thread_ids(self): return (11,)

            def get_main_thread_id(self): return 11

            def get_profile_events(self, thread_id=0): return self.rows.get(thread_id, ())

        result = stage_e._collect_events(Ev(), "KHL_CAPTURE_ASSOC_aaaa")
        self.assertFalse(result["association_found"], "substring match must not count as exact association")
        self.assertIsNone(result["association_thread_id"])

    def test_thread_count_truncation_is_distinct(self):
        class Ev:
            rows = {11: ({"name": "m"},)}

            def get_profile_thread_ids(self): return tuple(range(stage_e.MAX_PROFILE_THREADS + 1))

            def get_main_thread_id(self): return 11

            def get_profile_events(self, thread_id=0): return self.rows.get(11, ())

        result = stage_e._collect_events(Ev(), "absent-marker")
        self.assertTrue(result["threads_truncated"])
        self.assertFalse(result["output_truncated"])
        self.assertFalse(result["traversal_truncated"])
        self.assertTrue(result["events_truncated"])

    def test_output_truncation_does_not_set_thread_truncation(self):
        # Two threads producing more payload than MAX_CAPTURE_EVENTS output slots.
        class Ev:
            rows = {
                11: tuple({"name": f"m{i}"} for i in range(stage_e.MAX_CAPTURE_EVENTS)),
                22: tuple({"name": f"n{i}"} for i in range(stage_e.MAX_CAPTURE_EVENTS)),
            }

            def get_profile_thread_ids(self): return (11, 22)

            def get_main_thread_id(self): return 11

            def get_profile_events(self, thread_id=0): return self.rows.get(thread_id, ())

        result = stage_e._collect_events(Ev(), "absent-marker")
        self.assertFalse(result["threads_truncated"])
        self.assertTrue(result["output_truncated"])
        self.assertTrue(result["events_truncated"])

    def test_collect_does_not_republish_across_frames(self):
        class Ev:
            def __init__(self):
                self.rows = {11: ({"name": "old-marker"},), 22: ({"name": "fresh"},)}

            def get_profile_thread_ids(self): return (11, 22)

            def get_main_thread_id(self): return 11

            def get_profile_events(self, thread_id=0): return self.rows.get(thread_id, ())

        events = Ev()
        first = stage_e._collect_events(events, "old-marker")
        self.assertTrue(first["association_found"])
        self.assertEqual(first["association_thread_id"], 11)
        # Simulate the monitor publishing a fresh frame with different content.
        events.rows = {11: ({"name": "fresh"},), 22: ({"name": "newer"},)}
        fresh = stage_e._collect_events(events, "absent-marker")
        self.assertFalse(fresh["association_found"])
        self.assertIsNone(fresh["association_thread_id"])

    def test_main_thread_is_prioritized_without_expanding_thread_bound(self):
        # Adversarial: MAX_PROFILE_THREADS bound excludes the main thread id.
        many = stage_e.MAX_PROFILE_THREADS
        class Ev:
            rows = {tid: ({"name": f"t{tid}"},) for tid in range(1, many + 1)}
            rows[999999] = ({"name": "KHL_CAPTURE_ASSOC_x"},)

            def get_profile_thread_ids(self): return (tuple(range(1, many + 1)) + (999999,))

            def get_main_thread_id(self): return 999999

            def get_profile_events(self, thread_id=0): return self.rows.get(thread_id, ())

        result = stage_e._collect_events(Ev(), "KHL_CAPTURE_ASSOC_x")
        # Global thread bound holds while the main thread remains first because
        # it owns the stop-boundary association token.
        self.assertTrue(result["threads_truncated"])
        self.assertTrue(result["association_found"])
        self.assertLessEqual(len(result["threads_scanned"]), stage_e.MAX_PROFILE_THREADS)
        self.assertEqual(result["threads_scanned"][0]["thread_id"], 999999)

    def test_advertised_thread_iterator_is_consumed_only_to_bound_plus_one(self):
        consumed = 0

        class Ev:
            def get_profile_thread_ids(self):
                nonlocal consumed
                for thread_id in range(stage_e.MAX_PROFILE_THREADS + 1):
                    consumed += 1
                    yield thread_id
                raise AssertionError("collector consumed beyond the bounded sample")

            def get_main_thread_id(self): return 0

            def get_profile_events(self, thread_id=0): return ()

        result = stage_e._collect_events(Ev(), "absent-marker")
        self.assertTrue(result["threads_truncated"])
        self.assertEqual(consumed, stage_e.MAX_PROFILE_THREADS + 1)
        self.assertLessEqual(len(result["threads_scanned"]), stage_e.MAX_PROFILE_THREADS)

    def test_oversized_children_are_bounded_and_mark_traversal_truncated(self):
        class Ev:
            root = {
                "name": "root",
                "children": tuple({"name": f"c{i}"} for i in range(50_000)),
            }
            rows = {11: (root,)}

            def get_profile_thread_ids(self): return (11,)

            def get_main_thread_id(self): return 11

            def get_profile_events(self, thread_id=0): return self.rows.get(thread_id, ())

        result = stage_e._collect_events(Ev(), "absent-marker")
        self.assertTrue(result["traversal_truncated"])
        # Bounded by the global visit budget, not by the oversized child collection.
        self.assertLessEqual(result["events_visited"], stage_e.MAX_NATIVE_EVENTS_VISITED)
        # Root plus clamped children should be bounded well below 50_000.
        self.assertLess(result["events_visited"], 50_000)

    def test_oversized_root_events_are_bounded_and_mark_traversal_truncated(self):
        # Adversarial: more initial root events than visit_limit.
        class Ev:
            rows = {11: tuple({"name": f"r{i}"} for i in range(50_000))}

            def get_profile_thread_ids(self): return (11,)

            def get_main_thread_id(self): return 11

            def get_profile_events(self, thread_id=0): return self.rows.get(thread_id, ())

        result = stage_e._collect_events(Ev(), "absent-marker")
        self.assertTrue(result["traversal_truncated"])
        self.assertLessEqual(result["events_visited"], stage_e.MAX_NATIVE_EVENTS_VISITED)
        self.assertLess(result["events_visited"], 50_000,
                        "initial root enqueue must be clamped by the same visit budget")

    def test_stop_boundary_association_survives_oversized_root_sequence(self):
        marker = "KHL_CAPTURE_ASSOC_tail"

        class Ev:
            rows = {
                11: tuple(
                    {"name": marker if index == 49_999 else f"r{index}"}
                    for index in range(50_000)
                )
            }

            def get_profile_thread_ids(self): return (11,)

            def get_main_thread_id(self): return 11

            def get_profile_events(self, thread_id=0): return self.rows.get(thread_id, ())

        result = stage_e._collect_events(Ev(), marker)
        self.assertTrue(result["association_found"])
        self.assertTrue(result["traversal_truncated"])
        self.assertLessEqual(result["events_visited"], stage_e.MAX_NATIVE_EVENTS_VISITED)

    def test_stop_boundary_association_survives_oversized_child_sequence(self):
        marker = "KHL_CAPTURE_ASSOC_child_tail"

        class Ev:
            rows = {
                11: ({
                    "name": "root",
                    "children": tuple(
                        {"name": marker if index == 49_999 else f"c{index}"}
                        for index in range(50_000)
                    ),
                },)
            }

            def get_profile_thread_ids(self): return (11,)

            def get_main_thread_id(self): return 11

            def get_profile_events(self, thread_id=0): return self.rows.get(thread_id, ())

        result = stage_e._collect_events(Ev(), marker)
        self.assertTrue(result["association_found"])
        self.assertTrue(result["traversal_truncated"])
        self.assertLessEqual(result["events_visited"], stage_e.MAX_NATIVE_EVENTS_VISITED)

    def test_exact_association_survives_full_output(self):
        class Ev:
            rows = {
                11: tuple({"name": f"m{i}"} for i in range(stage_e.MAX_CAPTURE_EVENTS)),
                22: ({"name": "KHL_CAPTURE_ASSOC_full"},),
            }

            def get_profile_thread_ids(self): return (11, 22)

            def get_main_thread_id(self): return 11

            def get_profile_events(self, thread_id=0): return self.rows.get(thread_id, ())

        result = stage_e._collect_events(Ev(), "KHL_CAPTURE_ASSOC_full")
        self.assertTrue(result["association_found"])
        self.assertEqual(result["events"][0]["name"], "KHL_CAPTURE_ASSOC_full")
        self.assertLessEqual(len(result["events"]), stage_e.MAX_CAPTURE_EVENTS)
        assoc = [row for row in result["events"] if row["name"] == "KHL_CAPTURE_ASSOC_full"]
        self.assertEqual(len(assoc), 1)  # exactly once in output

    async def test_instant_suppression_fails_association_but_restores_state(self):
        manager = FakeManager()
        settings = FakeSettings()
        profiler = FakeProfiler(instants_visible=False)
        carb = ModuleType("carb")
        carb.__path__ = []
        carb.settings = SimpleNamespace(get_settings=lambda: settings)
        profiler_module = ModuleType("carb.profiler")
        profiler_module.IProfiler = type("IProfiler", (), {"set_python_profiling_enabled": lambda *_: None})
        profiler_module.is_profiler_active = lambda: True
        profiler_module.acquire_profiler_interface = lambda *, plugin_name: (
            profiler if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        profiler_module.acquire_profile_monitor_interface = lambda *, plugin_name: (
            FakeMonitor(profiler) if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        profiler_module.InstantType = SimpleNamespace(THREAD=0, PROCESS=1)
        carb.profiler = profiler_module
        modules = {**kit_modules(manager), "carb": carb, "carb.profiler": profiler_module}
        request = stage_e.ProfilerCaptureRequest(capture_id="8" * 32, duration_seconds=0.01, python_profile=True)
        with patch.dict(sys.modules, modules):
            result = await stage_e.profiler_capture(request)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "CAPTURE_ASSOCIATION_FAILED")
        self.assertTrue(result["result"]["restoration"]["complete"])
        self.assertEqual(profiler.mask, 0)
        self.assertFalse(profiler.python)

    async def test_genuinely_concurrent_identical_capture_ids_accept_exactly_one(self):
        # True intra-event-loop concurrency: both profiler_capture calls run to a
        # scheduling point where the first is in-flight before the second starts.
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
        profiler_module.acquire_profile_monitor_interface = lambda *, plugin_name: (
            FakeMonitor(profiler) if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        profiler_module.InstantType = SimpleNamespace(THREAD=0, PROCESS=1)
        carb.profiler = profiler_module
        modules = {**kit_modules(manager), "carb": carb, "carb.profiler": profiler_module}
        request = stage_e.ProfilerCaptureRequest(capture_id="9" * 32, duration_seconds=0.02, python_profile=True)
        with patch.dict(sys.modules, modules):
            # Launch both before either completes so they genuinely overlap.
            first_task = asyncio.ensure_future(stage_e.profiler_capture(request))
            await asyncio.sleep(0)  # let the first task start and reserve its ID synchronously
            second_task = asyncio.ensure_future(stage_e.profiler_capture(request))
            first, second = await asyncio.gather(first_task, second_task)
        outcomes = [first["ok"], second["ok"]]
        self.assertEqual(outcomes.count(True), 1, "exactly one concurrent same-ID request must be accepted")
        rejected = second if not second["ok"] else first
        self.assertEqual(rejected["error"]["code"], "DUPLICATE_CAPTURE_ID")
        # The accepted operation genuinely completed and restored state.
        accepted = first if first["ok"] else second
        self.assertTrue(accepted["result"]["restoration"]["complete"])
        self.assertEqual(profiler.mask, 0)
        self.assertFalse(profiler.python)

    async def test_duplicate_id_rejected_even_when_accepted_in_flight(self):
        # Covers the same case as the genuine-concurrency test but serial emission
        # timing to make intent explicit and stabilize non-GIL timing on slow machines.
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
        profiler_module.acquire_profile_monitor_interface = lambda *, plugin_name: (
            FakeMonitor(profiler) if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        profiler_module.InstantType = SimpleNamespace(THREAD=0, PROCESS=1)
        carb.profiler = profiler_module
        modules = {**kit_modules(manager), "carb": carb, "carb.profiler": profiler_module}
        request = stage_e.ProfilerCaptureRequest(capture_id="9" * 32, duration_seconds=0.05, python_profile=True)
        with patch.dict(sys.modules, modules):
            task = asyncio.ensure_future(stage_e.profiler_capture(request))
            await asyncio.sleep(0.01)
            duplicate = await stage_e.profiler_capture(request)
            first = await task
        self.assertTrue(first["ok"], first)
        self.assertFalse(duplicate["ok"])
        self.assertEqual(duplicate["error"]["code"], "DUPLICATE_CAPTURE_ID")

    async def test_create_task_failure_cleans_up_reservation(self):
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
        profiler_module.acquire_profile_monitor_interface = lambda *, plugin_name: (
            FakeMonitor(profiler) if plugin_name == "carb.profiler-cpu.plugin" else None
        )
        profiler_module.InstantType = SimpleNamespace(THREAD=0, PROCESS=1)
        carb.profiler = profiler_module
        modules = {**kit_modules(manager), "carb": carb, "carb.profiler": profiler_module}
        request = stage_e.ProfilerCaptureRequest(capture_id="9" * 32, duration_seconds=0.01, python_profile=True)
        with patch.dict(sys.modules, modules):
            with patch.object(stage_e.asyncio, "create_task", side_effect=RuntimeError("no loop")):
                with self.assertRaises(RuntimeError):
                    await stage_e.profiler_capture(request)
            # Reservation must be cleaned up; the ID is free again.
            self.assertNotIn("9" * 32, stage_e._capture_operations)
            self.assertNotIn("9" * 32, stage_e._capture_tasks)

    async def test_cancelled_request_keeps_strong_cleanup_owner(self):
        manager = FakeManager()
        settings = FakeSettings()
        profiler = FakeProfiler()
        entered_update = asyncio.Event()
        release_update = asyncio.Event()

        async def update():
            entered_update.set()
            await release_update.wait()

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
        profiler_module.InstantType = SimpleNamespace(THREAD=0, PROCESS=1)
        carb.profiler = profiler_module
        app = SimpleNamespace(get_extension_manager=lambda: manager, next_update_async=update)
        modules = {**kit_modules(manager, app), "carb": carb, "carb.profiler": profiler_module}
        capture_id = "a" * 32
        request = stage_e.ProfilerCaptureRequest(
            capture_id=capture_id, duration_seconds=0.01, python_profile=True)
        with patch.dict(sys.modules, modules):
            request_task = asyncio.create_task(stage_e.profiler_capture(request))
            await entered_update.wait()
            owner = stage_e._capture_tasks[capture_id]
            request_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request_task
            self.assertIs(stage_e._capture_tasks.get(capture_id), owner)
            release_update.set()
            result = await owner
            await asyncio.sleep(0)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["result"]["restoration"]["complete"])
        self.assertNotIn(capture_id, stage_e._capture_tasks)
        self.assertEqual(profiler.mask, 0)
        self.assertFalse(profiler.python)

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
        profiler_module.InstantType = SimpleNamespace(THREAD=0, PROCESS=1)
        carb.profiler = profiler_module
        modules = {**kit_modules(manager), "carb": carb, "carb.profiler": profiler_module}
        request = stage_e.ProfilerCaptureRequest(capture_id="1" * 32, duration_seconds=0.01, python_profile=True)
        with patch.dict(sys.modules, modules):
            result = await stage_e.profiler_capture(request)
            status = stage_e.profiler_status_impl()
        self.assertTrue(result["ok"], result)
        capture = result["result"]
        # Python instrumentation is on, so the begin/end payload zone is dropped by
        # this fake per measured semantics; association succeeds via the instant token.
        self.assertTrue(capture["association_found"])
        self.assertEqual(
            status["capabilities"]["bounded_capture"],
            stage_e.VERIFIED_CAPTURE_CAPABILITY,
        )
        self.assertEqual(profiler.mask, 0)
        self.assertFalse(profiler.python)
        self.assertTrue(capture["restoration"]["complete"])
        self.assertTrue(monitor.marked)
        instant_rows = [row for row in monitor.profiler.last_events() if row["name"] == capture["association_marker"]]
        self.assertTrue(instant_rows, "instant ownership token must appear in the published snapshot")
        self.assertEqual(instant_rows[-1]["duration"], 0.0)

    async def test_instant_token_associates_when_python_profiling_drops_zones(self):
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
        profiler_module.InstantType = SimpleNamespace(THREAD=0, PROCESS=1)
        carb.profiler = profiler_module
        modules = {**kit_modules(manager), "carb": carb, "carb.profiler": profiler_module}
        request = stage_e.ProfilerCaptureRequest(capture_id="7" * 32, duration_seconds=0.01, python_profile=True)
        with patch.dict(sys.modules, modules):
            result = await stage_e.profiler_capture(request)
        self.assertTrue(result["ok"], result)
        capture = result["result"]
        self.assertTrue(capture["association_found"], "association must hold via the instant token under python profiling")
        self.assertTrue(capture["restoration"]["complete"])
        marker_rows = [row for row in monitor.profiler.last_events() if row["name"] == capture["association_marker"]]
        self.assertEqual(len(marker_rows), 1)
        self.assertEqual(marker_rows[0]["duration"], 0.0, "only the instant token carries the marker under python")

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
        profiler_module.InstantType = SimpleNamespace(THREAD=0, PROCESS=1)
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
