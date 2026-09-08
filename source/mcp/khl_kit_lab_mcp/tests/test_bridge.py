"""Exercise the actual bridge in isolated Python, without a running Kit or USD stage."""

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


BRIDGE = (Path(__file__).resolve().parents[3] / "extensions/omni.khl.kit_lab"
          / "omni/khl/kit_lab/service.py")


class Router:
    def __init__(self, prefix, **kwargs):
        self.prefix = prefix
        self.routes = {}

    def get(self, path, **kwargs):
        return self._register("GET", path)

    def post(self, path, **kwargs):
        return self._register("POST", path)

    def _register(self, method, path):
        def decorate(function):
            self.routes[(method, self.prefix + path)] = function
            return function
        return decorate


def load_bridge():
    routers = ModuleType("omni.services.core.routers")
    routers.ServiceAPIRouter = Router
    spec = importlib.util.spec_from_file_location("isolated_kit_lab_bridge", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"omni.services.core.routers": routers}):
        spec.loader.exec_module(module)
    return module


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bridge = load_bridge()

    def stage_modules(self, stage):
        context = SimpleNamespace(get_stage=lambda: stage, get_stage_url=lambda: "")
        usd = ModuleType("omni.usd")
        usd.get_context = lambda: context
        omni = ModuleType("omni")
        omni.usd = usd
        pxr = ModuleType("pxr")
        pxr.UsdGeom = SimpleNamespace(GetStageMetersPerUnit=lambda _: 0.01,
                                      GetStageUpAxis=lambda _: "Y")
        return patch.dict(sys.modules, {"omni": omni, "omni.usd": usd, "pxr": pxr})

    def stage(self):
        stage = Mock()
        stage.GetDefaultPrim.return_value = None
        stage.GetRootLayer.return_value.identifier = "root"
        stage.GetSessionLayer.return_value.identifier = "session"
        stage.GetEditTarget.return_value.GetLayer.return_value.identifier = "edit"
        stage.GetLayerStack.return_value = []
        stage.GetPseudoRoot.return_value.GetChildren.return_value = []
        for name in ("GetStartTimeCode", "GetEndTimeCode", "GetTimeCodesPerSecond",
                     "GetFramesPerSecond"):
            getattr(stage, name).return_value = 24
        return stage

    async def test_routes_and_capabilities_remove_only_retired_operations(self):
        self.assertEqual(set(self.bridge.router.routes), {
            ("GET", "/khl/lab/status"), ("GET", "/khl/lab/runtime/info"),
            ("GET", "/khl/lab/runtime/identity"),
            ("GET", "/khl/lab/stage/summary"), ("POST", "/khl/lab/stage/summary"),
            ("POST", "/khl/lab/extensions/list"), ("GET", "/khl/lab/viewport/info"),
            ("POST", "/khl/lab/python/execute"), ("POST", "/khl/lab/session/reset"),
        })
        self.assertEqual(set(self.bridge.legacy_router.routes), {
            ("GET", "/khl/ai/status"), ("POST", "/khl/ai/execute"),
            ("POST", "/khl/ai/reset"),
        })
        self.assertEqual((await self.bridge.status())["capabilities"]["read"],
                         ["runtime.info", "runtime.identity", "stage.summary", "extensions.list", "viewport.info"])
        self.assertFalse(hasattr(self.bridge, "PrimInspectRequest"))
        self.assertFalse(hasattr(self.bridge, "SettingGetRequest"))

    async def test_default_get_and_post_never_traverse(self):
        stage = self.stage()
        stage.Traverse.side_effect = AssertionError("default must not traverse")
        with self.stage_modules(stage):
            responses = [await self.bridge.stage_summary(),
                         await self.bridge.stage_summary_request(self.bridge.StageSummaryRequest())]
        for response in responses:
            self.assertTrue(response["ok"])
            self.assertFalse(response["result"]["statistics_computed"])
            self.assertIsNone(response["result"]["prim_count"])
            self.assertIsNone(response["result"]["type_counts"])
        stage.Traverse.assert_not_called()

    async def test_runtime_identity_reports_actual_process_and_setting_without_execution_lock(self):
        import os
        carb = ModuleType("carb")
        settings = Mock()
        settings.get.return_value = "/fixture/kit_current.log"
        carb.settings = SimpleNamespace(get_settings=lambda: settings)
        omni = ModuleType("omni")
        kit = ModuleType("omni.kit")
        app = ModuleType("omni.kit.app")
        app.get_app = lambda: SimpleNamespace(is_app_ready=lambda: True)
        omni.kit, kit.app = kit, app
        with patch.dict(sys.modules, {"carb": carb, "omni": omni, "omni.kit": kit, "omni.kit.app": app}), \
                patch.dict(os.environ, {"KHL_KIT_ID": "nchc-kit-dev-main"}):
            async with self.bridge._execute_lock:
                response = await asyncio.wait_for(self.bridge.runtime_identity(), 1)
        self.assertTrue(response["ok"])
        result = response["result"]
        self.assertEqual(result["kit_id"], "nchc-kit-dev-main")
        self.assertEqual(result["pid"], os.getpid())
        self.assertGreater(result["start_ticks"], 0)
        self.assertTrue(result["ready"])
        self.assertEqual(result["native_log_path"], "/fixture/kit_current.log")
        settings.get.assert_called_once_with("/log/file")

    async def test_opt_in_counts_default_traversal_including_untyped(self):
        stage = self.stage()
        stage.Traverse.return_value = iter([
            SimpleNamespace(GetTypeName=lambda: "Cube"),
            SimpleNamespace(GetTypeName=lambda: ""),
            SimpleNamespace(GetTypeName=lambda: "Cube"),
        ])
        with self.stage_modules(stage):
            response = await self.bridge.stage_summary_request(
                self.bridge.StageSummaryRequest(include_statistics=True))
        result = response["result"]
        self.assertTrue(result["statistics_computed"])
        self.assertEqual(result["prim_count"], 3)
        self.assertEqual(result["type_counts"], {"Cube": 2, "<untyped>": 1})
        stage.Traverse.assert_called_once_with()

    async def test_absent_and_computed_empty_stage_are_distinct(self):
        with self.stage_modules(None):
            absent = (await self.bridge.stage_summary(True))["result"]
        self.assertFalse(absent["available"])
        self.assertFalse(absent["statistics_computed"])
        self.assertIsNone(absent["prim_count"])
        stage = self.stage()
        stage.Traverse.return_value = iter(())
        with self.stage_modules(stage):
            empty = (await self.bridge.stage_summary(True))["result"]
        self.assertTrue(empty["statistics_computed"])
        self.assertEqual(empty["prim_count"], 0)
        self.assertEqual(empty["type_counts"], {})

    async def test_root_children_are_limited_and_truncation_is_explicit(self):
        stage = self.stage()
        consumed = []
        def children():
            for i in range(10_000):
                consumed.append(i)
                yield SimpleNamespace(GetPath=lambda i=i: f"/Root{i}")
        stage.GetPseudoRoot.return_value.GetChildren.return_value = children()
        with self.stage_modules(stage):
            result = (await self.bridge.stage_summary())["result"]
        self.assertEqual(len(consumed), 257)
        self.assertEqual(len(result["root_children"]), 256)
        self.assertTrue(result["root_children_truncated"])

    async def run_source(self, code):
        return await self.bridge.execute(self.bridge.ExecuteRequest(code=code))

    async def test_persistent_namespace_await_and_last_expression(self):
        first = await self.run_source("import asyncio\nx = 7\nawait asyncio.sleep(0)\nx + 1")
        self.assertTrue(first["ok"])
        self.assertEqual(first["result"], "8")
        second = await self.run_source("await asyncio.sleep(0, result=x + 2)")
        self.assertEqual(second["result"], "9")
        statements = await self.run_source("await asyncio.sleep(0)\nx = 10")
        self.assertTrue(statements["ok"])
        self.assertIsNone(statements["result"])
        self.assertEqual((await self.run_source("x"))["result"], "10")

    async def test_stdout_stderr_traceback_and_recovery(self):
        response = await self.run_source(
            "import sys\nprint('out')\nprint('err', file=sys.stderr)\nraise ValueError('fixture')")
        self.assertFalse(response["ok"])
        self.assertEqual(response["stdout"], "out\n")
        self.assertEqual(response["stderr"], "err\n")
        self.assertEqual(response["exception_type"], "ValueError")
        self.assertIn("ValueError: fixture", response["traceback"])
        syntax = await self.run_source("if:")
        self.assertEqual(syntax["exception_type"], "SyntaxError")
        self.assertTrue((await self.run_source("2 + 2"))["ok"])
        self.assertFalse((await self.bridge.status())["busy"])

    async def test_legacy_namespace_and_reset_do_not_cancel_tasks(self):
        await self.run_source("x = 1")
        legacy = await self.bridge.legacy_execute(self.bridge.ExecuteRequest(code="x"))
        self.assertEqual(legacy["result"], "1")
        task = asyncio.create_task(asyncio.Event().wait())
        self.bridge._namespace["task"] = task
        try:
            await self.bridge.legacy_reset()
            self.assertNotIn("x", self.bridge._namespace)
            self.assertFalse(task.cancelled())
            self.assertFalse(task.done())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
