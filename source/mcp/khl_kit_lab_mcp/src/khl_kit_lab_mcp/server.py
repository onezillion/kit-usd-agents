import json
import logging
import os
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
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
from .live_stage import (
    MAX_PRIM_PATH_CHARS,
    PRIM_PATH_PATTERN,
    PrimType,
    build_create_prim_source,
    build_remove_prim_source,
)
from .policy import (
    SERVER_INSTRUCTIONS,
    policy_payload,
    tool_annotations,
    tool_description,
    tool_meta,
)


LOGGER = logging.getLogger(__name__)


def _policy_annotations(tool_name: str) -> ToolAnnotations:
    return ToolAnnotations(**tool_annotations(tool_name))


def _tool_options(tool_name: str) -> dict[str, Any]:
    return {
        "description": tool_description(tool_name),
        "annotations": _policy_annotations(tool_name),
        "meta": tool_meta(tool_name),
    }


mcp = MCPServer(
    "KHL Kit Lab Runtime",
    title="KHL Kit Lab Runtime",
    description="Controlled local MCP adapter for one persistent Omniverse Kit laboratory.",
    instructions=SERVER_INSTRUCTIONS,
    version=__version__,
)
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

    raise ToolError("Kit Lab operation failed: " + json.dumps(payload, ensure_ascii=False)[:4000])


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


async def _controlled_python_post(
    tool_name: str,
    arguments: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    """Run server-generated, validated Kit Python as a deterministic MCP operation."""

    async def operation() -> dict[str, Any]:
        try:
            return _tool_result(await client.post("/khl/lab/python/execute", {"code": source}))
        except KitLabClientError as exc:
            raise ToolError(str(exc)) from exc

    return await _recorded_call(
        tool_name,
        arguments,
        operation,
        python_source=source,
    )


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


@mcp.tool(title="Read Kit Lab policy", **_tool_options("kit_lab_policy"))
async def kit_lab_policy() -> dict[str, Any]:
    """Return the authoritative policy payload without contacting Kit."""
    return policy_payload()


@mcp.tool(title="Kit Lab status", **_tool_options("kit_lab_status"))
async def kit_lab_status() -> dict[str, Any]:
    """Check whether the persistent local Kit laboratory is reachable and ready."""
    return await _get("kit_lab_status", "/khl/lab/status")


@mcp.tool(title="Inspect Kit runtime", **_tool_options("kit_runtime_info"))
async def kit_runtime_info() -> dict[str, Any]:
    """Read Kit/app versions and active renderer or multi-GPU settings."""
    return await _get("kit_runtime_info", "/khl/lab/runtime/info")


@mcp.tool(title="Summarize USD stage", **_tool_options("kit_stage_summary"))
async def kit_stage_summary() -> dict[str, Any]:
    """Read a bounded summary of the active USD stage without modifying it."""
    return await _get("kit_stage_summary", "/khl/lab/stage/summary")


@mcp.tool(title="Inspect USD prim", **_tool_options("kit_prim_inspect"))
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


@mcp.tool(title="List Kit extensions", **_tool_options("kit_extensions_list"))
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


@mcp.tool(title="Read Kit setting", **_tool_options("kit_setting_get"))
async def kit_setting_get(
    path: Annotated[
        str,
        Field(description="Absolute Carb setting path, such as /renderer/multiGpu/enable."),
    ],
) -> dict[str, Any]:
    """Read one Carb setting from the active Kit process."""
    return await _post("kit_setting_get", "/khl/lab/settings/get", {"path": path})


@mcp.tool(title="Inspect active viewport", **_tool_options("kit_viewport_info"))
async def kit_viewport_info() -> dict[str, Any]:
    """Read the active viewport's camera, resolution and render-product path."""
    return await _get("kit_viewport_info", "/khl/lab/viewport/info")


@mcp.tool(title="Create live USD prim", **_tool_options("kit_prim_create"))
async def kit_prim_create(
    path: Annotated[
        str,
        Field(
            min_length=2,
            max_length=MAX_PRIM_PATH_CHARS,
            pattern=PRIM_PATH_PATTERN.pattern,
            description=(
                "Absolute live USD prim path. For tests or unspecified locations, prefer "
                "/World/AgentSceneLab/<name>."
            ),
        ),
    ],
    prim_type: PrimType = "Xform",
) -> dict[str, Any]:
    """Create one new live prim using validated server-generated Kit Python."""
    try:
        source = build_create_prim_source(path, prim_type)
    except ValueError as exc:
        raise ToolError(f"INVALID_PRIM_REQUEST: {exc}") from exc
    return await _controlled_python_post(
        "kit_prim_create",
        {"path": path, "prim_type": prim_type},
        source,
    )


@mcp.tool(title="Remove live USD prim", **_tool_options("kit_prim_remove"))
async def kit_prim_remove(
    path: Annotated[
        str,
        Field(
            min_length=2,
            max_length=MAX_PRIM_PATH_CHARS,
            pattern=PRIM_PATH_PATTERN.pattern,
            description="Exact absolute path of the live prim subtree to remove.",
        ),
    ],
) -> dict[str, Any]:
    """Remove one exact live prim using validated server-generated Kit Python."""
    try:
        source = build_remove_prim_source(path)
    except ValueError as exc:
        raise ToolError(f"INVALID_PRIM_REQUEST: {exc}") from exc
    return await _controlled_python_post(
        "kit_prim_remove",
        {"path": path},
        source,
    )


@mcp.tool(title="Execute unrestricted Kit Python", **_tool_options("kit_execute_python"))
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

    Prefer deterministic Kit Lab tools when they cover the task. The runtime is
    technically capable of broad effects, so the MCP policy forbids persistence,
    external package injection, blocking loops, and policy bypass even after the
    user authorizes Python. It must target only the local development laboratory.
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


@mcp.tool(title="Reset Kit Python session", **_tool_options("kit_reset_python_session"))
async def kit_reset_python_session() -> dict[str, Any]:
    """Clear variables retained by the Kit Lab persistent Python namespace."""
    return await _post("kit_reset_python_session", "/khl/lab/session/reset", {})


@mcp.tool(title="Start Kit experiment", **_tool_options("kit_experiment_start"))
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


@mcp.tool(title="Get current Kit experiment", **_tool_options("kit_experiment_current"))
async def kit_experiment_current() -> dict[str, Any]:
    """Return the active experiment, or a clean inactive result."""
    return _experiment_result(experiments.current)


@mcp.tool(title="List Kit experiments", **_tool_options("kit_experiment_list"))
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


@mcp.tool(title="Get Kit experiment", **_tool_options("kit_experiment_get"))
async def kit_experiment_get(
    experiment_id: Annotated[str, Field(min_length=1, max_length=96)],
    event_limit: Annotated[int, Field(ge=0, le=MAX_EVENT_LIMIT)] = 100,
) -> dict[str, Any]:
    """Return one manifest and a bounded list of its most recent events."""
    return _experiment_result(lambda: experiments.get(experiment_id, event_limit))


@mcp.tool(title="Add Kit experiment note", **_tool_options("kit_experiment_note"))
async def kit_experiment_note(
    note: Annotated[str, Field(min_length=1, max_length=MAX_NOTE_CHARS)],
) -> dict[str, Any]:
    """Append a bounded plain-text note to the active experiment."""
    return _experiment_result(lambda: experiments.note(note))


@mcp.tool(title="Finish Kit experiment", **_tool_options("kit_experiment_finish"))
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
