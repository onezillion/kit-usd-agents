import json
import logging
import os
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import KitLabClient, KitLabClientError
from .experiments import (
    MAX_EVENT_LIMIT,
    MAX_LIST_LIMIT,
    MAX_LIST_OFFSET,
    MAX_NOTE_CHARS,
    MAX_OBJECTIVE_CHARS,
    MAX_PYTHON_SOURCE_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TAG_CHARS,
    MAX_TAGS,
    MAX_TITLE_CHARS,
    ExperimentError,
    ExperimentStore,
    invoke_and_record,
    utc_now,
)


LOGGER = logging.getLogger(__name__)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
DEVELOPMENT_EXECUTION = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)
SESSION_MUTATION = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
EXPERIMENT_MUTATION = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)

mcp = MCPServer("KHL Kit Lab Runtime")
client = KitLabClient()
experiments = ExperimentStore()


def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("ok", False):
        return payload

    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code", "KIT_LAB_ERROR")
        message = error.get("message", "Kit Lab operation failed")
        raise ToolError(f"{code}: {message}")

    raise ToolError(
        "Kit Lab operation failed: "
        + json.dumps(payload, ensure_ascii=False)[:4000]
    )


def _experiment_error(exc: ExperimentError) -> ToolError:
    return ToolError(f"EXPERIMENT_ERROR: {exc}")


async def _recorded_call(
    tool_name: str,
    arguments: dict[str, Any],
    operation: Any,
    *,
    python_source: str | None = None,
) -> dict[str, Any]:
    try:
        experiment_id = experiments.current_id()
        return await invoke_and_record(
            experiments,
            experiment_id,
            tool_name,
            arguments,
            operation,
            python_source=python_source,
        )
    except ExperimentError as exc:
        raise _experiment_error(exc) from exc


async def _get(tool_name: str, path: str) -> dict[str, Any]:
    async def operation() -> dict[str, Any]:
        try:
            return _tool_result(await client.get(path))
        except KitLabClientError as exc:
            raise ToolError(str(exc)) from exc

    return await _recorded_call(tool_name, {}, operation)


async def _post(
    tool_name: str,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    async def operation() -> dict[str, Any]:
        try:
            return _tool_result(await client.post(path, payload))
        except KitLabClientError as exc:
            raise ToolError(str(exc)) from exc

    return await _recorded_call(tool_name, payload, operation)


async def _runtime_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {"captured_at": utc_now()}
    for key, path in (
        ("status", "/khl/lab/status"),
        ("runtime_info", "/khl/lab/runtime/info"),
    ):
        try:
            snapshot[key] = await client.get(path)
        except KitLabClientError as exc:
            snapshot[key] = {"available": False, "error": str(exc)[:2_000]}
    return snapshot


def _experiment_result(operation: Any) -> dict[str, Any]:
    try:
        return operation()
    except ExperimentError as exc:
        raise _experiment_error(exc) from exc


@mcp.tool(title="Kit Lab status", annotations=READ_ONLY)
async def kit_lab_status() -> dict[str, Any]:
    """Check whether the persistent local Kit laboratory is reachable and ready."""
    return await _get("kit_lab_status", "/khl/lab/status")


@mcp.tool(title="Inspect Kit runtime", annotations=READ_ONLY)
async def kit_runtime_info() -> dict[str, Any]:
    """Read Kit/app versions and active renderer or multi-GPU settings."""
    return await _get("kit_runtime_info", "/khl/lab/runtime/info")


@mcp.tool(title="Summarize USD stage", annotations=READ_ONLY)
async def kit_stage_summary() -> dict[str, Any]:
    """Read a bounded summary of the active USD stage without modifying it."""
    return await _get("kit_stage_summary", "/khl/lab/stage/summary")


@mcp.tool(title="Inspect USD prim", annotations=READ_ONLY)
async def kit_prim_inspect(
    path: Annotated[str, Field(description="Absolute USD prim path, such as /World/Cube.")],
    include_attributes: bool = True,
    include_relationships: bool = True,
    include_metadata: bool = True,
) -> dict[str, Any]:
    """Inspect one existing prim's state, properties, metadata and transform ops."""
    return await _post(
        "kit_prim_inspect",
        "/khl/lab/prim/inspect",
        {
            "path": path,
            "include_attributes": include_attributes,
            "include_relationships": include_relationships,
            "include_metadata": include_metadata,
        },
    )


@mcp.tool(title="List Kit extensions", annotations=READ_ONLY)
async def kit_extensions_list(
    enabled_only: bool = False,
    search: str | None = None,
    limit: Annotated[int, Field(ge=1, le=5000)] = 500,
) -> dict[str, Any]:
    """List installed Kit extensions with optional enabled-state and text filters."""
    return await _post(
        "kit_extensions_list",
        "/khl/lab/extensions/list",
        {
            "enabled_only": enabled_only,
            "search": search,
            "limit": limit,
        },
    )


@mcp.tool(title="Read Kit setting", annotations=READ_ONLY)
async def kit_setting_get(
    path: Annotated[
        str,
        Field(description="Absolute Carb setting path, such as /renderer/multiGpu/enable."),
    ],
) -> dict[str, Any]:
    """Read one Carb setting from the active Kit process."""
    return await _post("kit_setting_get", "/khl/lab/settings/get", {"path": path})


@mcp.tool(title="Inspect active viewport", annotations=READ_ONLY)
async def kit_viewport_info() -> dict[str, Any]:
    """Read the active viewport's camera, resolution and render-product path."""
    return await _get("kit_viewport_info", "/khl/lab/viewport/info")


@mcp.tool(title="Execute unrestricted Kit Python", annotations=DEVELOPMENT_EXECUTION)
async def kit_execute_python(
    code: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_PYTHON_SOURCE_CHARS,
            description=(
                "Python source to execute inside the active Kit interpreter. "
                "This can modify or freeze the development Kit."
            ),
        ),
    ],
) -> dict[str, Any]:
    """Development escape hatch: execute unrestricted Python inside local Kit.

    Prefer deterministic Kit Lab tools when they cover the task. This tool can
    change the stage, settings, extensions, files, or process state and may hang
    Kit. It must only target the local development laboratory.
    """
    async def operation() -> dict[str, Any]:
        try:
            # Python exceptions are experimental results, so return their captured
            # traceback instead of converting them into an MCP transport error.
            return await client.post("/khl/lab/python/execute", {"code": code})
        except KitLabClientError as exc:
            raise ToolError(str(exc)) from exc

    return await _recorded_call(
        "kit_execute_python",
        {"code": code},
        operation,
        python_source=code,
    )


@mcp.tool(title="Reset Kit Python session", annotations=SESSION_MUTATION)
async def kit_reset_python_session() -> dict[str, Any]:
    """Clear variables retained by the Kit Lab persistent Python namespace."""
    return await _post("kit_reset_python_session", "/khl/lab/session/reset", {})


@mcp.tool(title="Start Kit experiment", annotations=EXPERIMENT_MUTATION)
async def kit_experiment_start(
    title: Annotated[str, Field(min_length=1, max_length=MAX_TITLE_CHARS)],
    objective: Annotated[str, Field(min_length=1, max_length=MAX_OBJECTIVE_CHARS)],
    tags: Annotated[list[str] | None, Field(max_length=MAX_TAGS)] = None,
) -> dict[str, Any]:
    """Create and activate one durable experiment record using the configured root."""
    try:
        experiments.ensure_can_start()
        snapshot = await _runtime_snapshot()
        manifest = experiments.start(title, objective, tags, snapshot)
        return {"active": True, "experiment": manifest}
    except ExperimentError as exc:
        raise _experiment_error(exc) from exc


@mcp.tool(title="Get current Kit experiment", annotations=READ_ONLY)
async def kit_experiment_current() -> dict[str, Any]:
    """Return the active experiment, or a clean inactive result."""
    return _experiment_result(experiments.current)


@mcp.tool(title="List Kit experiments", annotations=READ_ONLY)
async def kit_experiment_list(
    status: Literal["active", "finished"] | None = None,
    tag: Annotated[str | None, Field(max_length=MAX_TAG_CHARS)] = None,
    limit: Annotated[int, Field(ge=1, le=MAX_LIST_LIMIT)] = 20,
    offset: Annotated[int, Field(ge=0, le=MAX_LIST_OFFSET)] = 0,
) -> dict[str, Any]:
    """List bounded experiment summaries with optional status and tag filters."""
    return _experiment_result(
        lambda: experiments.list(status=status, tag=tag, limit=limit, offset=offset)
    )


@mcp.tool(title="Get Kit experiment", annotations=READ_ONLY)
async def kit_experiment_get(
    experiment_id: Annotated[str, Field(min_length=1, max_length=96)],
    event_limit: Annotated[int, Field(ge=0, le=MAX_EVENT_LIMIT)] = 100,
) -> dict[str, Any]:
    """Return one manifest and a bounded list of its most recent events."""
    return _experiment_result(lambda: experiments.get(experiment_id, event_limit))


@mcp.tool(title="Add Kit experiment note", annotations=EXPERIMENT_MUTATION)
async def kit_experiment_note(
    note: Annotated[str, Field(min_length=1, max_length=MAX_NOTE_CHARS)],
) -> dict[str, Any]:
    """Append a bounded plain-text note to the active experiment."""
    return _experiment_result(lambda: experiments.note(note))


@mcp.tool(title="Finish Kit experiment", annotations=EXPERIMENT_MUTATION)
async def kit_experiment_finish(
    summary: Annotated[str, Field(min_length=1, max_length=MAX_SUMMARY_CHARS)],
    outcome: Literal["success", "failed", "inconclusive", "cancelled"],
) -> dict[str, Any]:
    """Finish the active experiment, write summary.md and clear the current pointer."""
    return _experiment_result(lambda: experiments.finish(summary, outcome))


def main() -> None:
    host = os.environ.get("KIT_LAB_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("KIT_LAB_MCP_PORT", "9910"))

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("KIT_LAB_MCP_HOST must remain on loopback")
    if port < 1 or port > 65535:
        raise RuntimeError("KIT_LAB_MCP_PORT must be between 1 and 65535")

    LOGGER.info("Starting KHL Kit Lab Runtime MCP on %s:%d", host, port)
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path="/mcp",
    )


if __name__ == "__main__":
    main()
