import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import Client

from khl_kit_lab_mcp.experiments import DEFAULT_EXPERIMENT_ROOT, validate_experiment_id


EXPECTED_TOOLS = {
    "kit_lab_status",
    "kit_runtime_info",
    "kit_stage_summary",
    "kit_prim_inspect",
    "kit_extensions_list",
    "kit_setting_get",
    "kit_viewport_info",
    "kit_execute_python",
    "kit_reset_python_session",
    "kit_experiment_start",
    "kit_experiment_current",
    "kit_experiment_list",
    "kit_experiment_get",
    "kit_experiment_note",
    "kit_experiment_finish",
}


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
    root = Path(
        os.environ.get("KIT_LAB_EXPERIMENT_ROOT", DEFAULT_EXPERIMENT_ROOT)
    ).expanduser().resolve()
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
    if stage_event is None or python_event is None:
        raise SystemExit("Expected recorded tool events were not returned")
    for event in (stage_event, python_event):
        result_file = contained_record_file(directory, event.get("result_file"))
        if not result_file.is_file():
            raise SystemExit(f"Missing recorded result file: {result_file}")
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
    print("tool count: 15")
    print("expected tools: PASS")


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
                    "title": f"Phase 2C persistence verification {stamp}",
                    "objective": "Verify durable runtime MCP experiment recording.",
                    "tags": ["phase-2c", "verification"],
                },
            )
        )
        experiment_id = started["experiment"]["experiment_id"]
        print("experiment start: PASS")

        payload(
            await client.call_tool(
                "kit_experiment_note",
                {"note": "Live verifier started deterministic and Python checks."},
            )
        )
        print("experiment note: PASS")

        payload(await client.call_tool("kit_stage_summary", {}))
        print("kit_stage_summary: PASS")

        python_result = payload(
            await client.call_tool("kit_execute_python", {"code": "2 + 2"})
        )
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
                    "summary": "Phase 2C live persistence verification passed.",
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

    print("KIT_LAB_PHASE2C_LIVE_OK")


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
    print("KIT_LAB_PHASE2C_POST_RESTART_OK")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--post-restart", metavar="EXPERIMENT_ID")
    arguments = parser.parse_args()
    endpoint = os.environ.get("KIT_LAB_MCP_ENDPOINT", "http://127.0.0.1:9910/mcp")
    print("Endpoint:", endpoint)
    if arguments.post_restart:
        asyncio.run(post_restart_verification(endpoint, arguments.post_restart))
    else:
        asyncio.run(live_verification(endpoint))


if __name__ == "__main__":
    main()
