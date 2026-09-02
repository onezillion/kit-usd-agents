import ast
import asyncio
import contextlib
import inspect
import io
import time
import traceback
from typing import Optional

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


class ExecuteRequest(BaseModel):
    code: str = Field(
        ...,
        min_length=1,
        description="Python code executed inside the active Kit interpreter.",
    )


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
        "busy": _execute_lock.locked(),
        "namespace_keys": sorted(
            key for key in _namespace if not key.startswith("__")
        ),
    }


@router.get("/status", summary="Get Kit Lab status")
async def status():
    return _status_payload("omni.khl.kit_lab")


@legacy_router.get("/status", summary="Get legacy AI console status")
async def legacy_status():
    return _status_payload("omni.khl.ai_console")


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
