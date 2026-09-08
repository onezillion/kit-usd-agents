from __future__ import annotations

import ast
import json
import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = json.loads((ROOT / "mcp-services-user-local.json").read_text(encoding="utf-8"))
SERVICES = {service["id"]: service for service in INVENTORY["services"]}

OFFICIAL_MAIN = {
    "omni-ui": ROOT / "source/mcp/omni_ui_mcp/src/omni_ui_mcp/__main__.py",
    "kit": ROOT / "source/mcp/kit_mcp/src/kit_mcp/__main__.py",
    "usd-code": ROOT / "source/mcp/usd_code_mcp/src/usd_code_mcp/__main__.py",
    "isaacsim": ROOT / "source/mcp/isaacsim_mcp/src/isaacsim_mcp/__main__.py",
}
OFFICIAL_CONFIG = {
    "omni-ui": [ROOT / "source/mcp/omni_ui_mcp/workflow/config.yaml"],
    "kit": [
        ROOT / "source/mcp/kit_mcp/workflows/config.yaml",
        ROOT / "source/mcp/kit_mcp/workflows/local_config.yaml",
    ],
    "usd-code": [
        ROOT / "source/mcp/usd_code_mcp/workflow/config.yaml",
        ROOT / "source/mcp/usd_code_mcp/workflow/local_config.yaml",
    ],
    "isaacsim": [ROOT / "source/mcp/isaacsim_mcp/workflows/config.yaml"],
}
EXPECTED_COUNTS = {"omni-ui": 10, "kit": 12, "usd-code": 7, "isaacsim": 5, "kit-lab": 20}


def main_tool_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        values = [item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]
        for index, value in enumerate(values[:-1]):
            if value == "--tool_names":
                names.append(values[index + 1])
    return names


def config_tool_names(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    in_list = False
    names: list[str] = []
    for line in lines:
        if re.fullmatch(r"\s+tool_names:\s*", line):
            in_list = True
            continue
        if in_list:
            match = re.fullmatch(r"\s+-\s+([A-Za-z0-9_]+)\s*", line)
            if match:
                names.append(match.group(1))
                continue
            if line.strip() and not line.lstrip().startswith("#"):
                break
    return names


class InventoryTests(unittest.TestCase):
    def test_service_registry_is_exact(self) -> None:
        self.assertEqual(list(SERVICES), ["omni-ui", "kit", "usd-code", "isaacsim", "kit-lab"])
        self.assertEqual([service["port"] for service in SERVICES.values()], [9901, 9902, 9903, 9904, 9910])
        for service_id, expected_count in EXPECTED_COUNTS.items():
            tools = SERVICES[service_id]["tools"]
            self.assertEqual(len(tools), expected_count, service_id)
            self.assertEqual(len(tools), len(set(tools)), service_id)

    def test_official_launchers_and_configs_match_inventory(self) -> None:
        for service_id, main_path in OFFICIAL_MAIN.items():
            expected = SERVICES[service_id]["tools"]
            self.assertEqual(main_tool_names(main_path), expected, service_id)
            for config_path in OFFICIAL_CONFIG[service_id]:
                self.assertEqual(config_tool_names(config_path), expected, f"{service_id}: {config_path}")

    def test_kit_lab_server_matches_inventory(self) -> None:
        server = (ROOT / "source/mcp/khl_kit_lab_mcp/src/khl_kit_lab_mcp/server.py").read_text(encoding="utf-8")
        names = re.findall(r'@mcp\.tool\([^\n]+_tool_options\("([a-z0-9_]+)"\)', server)
        self.assertEqual(names, SERVICES["kit-lab"]["tools"])

    def test_kit_lab_versions_docs_verifier_and_reviewer_match(self) -> None:
        package = ROOT / "source/mcp/khl_kit_lab_mcp"
        expected = set(SERVICES["kit-lab"]["tools"])
        verifier = ast.parse((package / "verify.py").read_text())
        inventory = next(n.value for n in verifier.body
                         if isinstance(n, ast.Assign)
                         and any(isinstance(t, ast.Name) and t.id == "EXPECTED_TOOLS"
                                 for t in n.targets))
        self.assertEqual(ast.literal_eval(inventory), expected)
        for path in (ROOT / "MCP_USER_LOCAL.md", package / "README.md"):
            names = set(re.findall(r"^- `(kit_[a-z_]+)`$", path.read_text(), re.MULTILINE))
            self.assertEqual(names, expected, path)
        for path in (package / "pyproject.toml", package / "src/khl_kit_lab_mcp/__init__.py"):
            self.assertIn(f'"{SERVICES["kit-lab"]["expected_version"]}"', path.read_text())
        spec = importlib.util.spec_from_file_location(
            "kit_lab_policy_for_sync", package / "src/khl_kit_lab_mcp/policy.py")
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        reviewer = (ROOT / ".github/agents/glm-reviewer.agent.md").read_text()
        allowlist = re.findall(r"^  - kit-lab-runtime/(.+)$", reviewer, re.MULTILINE)
        self.assertTrue(allowlist)
        for name in allowlist:
            self.assertIn(name, expected)
            self.assertEqual(policy.TOOL_POLICIES[name]["class"], "READ_ONLY")
        self.assertIn("  - kit-dev-mcp/*", reviewer)
        self.assertIn("model: GLM 5.2", reviewer)

    def test_wrappers_are_local_only_and_disable_usage_logging(self) -> None:
        for service in SERVICES.values():
            wrapper = ROOT / service["wrapper"]
            self.assertTrue(wrapper.is_file(), wrapper)
            text = wrapper.read_text(encoding="utf-8")
            if service["id"] != "kit-lab":
                self.assertIn('MCP_HOST="127.0.0.1"', text)
                self.assertIn(f'MCP_PORT="{service["port"]}"', text)
                self.assertIn("DISABLE_USAGE_LOGGING", text)
                self.assertNotIn("0.0.0.0", text)
                self.assertNotIn("nvidia-kit-mcp", text)

    def test_manager_registry_and_documentation_are_synchronized(self) -> None:
        manager = (ROOT / "manage-mcps-user-local.sh").read_text(encoding="utf-8")
        docs = (ROOT / "MCP_USER_LOCAL.md").read_text(encoding="utf-8")
        for service in SERVICES.values():
            self.assertIn(service["id"], manager)
            self.assertIn(service["wrapper"], manager)
            self.assertIn(str(service["port"]), manager)
            self.assertIn(service["display_name"], docs)
            for tool in service["tools"]:
                self.assertIn(f"`{tool}`", docs)


class SecurityAndHygieneTests(unittest.TestCase):
    def test_env_file_is_not_tracked(self) -> None:
        completed = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "source/mcp/.env"],
            cwd=ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertNotEqual(completed.returncode, 0)

    def test_runtime_artifacts_are_not_tracked(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout.splitlines()
        forbidden = re.compile(r"(^|/)(\.venv|venv|logs?|run|cache|tmp)(/|$)|\.(pid|state)$")
        offenders = [path for path in tracked if forbidden.search(path)]
        self.assertEqual(offenders, [])

    def test_new_management_files_have_no_credential_literals(self) -> None:
        paths = [
            ROOT / ".gitignore",
            ROOT / "mcp-services-user-local.json",
            ROOT / "requirements-mcps-user-local.txt",
            ROOT / "manage-mcps-user-local.sh",
            ROOT / "setup-mcps-user-local.sh",
            ROOT / "verify-mcps-user-local.py",
            ROOT / "mcp-operational-log.py",
            ROOT / "MCP_USER_LOCAL.md",
            ROOT / "tests/test_mcp_user_local.py",
        ]
        patterns = (
            re.compile(r"nvapi-[A-Za-z0-9_-]{16,}"),
            re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
            re.compile(r"AKIA[0-9A-Z]{16}"),
        )
        offenders: list[str] = []
        for path in paths:
            text = path.read_text(encoding="utf-8")
            if any(pattern.search(text) for pattern in patterns):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_operational_logger_discards_tool_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "service.log"
            sample = """server started\ncalled with request: private value\nsearch with query: private value\nretrieved context for 'private value'\nserver stopped\n"""
            subprocess.run(
                [sys.executable, str(ROOT / "mcp-operational-log.py"), str(log_path)],
                input=sample,
                text=True,
                check=True,
            )
            logged = log_path.read_text(encoding="utf-8")
            self.assertEqual(logged, "server started\nserver stopped\n")


if __name__ == "__main__":
    unittest.main()
