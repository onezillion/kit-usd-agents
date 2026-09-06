import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from mcp import Client


_SERVER_IMPORT_ROOT = tempfile.TemporaryDirectory()
os.environ["KIT_LAB_EXPERIMENT_ROOT"] = str(Path(_SERVER_IMPORT_ROOT.name) / "experiments")

from khl_kit_lab_mcp.live_stage import (  # noqa: E402
    build_create_prim_source,
    build_remove_prim_source,
    validate_prim_path,
)
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


class PolicyMetadataTests(unittest.TestCase):
    def registered_tools(self):
        return asyncio.run(mcp.list_tools())

    def test_registered_tools_exactly_match_policy_entries(self) -> None:
        tools = self.registered_tools()
        self.assertEqual({tool.name for tool in tools}, set(TOOL_POLICIES))
        self.assertEqual(len(tools), 18)

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


class LiveStageSourceTests(unittest.TestCase):
    def test_general_absolute_paths_are_allowed(self) -> None:
        self.assertEqual(
            validate_prim_path("/World/ProductionLike/TestCube"), "/World/ProductionLike/TestCube"
        )
        self.assertEqual(
            validate_prim_path("/OtherRoot/Scene/Camera_1"), "/OtherRoot/Scene/Camera_1"
        )

    def test_unsafe_or_property_paths_are_rejected(self) -> None:
        for path in (
            "World/Cube",
            "/",
            "/World/Cube.translate",
            "/World/../Cube",
            "/World/Bad-Name",
            "/World//Cube",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    validate_prim_path(path)

    def test_removal_protects_only_structural_roots(self) -> None:
        with self.assertRaises(ValueError):
            build_remove_prim_source("/World")
        source = build_remove_prim_source("/World/UserChosen/Prim")
        self.assertIn('path = "/World/UserChosen/Prim"', source)

    def test_create_source_uses_enumerated_type_and_quoted_path(self) -> None:
        source = build_create_prim_source("/World/AgentSceneLab/Cube", "Cube")
        self.assertIn('path = "/World/AgentSceneLab/Cube"', source)
        self.assertIn('prim_type = "Cube"', source)
        self.assertIn("Parent prim does not exist", source)
        self.assertNotIn("save", source.lower())
        self.assertNotIn("omni.client", source)
        compile(source, "<kit_prim_create>", "exec")

    def test_python_injection_like_paths_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_create_prim_source('/World/Cube";open("/tmp/x","w")', "Cube")

    def test_remove_source_is_valid_python(self) -> None:
        source = build_remove_prim_source("/World/AgentSceneLab/Cube")
        self.assertNotIn("save", source.lower())
        self.assertNotIn("omni.client", source)
        compile(source, "<kit_prim_remove>", "exec")


if __name__ == "__main__":
    unittest.main()
