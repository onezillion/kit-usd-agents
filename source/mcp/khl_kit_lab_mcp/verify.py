import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from mcp import Client

from khl_kit_lab_mcp.experiments import DEFAULT_EXPERIMENT_ROOT, validate_experiment_id
from khl_kit_lab_mcp.policy import SERVER_INSTRUCTIONS, TOOL_POLICIES, tool_meta


EXPECTED_TOOLS = {
    "kit_lab_policy",
    "kit_lab_status",
    "kit_runtime_info",
    "kit_stage_summary",
    "kit_extensions_list",
    "kit_viewport_info",
    "kit_execute_python",
    "kit_reset_python_session",
    "kit_experiment_start",
    "kit_experiment_current",
    "kit_experiment_list",
    "kit_experiment_get",
    "kit_experiment_note",
    "kit_experiment_finish",
    "kit_lifecycle_config", "kit_status", "kit_start", "kit_stop", "kit_restart", "kit_log_paths",
}
EXPECTED_MCP_SDK_VERSION = "2.1.1"


def payload(result: Any) -> dict[str, Any]:
    if result.is_error:
        raise SystemExit(f"MCP tool failed: {result.content}")
    structured = result.structured_content
    if isinstance(structured, dict):
        return structured
    for content in result.content:
        text = getattr(content, "text", None)
        if text:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
    raise SystemExit("MCP tool returned no object payload")


def experiment_directory(experiment_id: str) -> Path:
    validate_experiment_id(experiment_id)
    root = (
        Path(os.environ.get("KIT_LAB_EXPERIMENT_ROOT", DEFAULT_EXPERIMENT_ROOT))
        .expanduser()
        .resolve()
    )
    directory = (root / experiment_id).resolve()
    if root not in directory.parents:
        raise SystemExit("Experiment path escaped the configured root")
    return directory


def contained_record_file(directory: Path, reference: Any) -> Path:
    raw_path = directory / str(reference or "")
    if raw_path.is_symlink():
        raise SystemExit("Recorded file reference must not be a symlink")
    path = raw_path.resolve()
    if directory not in path.parents:
        raise SystemExit("Recorded file reference escaped the experiment directory")
    return path


def verify_record_files(experiment_id: str, events: list[dict[str, Any]]) -> None:
    directory = experiment_directory(experiment_id)
    # Historical events may name retired tools. Validate evidence, not today's inventory.
    if not events:
        raise SystemExit("Expected recorded events were not returned")
    for event in events:
        if event.get("tool") and not event.get("result_file"):
            raise SystemExit("Tool event is missing its recorded result reference")
        for field in ("result_file", "python_source_file"):
            reference = event.get(field)
            if reference and not contained_record_file(directory, reference).is_file():
                raise SystemExit(f"Missing recorded evidence: {field}")


async def check_tools(client: Client) -> None:
    listed = await client.list_tools()
    names = {tool.name for tool in listed.tools}
    if names != EXPECTED_TOOLS:
        missing = sorted(EXPECTED_TOOLS - names)
        unexpected = sorted(names - EXPECTED_TOOLS)
        raise SystemExit(f"Tool mismatch; missing={missing}, unexpected={unexpected}")
    if client.instructions != SERVER_INSTRUCTIONS:
        raise SystemExit("Server instructions were not exposed exactly as configured")
    for tool in listed.tools:
        expected = TOOL_POLICIES[tool.name]
        if tool.description != expected["description"]:
            raise SystemExit(f"Tool description mismatch: {tool.name}")
        annotations = tool.annotations
        if annotations is None:
            raise SystemExit(f"Tool annotations missing: {tool.name}")
        for field, value in expected["annotations"].items():
            if getattr(annotations, field) != value:
                raise SystemExit(f"Tool annotation mismatch: {tool.name}.{field}")
        if tool.meta != tool_meta(tool.name):
            raise SystemExit(f"Tool policy metadata mismatch: {tool.name}")
        schema = tool.input_schema
        if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
            raise SystemExit(f"Tool input schema missing or invalid: {tool.name}")
        if tool.name == "kit_stage_summary":
            option = schema["properties"].get("include_statistics", {})
            if option.get("type") != "boolean" or option.get("default") is not False:
                raise SystemExit("Stage summary must default to skipping statistics")
    print("tool count: 20")
    print("expected tools: PASS")
    print("instructions/descriptions/annotations/policy metadata: PASS")


def validate_summary(response: dict[str, Any]) -> None:
    result = response.get("result", {})
    if result.get("statistics_computed") is not False:
        raise SystemExit("Bridge activation pending or invalid summary: statistics were not explicitly skipped")
    if result.get("prim_count") is not None or result.get("type_counts") is not None:
        raise SystemExit("Default stage summary must return null uncomputed counts")


async def read_only_verification(endpoint: str) -> None:
    async with Client(endpoint) as client:
        await check_tools(client)
        payload(await client.call_tool("kit_lab_status", {}))
        validate_summary(payload(await client.call_tool("kit_stage_summary", {})))
        payload(await client.call_tool("kit_experiment_current", {}))
    print("KIT_LAB_READ_ONLY_OK")


async def lifecycle_verification(endpoint: str) -> None:
    async with Client(endpoint) as client:
        config = payload(await client.call_tool("kit_lifecycle_config", {}))
        status = payload(await client.call_tool("kit_status", {}))
        logs = payload(await client.call_tool("kit_log_paths", {}))
    if config.get("inspection", {}).get("denied_candidates"):
        raise SystemExit("Lifecycle service cannot inspect Kit candidates")
    if status.get("state") != "READY" or status.get("kit_id") != "nchc-kit-dev-main":
        raise SystemExit(f"Identified Kit is not ready: {status}")
    if (logs.get("process") != status.get("process") or logs.get("contents_read") is not False
            or logs.get("association") != "matching_bridge_identity" or not logs.get("native_log_path")):
        raise SystemExit(f"Native log association failed or Kit changed during verification: {logs}")
    print("lifecycle:", json.dumps({"status": status, "logs": logs}))
    print("KIT_LAB_LIFECYCLE_OK")


async def live_verification(endpoint: str) -> None:
    async with Client(endpoint) as client:
        await check_tools(client)

        current = payload(await client.call_tool("kit_experiment_current", {}))
        if current.get("active"):
            active_id = current.get("experiment", {}).get("experiment_id")
            raise SystemExit(
                f"Experiment {active_id} is already active; finish it before running verification"
            )

        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        started = payload(
            await client.call_tool(
                "kit_experiment_start",
                {
                    "title": f"Kit Lab Python recording verification {stamp}",
                    "objective": (
                        "Verify model-visible policy metadata, Python execution, "
                        "cleanup, and durable experiment recording."
                    ),
                    "tags": ["phase-3", "policy", "verification"],
                },
            )
        )
        experiment_id = started["experiment"]["experiment_id"]
        print("experiment start: PASS")

        payload(
            await client.call_tool(
                "kit_experiment_note",
                {"note": "Live verifier started policy and Python recording checks."},
            )
        )
        print("experiment note: PASS")

        validate_summary(payload(await client.call_tool("kit_stage_summary", {})))
        print("kit_stage_summary: PASS")

        python_result = payload(await client.call_tool("kit_execute_python", {"code": "2 + 2"}))
        if not python_result.get("ok", False) or python_result.get("result") != "4":
            raise SystemExit(f"Harmless Python expression failed: {python_result}")
        print("kit_execute_python: PASS")

        retrieved = payload(
            await client.call_tool(
                "kit_experiment_get",
                {"experiment_id": experiment_id, "event_limit": 100},
            )
        )
        verify_record_files(experiment_id, retrieved["events"])
        events = retrieved["events"]
        if not any(e.get("tool") == "kit_stage_summary" for e in events):
            raise SystemExit("Stage summary evidence was not recorded")
        python_event = next((e for e in events if e.get("tool") == "kit_execute_python"), {})
        source_file = contained_record_file(
            experiment_directory(experiment_id), python_event.get("python_source_file"))
        if not source_file.is_file() or source_file.read_text(encoding="utf-8") != "2 + 2":
            raise SystemExit("Python source evidence is missing or incorrect")
        print("events/results/source files: PASS")

        payload(
            await client.call_tool(
                "kit_experiment_finish",
                {
                    "summary": (
                        "Phase 3 MCP policy metadata, Python execution, cleanup, "
                        "and Phase 2C persistence verification passed."
                    ),
                    "outcome": "success",
                },
            )
        )
        completed = payload(
            await client.call_tool(
                "kit_experiment_get",
                {"experiment_id": experiment_id, "event_limit": 100},
            )
        )
        if completed["experiment"].get("status") != "finished":
            raise SystemExit("Completed experiment was not retrievable as finished")
        print("finish/retrieve: PASS")
        print("experiment ID:", experiment_id)
        print("POST_RESTART_COMMAND:")
        print(f"./verify-user-local.sh --post-restart {experiment_id}")

    print("KIT_LAB_LIVE_OK")


async def post_restart_verification(endpoint: str, experiment_id: str) -> None:
    validate_experiment_id(experiment_id)
    async with Client(endpoint) as client:
        await check_tools(client)
        retrieved = payload(
            await client.call_tool(
                "kit_experiment_get",
                {"experiment_id": experiment_id, "event_limit": 100},
            )
        )
        manifest = retrieved.get("experiment", {})
        if manifest.get("experiment_id") != experiment_id:
            raise SystemExit("Retrieved experiment ID did not match")
        if manifest.get("status") != "finished":
            raise SystemExit("Verification experiment is not finished")
        verify_record_files(experiment_id, retrieved.get("events", []))
        directory = experiment_directory(experiment_id)
        if not (directory / "summary.md").is_file():
            raise SystemExit("summary.md is missing after restart")
    print("experiment ID:", experiment_id)
    print("KIT_LAB_POST_RESTART_OK")


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--post-restart", metavar="EXPERIMENT_ID")
    mode.add_argument("--full", action="store_true",
                      help="Opt in to durable experiment writes and Kit Python execution (2 + 2).")
    mode.add_argument("--lifecycle", action="store_true", help="Read-only baseline plus configured lifecycle identity/log checks")
    arguments = parser.parse_args()
    endpoint = os.environ.get("KIT_LAB_MCP_ENDPOINT", "http://127.0.0.1:9910/mcp")
    installed_mcp = version("mcp")
    print("MCP SDK:", installed_mcp)
    if installed_mcp != EXPECTED_MCP_SDK_VERSION:
        raise SystemExit(f"Expected mcp=={EXPECTED_MCP_SDK_VERSION}, found mcp=={installed_mcp}")
    print("Endpoint:", endpoint)
    if arguments.post_restart:
        asyncio.run(post_restart_verification(endpoint, arguments.post_restart))
    elif arguments.full:
        asyncio.run(live_verification(endpoint))
    else:
        asyncio.run(read_only_verification(endpoint))
        if arguments.lifecycle:
            asyncio.run(lifecycle_verification(endpoint))


if __name__ == "__main__":
    main()
