import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp import Client


_SERVER_IMPORT_ROOT = tempfile.TemporaryDirectory()
os.environ["KIT_LAB_EXPERIMENT_ROOT"] = str(Path(_SERVER_IMPORT_ROOT.name) / "experiments")

from khl_kit_lab_mcp.policy import (  # noqa: E402
    PLAYGROUND_ROOT,
    POLICY_FINGERPRINT,
    POLICY_VERSION,
    PYTHON_PERMISSION_CHOICES,
    SERVER_INSTRUCTIONS,
    TOOL_POLICIES,
    policy_payload,
)
from khl_kit_lab_mcp.server import mcp  # noqa: E402
from khl_kit_lab_mcp import server  # noqa: E402
from khl_kit_lab_mcp.experiments import ExperimentStore  # noqa: E402


RETAINED_TOOLS = {
    "kit_lab_policy", "kit_lab_status", "kit_runtime_info", "kit_stage_summary",
    "kit_extensions_list", "kit_viewport_info", "kit_execute_python",
    "kit_extension_inspect", "kit_extension_enable", "kit_extension_disable",
    "kit_extension_reload", "kit_profiler_status", "kit_profiler_capture",
    "kit_profiler_capture_status",
    "kit_reset_python_session", "kit_experiment_start", "kit_experiment_current",
    "kit_experiment_list", "kit_experiment_get", "kit_experiment_note", "kit_experiment_finish",
    "kit_lifecycle_config", "kit_status", "kit_start", "kit_stop", "kit_restart", "kit_log_paths",
}


class PolicyMetadataTests(unittest.TestCase):
    def registered_tools(self):
        return asyncio.run(mcp.list_tools())

    def test_registered_tools_exactly_match_policy_entries(self) -> None:
        tools = self.registered_tools()
        self.assertEqual({tool.name for tool in tools}, set(TOOL_POLICIES))
        self.assertEqual(len(tools), 27)
        self.assertEqual({tool.name for tool in tools}, RETAINED_TOOLS)

    def test_descriptions_annotations_and_meta_match_policy(self) -> None:
        for tool in self.registered_tools():
            with self.subTest(tool=tool.name):
                expected = TOOL_POLICIES[tool.name]
                self.assertIn(expected["default_behavior"], {"allow", "ask", "deny"})
                self.assertEqual(tool.description, expected["description"])
                annotations = tool.annotations
                self.assertIsNotNone(annotations)
                for field, value in expected["annotations"].items():
                    self.assertEqual(getattr(annotations, field), value)
                self.assertEqual(tool.meta["khl_policy"]["version"], POLICY_VERSION)
                self.assertEqual(
                    tool.meta["khl_policy"]["fingerprint"],
                    POLICY_FINGERPRINT,
                )
                self.assertEqual(
                    tool.meta["khl_policy"]["class"],
                    expected["class"],
                )

    def test_server_instructions_and_policy_payload_are_complete(self) -> None:
        self.assertEqual(mcp.instructions, SERVER_INSTRUCTIONS)
        payload = policy_payload()
        self.assertEqual(payload["policy_version"], POLICY_VERSION)
        self.assertEqual(payload["policy_fingerprint"], POLICY_FINGERPRINT)
        self.assertEqual(payload["default_playground_root"], PLAYGROUND_ROOT)
        self.assertEqual(payload["python_permission_choices"], PYTHON_PERMISSION_CHOICES)
        self.assertFalse(payload["persistent_write_tools_exposed"])
        self.assertFalse(payload["destructive_external_tools_exposed"])
        self.assertEqual(
            {entry["tool"] for entry in payload["tools"]},
            set(TOOL_POLICIES),
        )

    def test_policy_manual_names_every_tool(self) -> None:
        manual = Path(__file__).resolve().parents[1] / "MCP_POLICY.md"
        text = manual.read_text(encoding="utf-8")
        self.assertIn(f"Policy version: **{POLICY_VERSION}**", text)
        self.assertIn(f"Policy fingerprint: **{POLICY_FINGERPRINT}**", text)
        for tool_name in TOOL_POLICIES:
            with self.subTest(tool=tool_name):
                self.assertIn(f"`{tool_name}`", text)

    def test_readme_names_every_tool(self) -> None:
        readme = Path(__file__).resolve().parents[1] / "README.md"
        text = readme.read_text(encoding="utf-8")
        self.assertIn(f"Policy fingerprint: `{POLICY_FINGERPRINT}`", text)
        for tool_name in TOOL_POLICIES:
            with self.subTest(tool=tool_name):
                self.assertIn(f"`{tool_name}`", text)

    def test_in_process_client_receives_policy_over_mcp(self) -> None:
        async def exercise():
            async with Client(mcp) as connected:
                listed = await connected.list_tools(cache_mode="bypass")
                result = await connected.call_tool("kit_lab_policy", {})
                return connected.instructions, listed.tools, result

        instructions, tools, result = asyncio.run(exercise())
        self.assertEqual(instructions, SERVER_INSTRUCTIONS)
        self.assertEqual({tool.name for tool in tools}, set(TOOL_POLICIES))
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["policy_version"], POLICY_VERSION)
        self.assertEqual(
            result.structured_content["policy_fingerprint"],
            POLICY_FINGERPRINT,
        )

    def test_summary_defaults_to_typed_post_and_records_opt_in(self):
        async def exercise():
            post = AsyncMock(return_value={"ok": True, "result": {}})
            get = AsyncMock(side_effect=AssertionError("must not use old GET"))
            with patch.object(server.client, "post", post), patch.object(server.client, "get", get):
                async with Client(mcp) as connected:
                    tools = (await connected.list_tools()).tools
                    schema = next(t for t in tools if t.name == "kit_stage_summary").input_schema
                    self.assertFalse(schema["properties"]["include_statistics"]["default"])
                    self.assertFalse((await connected.call_tool("kit_stage_summary", {})).is_error)
                    self.assertFalse((await connected.call_tool(
                        "kit_stage_summary", {"include_statistics": True})).is_error)
            self.assertEqual([call.args for call in post.await_args_list], [
                ("/khl/lab/stage/summary", {"include_statistics": False}),
                ("/khl/lab/stage/summary", {"include_statistics": True}),
            ])
        asyncio.run(exercise())

    def test_lifecycle_schemas_dispatch_and_progress_context(self):
        from types import SimpleNamespace
        from khl_kit_lab_mcp.lifecycle import LifecycleError
        async def exercise():
            async def start(timeout, progress):
                self.assertEqual(timeout, 5)
                await progress(1, 5, "fixture readiness")
                return {"state": "READY"}
            adapter = SimpleNamespace(start=AsyncMock(side_effect=start), stop=AsyncMock(return_value={"state": "STOPPED"}),
                                      status=AsyncMock(side_effect=LifecycleError("AMBIGUOUS", "fixture")))
            with patch.object(server, "_lifecycle", return_value=adapter):
                async with Client(mcp) as connected:
                    tools = {t.name: t for t in (await connected.list_tools()).tools}
                    for name in ("kit_lifecycle_config", "kit_status", "kit_start", "kit_stop", "kit_restart", "kit_log_paths"):
                        properties = tools[name].input_schema["properties"]
                        self.assertEqual(properties["kit_id"]["const"], "nchc-kit-dev-main")
                        self.assertFalse({"ctx", "pid", "command", "path", "port"} & properties.keys())
                    self.assertFalse(tools["kit_stop"].input_schema["properties"]["force"]["default"])
                    self.assertFalse((await connected.call_tool("kit_start", {"readiness_timeout": 5})).is_error)
                    self.assertFalse((await connected.call_tool("kit_stop", {})).is_error)
                    adapter.stop.assert_awaited_once()
                    self.assertFalse(adapter.stop.await_args.args[0])
                    result = await connected.call_tool("kit_status", {})
                    self.assertTrue(result.is_error)
                    self.assertIn("AMBIGUOUS", str(result.content))
        asyncio.run(exercise())

    def test_python_source_exceptions_and_historical_retrieval_over_mcp(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as temporary:
                store = ExperimentStore(Path(temporary) / "experiments")
                experiment_id = store.start("Historical compatibility", "Keep old evidence", [], {})["experiment_id"]
                for retired in ("kit_prim_create", "kit_prim_remove", "kit_prim_inspect", "kit_setting_get"):
                    store.record_operation(experiment_id, retired, {}, success=True,
                                           elapsed_ms=1, result={"ok": True})
                code = "print('captured')\nraise ValueError('fixture')"
                response = {"ok": False, "stdout": "captured\n", "stderr": "",
                            "exception_type": "ValueError", "traceback": "ValueError: fixture"}
                with patch.object(server, "experiments", store), patch.object(
                    server.client, "post", AsyncMock(return_value=response)
                ) as post:
                    async with Client(mcp) as connected:
                        result = await connected.call_tool("kit_execute_python", {"code": code})
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.structured_content, response)
                    post.assert_awaited_once_with("/khl/lab/python/execute", {"code": code})
                store.finish("fixture complete", "success")
                with patch.object(server, "experiments", ExperimentStore(store.root)):
                    async with Client(mcp) as connected:
                        result = await connected.call_tool("kit_experiment_get", {"experiment_id": experiment_id})
                self.assertFalse(result.is_error)
                events = result.structured_content["events"]
                self.assertTrue({"kit_prim_create", "kit_prim_remove", "kit_prim_inspect", "kit_setting_get"}
                                <= {e.get("tool") for e in events})
                event = next(e for e in events if e.get("tool") == "kit_execute_python")
                self.assertEqual(event["result_class"], "python_exception_result")
                self.assertEqual((store.root / experiment_id / event["python_source_file"]).read_text(), code)
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
