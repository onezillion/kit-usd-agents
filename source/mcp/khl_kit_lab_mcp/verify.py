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
from khl_kit_lab_mcp.policy import POLICY_FINGERPRINT, SERVER_INSTRUCTIONS, TOOL_POLICIES


EXPECTED_TOOLS = {
    "kit_lab_policy",
    "kit_lab_status",
    "kit_runtime_info",
    "kit_stage_summary",
    "kit_prim_inspect",
    "kit_extensions_list",
    "kit_setting_get",
    "kit_viewport_info",
    "kit_prim_create",
    "kit_prim_remove",
    "kit_execute_python",
    "kit_reset_python_session",
    "kit_experiment_start",
    "kit_experiment_current",
    "kit_experiment_list",
    "kit_experiment_get",
    "kit_experiment_note",
    "kit_experiment_finish",
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
    stage_event = next(
        (event for event in events if event.get("tool") == "kit_stage_summary"),
        None,
    )
    python_event = next(
        (event for event in events if event.get("tool") == "kit_execute_python"),
        None,
    )
    create_event = next(
        (event for event in events if event.get("tool") == "kit_prim_create"),
        None,
    )
    remove_event = next(
        (event for event in events if event.get("tool") == "kit_prim_remove"),
        None,
    )
    if any(event is None for event in (stage_event, create_event, remove_event, python_event)):
        raise SystemExit("Expected recorded tool events were not returned")
    for event in (stage_event, create_event, remove_event, python_event):
        assert event is not None
        result_file = contained_record_file(directory, event.get("result_file"))
        if not result_file.is_file():
            raise SystemExit(f"Missing recorded result file: {result_file}")
    for event in (create_event, remove_event):
        assert event is not None
        source_file = contained_record_file(directory, event.get("python_source_file"))
        if not source_file.is_file():
            raise SystemExit(f"Missing controlled Python source: {source_file}")
        source_text = source_file.read_text(encoding="utf-8")
        if "open(" in source_text or "omni.client" in source_text or ".Save(" in source_text:
            raise SystemExit("Controlled live-mutation source contains a persistence API")
    assert python_event is not None
    source_file = contained_record_file(directory, python_event.get("python_source_file"))
    if not source_file.is_file() or source_file.read_text(encoding="utf-8") != "2 + 2":
        raise SystemExit("Recorded Python source is missing or incorrect")


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
        metadata = tool.meta or {}
        policy_meta = metadata.get("khl_policy", {})
        if policy_meta.get("class") != expected["class"]:
            raise SystemExit(f"Tool policy metadata mismatch: {tool.name}")
        if policy_meta.get("fingerprint") != POLICY_FINGERPRINT:
            raise SystemExit(f"Tool policy fingerprint mismatch: {tool.name}")
    print("tool count: 18")
    print("expected tools: PASS")
    print("instructions/descriptions/annotations/policy metadata: PASS")


async def verify_live_prim_mutation(client: Client) -> None:
    playground = "/World/AgentSceneLab"
    suffix = "Phase3PolicyCube" + datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    cube_path = f"{playground}/{suffix}"
    parent_created = False
    cube_created = False

    parent = await client.call_tool("kit_prim_inspect", {"path": playground})
    if parent.is_error:
        payload(
            await client.call_tool(
                "kit_prim_create",
                {"path": playground, "prim_type": "Xform"},
            )
        )
        parent_created = True

    try:
        created = payload(
            await client.call_tool(
                "kit_prim_create",
                {"path": cube_path, "prim_type": "Cube"},
            )
        )
        if cube_path not in json.dumps(created, ensure_ascii=False):
            raise SystemExit("Created-prim result did not contain the requested path")
        cube_created = True

        inspected = payload(
            await client.call_tool(
                "kit_prim_inspect",
                {"path": cube_path},
            )
        )
        if cube_path not in json.dumps(inspected, ensure_ascii=False):
            raise SystemExit("Independent prim inspection did not contain the requested path")
        print("temporary live prim create/inspect: PASS")
    finally:
        cleanup_errors: list[str] = []
        if cube_created:
            try:
                payload(await client.call_tool("kit_prim_remove", {"path": cube_path}))
            except BaseException as exc:
                cleanup_errors.append(f"cube cleanup failed: {exc}")
        if parent_created:
            try:
                payload(await client.call_tool("kit_prim_remove", {"path": playground}))
            except BaseException as exc:
                cleanup_errors.append(f"playground cleanup failed: {exc}")
        if cleanup_errors:
            raise SystemExit("; ".join(cleanup_errors))

    removed = await client.call_tool("kit_prim_inspect", {"path": cube_path})
    if not removed.is_error:
        raise SystemExit("Temporary cube remained inspectable after cleanup")
    print("temporary live prim cleanup: PASS")


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
                    "title": f"Phase 3 MCP policy verification {stamp}",
                    "objective": (
                        "Verify model-visible policy metadata, controlled live mutation, "
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
                {"note": "Live verifier started policy, mutation, cleanup, and Python checks."},
            )
        )
        print("experiment note: PASS")

        payload(await client.call_tool("kit_stage_summary", {}))
        print("kit_stage_summary: PASS")

        await verify_live_prim_mutation(client)

        python_result = payload(await client.call_tool("kit_execute_python", {"code": "2 + 2"}))
        if not python_result.get("ok", False):
            raise SystemExit(f"Harmless Python expression failed: {python_result}")
        print("kit_execute_python: PASS")

        retrieved = payload(
            await client.call_tool(
                "kit_experiment_get",
                {"experiment_id": experiment_id, "event_limit": 100},
            )
        )
        verify_record_files(experiment_id, retrieved["events"])
        print("events/results/source files: PASS")

        payload(
            await client.call_tool(
                "kit_experiment_finish",
                {
                    "summary": (
                        "Phase 3 MCP policy metadata, controlled live mutation, cleanup, "
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

    print("KIT_LAB_PHASE3_LIVE_OK")


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
    print("KIT_LAB_PHASE3_POST_RESTART_OK")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--post-restart", metavar="EXPERIMENT_ID")
    arguments = parser.parse_args()
    endpoint = os.environ.get("KIT_LAB_MCP_ENDPOINT", "http://127.0.0.1:9910/mcp")
    installed_mcp = version("mcp")
    print("MCP SDK:", installed_mcp)
    if installed_mcp != EXPECTED_MCP_SDK_VERSION:
        raise SystemExit(f"Expected mcp=={EXPECTED_MCP_SDK_VERSION}, found mcp=={installed_mcp}")
    print("Endpoint:", endpoint)
    if arguments.post_restart:
        asyncio.run(post_restart_verification(endpoint, arguments.post_restart))
    else:
        asyncio.run(live_verification(endpoint))


if __name__ == "__main__":
    main()
