import asyncio
import json
import logging
import math
import os
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .client import KitLabClient, KitLabClientError
from .lifecycle import KIT_ID, KitLifecycle, LifecycleError, load_config, mutation_lock
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
from .policy import (
    SERVER_INSTRUCTIONS,
    policy_payload,
    tool_annotations,
    tool_description,
    tool_meta,
)
from .profiles import ProfileStore, ProfileStoreError


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
profiles = ProfileStore()
_lifecycle_instance: KitLifecycle | None = None
_background_control_tasks: set[asyncio.Task] = set()


def _lifecycle() -> KitLifecycle:
    global _lifecycle_instance
    config = load_config()
    if _lifecycle_instance is None or _lifecycle_instance.config != config:
        _lifecycle_instance = KitLifecycle(config)
    return _lifecycle_instance


async def _lifecycle_operation(operation, kit_id: str):
    if kit_id != KIT_ID:
        raise ToolError(f"UNKNOWN_KIT_ID: Only {KIT_ID} is configured")
    try:
        return await operation(_lifecycle())
    except (LifecycleError, OSError) as exc:
        raise ToolError(f"{getattr(exc, 'code', 'OS_ERROR')}: {exc}") from exc


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


async def _shielded_control_post(
    tool_name: str,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Keep the lifecycle advisory lock and recording alive after client cancellation."""
    experiment_id = experiments.current_id()

    async def worker() -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            try:
                with mutation_lock():
                    return _tool_result(await client.post(path, payload))
            except (KitLabClientError, LifecycleError) as exc:
                raise ToolError(f"{getattr(exc, 'code', 'KIT_LAB_ERROR')}: {exc}") from exc

        return await invoke_and_record(experiments, experiment_id, tool_name, payload, operation)

    task = asyncio.create_task(worker(), name=f"{tool_name}-control")
    _background_control_tasks.add(task)
    task.add_done_callback(_background_control_tasks.discard)
    return await asyncio.shield(task)


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
async def kit_stage_summary(
    include_statistics: Annotated[
        bool, Field(description="Opt in to a full stage.Traverse() count; may be expensive.")
    ] = False,
) -> dict[str, Any]:
    """Read basic stage context; compute full traversal statistics only on request."""
    # POST deliberately fails on old bridges instead of invoking their full-traversal GET.
    return await _post(
        "kit_stage_summary", "/khl/lab/stage/summary",
        {"include_statistics": include_statistics},
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


@mcp.tool(title="Inspect installed Kit extension", **_tool_options("kit_extension_inspect"))
async def kit_extension_inspect(
    extension: Annotated[
        str,
        Field(
            min_length=1,
            max_length=160,
            pattern=r"^[A-Za-z0-9_.]+(?:-[A-Za-z][A-Za-z0-9_.]*)?$",
            description="Canonical unversioned extension identity, retaining an optional Kit tag.",
        ),
    ],
) -> dict[str, Any]:
    """Resolve one local extension and its complete mutation-safety evidence."""
    return await _post(
        "kit_extension_inspect", "/khl/lab/extensions/inspect", {"extension": extension}
    )


@mcp.tool(title="Enable installed Kit extension", **_tool_options("kit_extension_enable"))
async def kit_extension_enable(
    extension: Annotated[
        str, Field(min_length=1, max_length=160,
                   pattern=r"^[A-Za-z0-9_.]+(?:-[A-Za-z][A-Za-z0-9_.]*)?$")
    ],
) -> dict[str, Any]:
    """Enable one safely resolved local extension; never install from a registry."""
    return await _shielded_control_post(
        "kit_extension_enable", "/khl/lab/extensions/enable", {"extension": extension}
    )


@mcp.tool(title="Disable installed Kit extension", **_tool_options("kit_extension_disable"))
async def kit_extension_disable(
    extension: Annotated[
        str, Field(min_length=1, max_length=160,
                   pattern=r"^[A-Za-z0-9_.]+(?:-[A-Za-z][A-Za-z0-9_.]*)?$")
    ],
) -> dict[str, Any]:
    """Disable one safe extension without cascading to active dependents."""
    return await _shielded_control_post(
        "kit_extension_disable", "/khl/lab/extensions/disable", {"extension": extension}
    )


@mcp.tool(title="Reload installed Kit extension", **_tool_options("kit_extension_reload"))
async def kit_extension_reload(
    extension: Annotated[
        str, Field(min_length=1, max_length=160,
                   pattern=r"^[A-Za-z0-9_.]+(?:-[A-Za-z][A-Za-z0-9_.]*)?$")
    ],
) -> dict[str, Any]:
    """Disable, rediscover and re-enable one safe extension in the same Kit process."""
    return await _shielded_control_post(
        "kit_extension_reload", "/khl/lab/extensions/reload", {"extension": extension}
    )


@mcp.tool(title="Inspect built-in Kit profiler", **_tool_options("kit_profiler_status"))
async def kit_profiler_status() -> dict[str, Any]:
    """Report installed, already-loaded profiler capabilities without activating capture."""
    return await _get("kit_profiler_status", "/khl/lab/profiler/status")


async def _profile_capture_worker(
    capture_id: str,
    duration_seconds: float,
    python_profile: bool,
    experiment_id: str | None,
) -> dict[str, Any]:
    payload = {
        "capture_id": capture_id,
        "duration_seconds": duration_seconds,
        "python_profile": python_profile,
        "experiment_id": experiment_id,
    }

    async def operation() -> dict[str, Any]:
        try:
            with mutation_lock():
                response = await client.post("/khl/lab/profiler/capture", payload)
        except (KitLabClientError, LifecycleError) as exc:
            response = {
                "ok": False,
                "operation": "profiler.capture",
                "error": {"code": getattr(exc, "code", "KIT_LAB_ERROR"), "message": str(exc)},
            }
        try:
            evidence = await asyncio.to_thread(profiles.finish, capture_id, response)
        except ProfileStoreError as exc:
            return {
                "ok": False,
                "operation": "profiler.capture",
                "error": {"code": "PROFILE_EVIDENCE_FAILED", "message": str(exc)},
                "capture": response,
            }
        result = response.setdefault("result", {})
        if isinstance(result, dict):
            result["infrastructure_output"] = evidence
        return response

    return await invoke_and_record(
        experiments,
        experiment_id,
        "kit_profiler_capture",
        {"duration_seconds": duration_seconds, "python_profile": python_profile},
        operation,
    )


@mcp.tool(title="Capture bounded built-in Kit profile", **_tool_options("kit_profiler_capture"))
async def kit_profiler_capture(
    duration_seconds: Annotated[
        float,
        Field(gt=0, le=10, description="Responsive-runtime capture bound in seconds; maximum 10."),
    ] = 1.0,
    python_profile: Annotated[
        bool,
        Field(description="Include Carbonite Python-call instrumentation; not cProfile output."),
    ] = False,
) -> dict[str, Any]:
    """Run one server-owned bounded CPU capture and persist bounded infrastructure evidence."""
    if not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool):
        raise ToolError("INVALID_CAPTURE_BOUND: duration_seconds must be numeric")
    if not math.isfinite(duration_seconds):
        raise ToolError("INVALID_CAPTURE_BOUND: duration_seconds must be finite")
    status = await _lifecycle().status()
    if status.get("state") != "READY":
        raise ToolError(f"KIT_NOT_READY: {status.get('state')}")
    experiment_id = experiments.current_id()
    request_evidence = {
        "duration_seconds": duration_seconds,
        "python_profile": python_profile,
        "experiment_id": experiment_id,
        "kit_id": status.get("kit_id"),
        "process": status.get("process"),
        "bridge_api": status.get("bridge_api"),
    }
    try:
        allocation = await asyncio.to_thread(profiles.create, request_evidence)
    except ProfileStoreError as exc:
        raise ToolError(f"PROFILE_OUTPUT_UNSAFE: {exc}") from exc
    capture_id = allocation["capture_id"]
    task = asyncio.create_task(
        _profile_capture_worker(capture_id, float(duration_seconds), python_profile, experiment_id),
        name=f"kit-profiler-capture-{capture_id}",
    )
    _background_control_tasks.add(task)
    task.add_done_callback(_background_control_tasks.discard)
    return await asyncio.shield(task)


@mcp.tool(title="Read Kit profiler capture status", **_tool_options("kit_profiler_capture_status"))
async def kit_profiler_capture_status(
    capture_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
) -> dict[str, Any]:
    """Read bounded infrastructure evidence or current bridge state for one generated ID."""
    try:
        evidence = await asyncio.to_thread(profiles.get, capture_id)
    except (ProfileStoreError, FileNotFoundError, json.JSONDecodeError) as exc:
        raise ToolError(f"CAPTURE_NOT_FOUND: {exc}") from exc
    if evidence["complete"]:
        return evidence
    try:
        bridge = await client.post(
            "/khl/lab/profiler/capture/status", {"capture_id": capture_id}
        )
    except KitLabClientError as exc:
        bridge = {"ok": False, "error": {"code": "BRIDGE_UNAVAILABLE", "message": str(exc)}}
    return {**evidence, "bridge": bridge}


@mcp.tool(title="Inspect active viewport", **_tool_options("kit_viewport_info"))
async def kit_viewport_info() -> dict[str, Any]:
    """Read the active viewport's camera, resolution and render-product path."""
    return await _get("kit_viewport_info", "/khl/lab/viewport/info")


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
    """Execute development Python inside local Kit.

    Develop and validate reusable installed-version Kit/USD source. The runtime is
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
    experiment_id: Annotated[str | None, Field(description="Expected active experiment ID for identity-safe finish; when supplied, must match the active ID or finish refuses without writing anything", max_length=96)] = None,
) -> dict[str, Any]:
    """Finish the active experiment, write summary.md and clear the current pointer. An optional expected experiment ID is validated atomically; mismatch refuses without any persistence mutation."""
    return _experiment_result(lambda: experiments.finish(summary, outcome, experiment_id))


@mcp.tool(title="Inspect Kit lifecycle configuration", **_tool_options("kit_lifecycle_config"))
async def kit_lifecycle_config(
    kit_id: Literal["nchc-kit-dev-main"] = KIT_ID,
) -> dict[str, Any]:
    """Read operator-configured launch settings and process-inspection diagnostics."""
    return await _lifecycle_operation(lambda lifecycle: lifecycle.configuration(), kit_id)


@mcp.tool(title="Identify managed Kit status", **_tool_options("kit_status"))
async def kit_status(kit_id: Literal["nchc-kit-dev-main"] = KIT_ID) -> dict[str, Any]:
    """Rediscover Kit by persistent OS environment identity, then check bridge readiness."""
    return await _lifecycle_operation(lambda lifecycle: lifecycle.status(), kit_id)


@mcp.tool(title="Start managed Kit", **_tool_options("kit_start"))
async def kit_start(
    ctx: Context,
    kit_id: Literal["nchc-kit-dev-main"] = KIT_ID,
    readiness_timeout: Annotated[float | None, Field(gt=0, le=3600)] = None,
) -> dict[str, Any]:
    """Start the approved launcher if no identified or untagged Kit is already running."""
    return await _lifecycle_operation(
        lambda lifecycle: lifecycle.start(readiness_timeout, ctx.report_progress), kit_id)


@mcp.tool(title="Stop managed Kit", **_tool_options("kit_stop"))
async def kit_stop(
    ctx: Context,
    kit_id: Literal["nchc-kit-dev-main"] = KIT_ID,
    force: Annotated[bool, Field(description="Explicitly allow SIGKILL after the graceful shutdown timeout.")] = False,
) -> dict[str, Any]:
    """Rediscover exactly one identified Kit and validate its pidfd before signalling."""
    return await _lifecycle_operation(lambda lifecycle: lifecycle.stop(force, ctx.report_progress), kit_id)


@mcp.tool(title="Restart managed Kit", **_tool_options("kit_restart"))
async def kit_restart(
    ctx: Context,
    kit_id: Literal["nchc-kit-dev-main"] = KIT_ID,
    force: Annotated[bool, Field(description="Explicitly allow SIGKILL after the graceful shutdown timeout.")] = False,
    readiness_timeout: Annotated[float | None, Field(gt=0, le=3600)] = None,
) -> dict[str, Any]:
    """Stop the currently identified Kit, confirm exit, then launch with the same Kit ID."""
    return await _lifecycle_operation(
        lambda lifecycle: lifecycle.restart(force, readiness_timeout, ctx.report_progress), kit_id)


@mcp.tool(title="Discover native Kit log paths", **_tool_options("kit_log_paths"))
async def kit_log_paths(kit_id: Literal["nchc-kit-dev-main"] = KIT_ID) -> dict[str, Any]:
    """Return process-associated native/output log paths without reading file contents."""
    return await _lifecycle_operation(lambda lifecycle: lifecycle.log_paths(), kit_id)


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
