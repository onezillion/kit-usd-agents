import ast
import asyncio
import contextlib
import inspect
import io
import os
import time
import traceback
from itertools import islice
from typing import Any, Optional

from omni.services.core.routers import ServiceAPIRouter
from pydantic import BaseModel, Field


router = ServiceAPIRouter(
    prefix="/khl/lab",
    tags=["KHL Kit Lab"],
)

# Compatibility router for clients using omni.khl.ai_console endpoints.
# It can be removed after every client has moved to /khl/lab/*.
legacy_router = ServiceAPIRouter(
    prefix="/khl/ai",
    tags=["KHL AI Console (Legacy)"],
)

_namespace = {}
_execute_lock = asyncio.Lock()

API_VERSION = "0.4.0"
MAX_COLLECTION_ITEMS = 256
MAX_STRING_LENGTH = 20_000


class ExecuteRequest(BaseModel):
    code: str = Field(
        ...,
        min_length=1,
        description="Python code executed inside the active Kit interpreter.",
    )


class StageSummaryRequest(BaseModel):
    include_statistics: bool = Field(
        False, description="Compute full stage.Traverse() statistics; may be expensive."
    )


class ExtensionsListRequest(BaseModel):
    enabled_only: bool = False
    search: Optional[str] = None
    limit: int = Field(500, ge=1, le=5000)


def reset_namespace():
    _namespace.clear()
    _namespace.update(
        {
            "__name__": "__khl_kit_lab__",
            "__builtins__": __builtins__,
        }
    )


def _safe_repr(value, limit=100_000):
    try:
        text = repr(value)
    except BaseException as exc:
        text = f"<repr failed: {type(exc).__name__}: {exc}>"

    if len(text) > limit:
        return text[:limit] + "\n... result truncated ..."

    return text


def _trim_output(text, limit=200_000):
    if len(text) > limit:
        return text[:limit] + "\n... output truncated ..."
    return text


def _json_safe(value: Any, depth=0):
    """Convert common Kit/USD values to bounded JSON-safe data."""
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            return value[:MAX_STRING_LENGTH] + "... value truncated ..."
        return value

    if depth >= 6:
        return _safe_repr(value, MAX_STRING_LENGTH)

    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_COLLECTION_ITEMS:
                result["...truncated..."] = True
                break
            result[str(key)] = _json_safe(item, depth + 1)
        return result

    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [
            _json_safe(item, depth + 1)
            for item in items[:MAX_COLLECTION_ITEMS]
        ]
        if len(items) > MAX_COLLECTION_ITEMS:
            result.append("... collection truncated ...")
        return result

    # Vt arrays and several Gf/USD value types are iterable but are not native
    # JSON values. Avoid treating strings and mappings as generic iterables.
    try:
        items = list(value)
    except (TypeError, ValueError):
        return _safe_repr(value, MAX_STRING_LENGTH)

    result = [
        _json_safe(item, depth + 1)
        for item in items[:MAX_COLLECTION_ITEMS]
    ]
    if len(items) > MAX_COLLECTION_ITEMS:
        result.append("... collection truncated ...")
    return result


def _read_member(obj, name, default=None):
    if obj is None:
        return default

    try:
        value = getattr(obj, name)
        return value() if callable(value) else value
    except Exception:
        return default


def _first_member(obj, names, default=None):
    for name in names:
        value = _read_member(obj, name, None)
        if value is not None:
            return value
    return default


def _failure(operation, code, message):
    return {
        "ok": False,
        "operation": operation,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _success(operation, result):
    return {
        "ok": True,
        "operation": operation,
        "result": result,
    }


async def _run_read_operation(operation, function):
    async with _execute_lock:
        try:
            return _success(operation, function())
        except Exception as exc:
            return _failure(
                operation,
                "KIT_OPERATION_FAILED",
                f"{type(exc).__name__}: {exc}",
            )


async def _evaluate(source):
    tree = ast.parse(source, filename="<khl-kit-lab>", mode="exec")

    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expression = tree.body.pop()

        if tree.body:
            ast.fix_missing_locations(tree)
            prefix_code = compile(
                tree,
                "<khl-kit-lab>",
                "exec",
                flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
            )
            prefix_result = eval(prefix_code, _namespace, _namespace)

            if inspect.isawaitable(prefix_result):
                await prefix_result

        expression = ast.Expression(last_expression.value)
        ast.fix_missing_locations(expression)

        expression_code = compile(
            expression,
            "<khl-kit-lab>",
            "eval",
            flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        )
        result = eval(expression_code, _namespace, _namespace)

        if inspect.isawaitable(result):
            result = await result

        return result

    ast.fix_missing_locations(tree)
    code = compile(
        tree,
        "<khl-kit-lab>",
        "exec",
        flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
    )
    result = eval(code, _namespace, _namespace)

    if inspect.isawaitable(result):
        result = await result

    return result


def _status_payload(service_name):
    return {
        "ok": True,
        "service": service_name,
        "api_version": API_VERSION,
        "kit_id": os.environ.get("KHL_KIT_ID"),
        "pid": os.getpid(),
        "busy": _execute_lock.locked(),
        "namespace_keys": sorted(
            key for key in _namespace if not key.startswith("__")
        ),
        "capabilities": {
            "read": [
                "runtime.info",
                "runtime.identity",
                "stage.summary",
                "extensions.list",
                "viewport.info",
            ],
            "development": [
                "python.execute",
                "session.reset",
            ],
        },
    }


@router.get("/status", summary="Get Kit Lab status")
async def status():
    return _status_payload("omni.khl.kit_lab")


@legacy_router.get("/status", summary="Get legacy AI console status")
async def legacy_status():
    return _status_payload("omni.khl.ai_console")


def _runtime_info_impl():
    import carb
    import omni.kit.app

    app = omni.kit.app.get_app()
    settings = carb.settings.get_settings()

    return {
        "app_name": _json_safe(
            _first_member(app, ("get_app_name", "get_name"))
            or settings.get("/app/name")
        ),
        "app_version": _json_safe(
            _first_member(app, ("get_app_version", "get_version"))
            or settings.get("/app/version")
        ),
        "kit_version": _json_safe(
            _first_member(app, ("get_kit_version", "get_build_version"))
        ),
        "renderer": {
            "render_mode": _json_safe(settings.get("/rtx/rendermode")),
            "multi_gpu_enabled": _json_safe(
                settings.get("/renderer/multiGpu/enable")
            ),
            "active_gpus": _json_safe(
                settings.get("/renderer/multiGpu/activeGpus")
            ),
        },
    }


@router.get("/runtime/info", summary="Inspect the active Kit runtime")
async def runtime_info():
    return await _run_read_operation("runtime.info", _runtime_info_impl)


def _runtime_identity_impl():
    import carb
    import omni.kit.app

    # Read only this process's identity and native log setting, never enumerate its environment.
    with open("/proc/self/stat", encoding="utf-8") as stream:
        start_ticks = int(stream.read().rsplit(")", 1)[1].split()[19])
    return {
        "kit_id": os.environ.get("KHL_KIT_ID"),
        "pid": os.getpid(),
        "start_ticks": start_ticks,
        "api_version": API_VERSION,
        "ready": bool(omni.kit.app.get_app().is_app_ready()),
        "native_log_path": _json_safe(carb.settings.get_settings().get("/log/file")),
    }


@router.get("/runtime/identity", summary="Read Kit identity, readiness and native log path")
async def runtime_identity():
    # Independent of the execution lock: a cooperative Python task need not block health checks.
    try:
        return _success("runtime.identity", _runtime_identity_impl())
    except Exception as exc:
        return _failure("runtime.identity", "IDENTITY_UNAVAILABLE", type(exc).__name__)


def _stage_summary_impl(include_statistics=False):
    import omni.usd
    from pxr import UsdGeom

    context = omni.usd.get_context()
    stage = context.get_stage()
    if stage is None:
        return {
            "available": False,
            "statistics_computed": False,
            "prim_count": None,
            "type_counts": None,
            "context_stage_url": _json_safe(
                _first_member(context, ("get_stage_url",), "")
            ),
        }

    prim_count = None
    type_counts = None
    if include_statistics:
        prim_count = 0
        type_counts = {}
        # Traverse() uses USD's default predicate; this is not a count of TraverseAll().
        for prim in stage.Traverse():
            prim_count += 1
            type_name = prim.GetTypeName() or "<untyped>"
            type_counts[type_name] = type_counts.get(type_name, 0) + 1

    default_prim = stage.GetDefaultPrim()
    edit_target = stage.GetEditTarget()
    edit_layer = edit_target.GetLayer() if edit_target else None

    try:
        meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage)
    except Exception:
        meters_per_unit = None

    try:
        layer_stack = [layer.identifier for layer in stage.GetLayerStack()]
    except Exception:
        layer_stack = []

    root_children = list(islice(stage.GetPseudoRoot().GetChildren(), MAX_COLLECTION_ITEMS + 1))
    return {
        "available": True,
        "root_layer": stage.GetRootLayer().identifier,
        "session_layer": stage.GetSessionLayer().identifier,
        "edit_target_layer": edit_layer.identifier if edit_layer else None,
        "layer_stack": _json_safe(layer_stack),
        "default_prim": str(default_prim.GetPath()) if default_prim else None,
        "up_axis": str(UsdGeom.GetStageUpAxis(stage)),
        "meters_per_unit": meters_per_unit,
        "start_time_code": stage.GetStartTimeCode(),
        "end_time_code": stage.GetEndTimeCode(),
        "time_codes_per_second": stage.GetTimeCodesPerSecond(),
        "frames_per_second": stage.GetFramesPerSecond(),
        "statistics_computed": bool(include_statistics),
        "statistics_scope": "Usd.Stage.Traverse default predicate" if include_statistics else None,
        "prim_count": prim_count,
        "root_children": [
            str(prim.GetPath()) for prim in root_children[:MAX_COLLECTION_ITEMS]
        ],
        "root_children_truncated": len(root_children) > MAX_COLLECTION_ITEMS,
        "type_counts": dict(sorted(type_counts.items())) if type_counts is not None else None,
    }


@router.get("/stage/summary", summary="Summarize the active USD stage")
async def stage_summary(include_statistics: bool = False):
    return await _run_read_operation(
        "stage.summary", lambda: _stage_summary_impl(include_statistics)
    )


@router.post("/stage/summary", summary="Summarize USD stage with optional full statistics")
async def stage_summary_request(request: StageSummaryRequest):
    return await stage_summary(request.include_statistics)


def _extensions_list_impl(request: ExtensionsListRequest):
    import omni.kit.app

    manager = omni.kit.app.get_app().get_extension_manager()
    raw_extensions = manager.get_extensions()
    if isinstance(raw_extensions, dict):
        raw_extensions = list(raw_extensions.values())

    query = request.search.casefold() if request.search else None
    extensions = []
    for raw in raw_extensions:
        if not isinstance(raw, dict):
            continue

        package = raw.get("package") or {}
        extension_id = raw.get("id") or raw.get("extension_id") or ""
        name = raw.get("name") or package.get("name") or extension_id
        version = raw.get("version") or package.get("version")
        title = raw.get("title") or package.get("title")
        path = raw.get("path")

        try:
            enabled = bool(manager.is_extension_enabled(name))
        except Exception:
            try:
                enabled = bool(manager.is_extension_enabled(extension_id))
            except Exception:
                enabled = bool(raw.get("enabled", False))

        searchable = " ".join(
            str(value) for value in (extension_id, name, version, title, path)
            if value is not None
        ).casefold()

        if request.enabled_only and not enabled:
            continue
        if query and query not in searchable:
            continue

        extensions.append(
            {
                "id": extension_id,
                "name": name,
                "version": version,
                "title": title,
                "enabled": enabled,
                "path": path,
            }
        )

    extensions.sort(key=lambda item: (str(item["name"]), str(item["id"])))
    total = len(extensions)
    return {
        "total": total,
        "returned": min(total, request.limit),
        "truncated": total > request.limit,
        "extensions": _json_safe(extensions[:request.limit]),
    }


@router.post("/extensions/list", summary="List Kit extensions")
async def extensions_list(request: ExtensionsListRequest):
    return await _run_read_operation(
        "extensions.list",
        lambda: _extensions_list_impl(request),
    )


def _viewport_info_impl():
    try:
        import omni.kit.viewport.utility as viewport_utility
    except ImportError:
        return {
            "available": False,
            "reason": "omni.kit.viewport.utility is not loaded",
        }

    viewport = None
    get_active_viewport = getattr(
        viewport_utility,
        "get_active_viewport",
        None,
    )
    if callable(get_active_viewport):
        viewport = get_active_viewport()

    if viewport is None:
        get_active_window = getattr(
            viewport_utility,
            "get_active_viewport_window",
            None,
        )
        if callable(get_active_window):
            window = get_active_window()
            viewport = _read_member(window, "viewport_api", None)

    if viewport is None:
        return {"available": False}

    return {
        "available": True,
        "camera_path": _json_safe(
            _first_member(viewport, ("camera_path", "get_active_camera"))
        ),
        "resolution": _json_safe(
            _first_member(
                viewport,
                ("resolution", "texture_resolution", "get_texture_resolution"),
            )
        ),
        "render_product_path": _json_safe(
            _first_member(
                viewport,
                ("render_product_path", "get_render_product_path"),
            )
        ),
        "viewport_id": _json_safe(
            _first_member(viewport, ("id", "viewport_id", "name"))
        ),
    }


@router.get("/viewport/info", summary="Inspect the active viewport")
async def viewport_info():
    return await _run_read_operation("viewport.info", _viewport_info_impl)


async def _reset_impl():
    async with _execute_lock:
        reset_namespace()

    return {
        "ok": True,
        "message": "Python namespace reset.",
    }


@router.post("/session/reset", summary="Reset persistent Python namespace")
async def reset():
    return await _reset_impl()


@legacy_router.post("/reset", summary="Reset persistent Python namespace")
async def legacy_reset():
    return await _reset_impl()


async def _execute_impl(request: ExecuteRequest):
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    result_repr: Optional[str] = None
    exception_type: Optional[str] = None
    traceback_text: Optional[str] = None
    started = time.perf_counter()

    async with _execute_lock:
        try:
            with contextlib.redirect_stdout(stdout_buffer):
                with contextlib.redirect_stderr(stderr_buffer):
                    result = await _evaluate(request.code)

            if result is not None:
                result_repr = _safe_repr(result)

            ok = True

        except BaseException as exc:
            ok = False
            exception_type = type(exc).__name__
            traceback_text = traceback.format_exc()

    elapsed_ms = (time.perf_counter() - started) * 1000.0

    return {
        "ok": ok,
        "stdout": _trim_output(stdout_buffer.getvalue()),
        "stderr": _trim_output(stderr_buffer.getvalue()),
        "result": result_repr,
        "exception_type": exception_type,
        "traceback": traceback_text,
        "elapsed_ms": round(elapsed_ms, 3),
    }


@router.post("/python/execute", summary="Execute Python inside Kit")
async def execute(request: ExecuteRequest):
    return await _execute_impl(request)


@legacy_router.post("/execute", summary="Execute Python inside Kit")
async def legacy_execute(request: ExecuteRequest):
    return await _execute_impl(request)


reset_namespace()
