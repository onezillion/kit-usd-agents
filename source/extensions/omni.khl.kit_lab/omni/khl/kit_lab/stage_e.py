"""Stage E installed-extension control and bounded Carbonite profiling.

This module deliberately imports Kit APIs only inside operations so its resolution and
validation helpers remain testable outside Kit.  Extension mutations use the installed
manager's immediate API and verify fresh state; they never invoke registry or lifecycle
operations.  Profiler capture uses the public in-memory Carbonite monitor, not private UI
methods or an unverified file-export setting contract.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import sys
import time
import uuid
from collections import deque
from itertools import islice
from typing import Any

from pydantic import BaseModel, Field


API_VERSION = "0.5.0"
CONTROL_TIMEOUT_SECONDS = 15.0
CLEANUP_TIMEOUT_SECONDS = 5.0
MAX_CAPTURE_SECONDS = 10.0
MAX_CAPTURE_EVENTS = 512
MAX_NATIVE_EVENTS_VISITED = 20_000
MAX_PROFILE_THREADS = 128
PROFILE_MASK = 1
CPU_PROFILER_PLUGIN = "carb.profiler-cpu.plugin"
BRIDGE_EXTENSION = "omni.khl.kit_lab"
PROFILER_SETTING_PATHS = (
    "/plugins/carb.profiler-cpu.plugin/saveProfile",
    "/plugins/carb.profiler-cpu.plugin/compressProfile",
    "/plugins/carb.profiler-cpu.plugin/filePath",
)
_TARGET_RE = re.compile(r"^[A-Za-z0-9_.]+(?:-[A-Za-z][A-Za-z0-9_.]*)?$")
_control_lock = asyncio.Lock()
_capture_operations: dict[str, dict[str, Any]] = {}
_bridge_generation: str | None = None


class ExtensionTargetRequest(BaseModel):
    extension: str = Field(..., min_length=1, max_length=160, pattern=_TARGET_RE.pattern)


class ProfilerCaptureRequest(BaseModel):
    capture_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    duration_seconds: float = Field(1.0, gt=0, le=MAX_CAPTURE_SECONDS)
    python_profile: bool = False
    experiment_id: str | None = Field(None, max_length=96)


class ProfilerCaptureStatusRequest(BaseModel):
    capture_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")


def start_bridge_generation() -> str:
    global _bridge_generation
    _bridge_generation = uuid.uuid4().hex
    _capture_operations.clear()
    return _bridge_generation


def bridge_generation() -> str | None:
    return _bridge_generation


def _phase(operation: dict[str, Any], name: str, **details: Any) -> None:
    operation["phase"] = name
    operation.setdefault("phase_history", []).append(
        {"phase": name, "monotonic": time.monotonic(), **details}
    )


def _failure(operation: str, code: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "operation": operation,
        "error": {"code": code, "message": message},
        **details,
    }


def _success(operation: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "operation": operation, "result": result}


def _as_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "get_dict"):
        value = value.get_dict()
    return value if isinstance(value, dict) else None


def _version_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return ".".join(str(part) for part in value[:3])
    return None


def _normalize_target(target: str) -> str:
    target = target.strip()
    if not _TARGET_RE.fullmatch(target):
        raise ValueError("Target must be a canonical extension name with an optional Kit tag")
    import omni.ext

    name, version = omni.ext.get_extension_name_and_version(target)
    if version or name != target:
        raise ValueError("Versioned extension IDs are not accepted; use the canonical unversioned identity")
    return target


def _all_extensions(manager: Any) -> list[dict[str, Any]]:
    raw = manager.get_extensions()
    if isinstance(raw, dict):
        raw = raw.values()
    return [dict(item) for item in raw if isinstance(item, dict)]


def _config(manager: Any, extension_id: str) -> dict[str, Any] | None:
    return _as_dict(manager.get_extension_dict(extension_id))


def _exact_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("package", {}).get("name") or "")


def _exact_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("extension_id") or "")


def _enabled(manager: Any, name: str) -> bool:
    return bool(manager.is_extension_enabled(name))


def _resolved_dependencies(config: dict[str, Any] | None) -> list[str] | None:
    if config is None:
        return None
    state = config.get("state")
    if not isinstance(state, dict) or "dependencies" not in state:
        return None
    dependencies = state.get("dependencies")
    if dependencies is None:
        return []
    # Kit 110.1.3 returns an empty dictionary for enabled leaf extensions and
    # a tuple of exact IDs when resolved dependencies exist.
    if isinstance(dependencies, dict) and not dependencies:
        return []
    if not isinstance(dependencies, (list, tuple)):
        return None
    return [str(item) for item in dependencies]


def _declared_dependencies(config: dict[str, Any] | None) -> dict[str, Any] | None:
    if config is None:
        return None
    value = config.get("dependencies", {})
    return value if isinstance(value, dict) else None


def _catalog(manager: Any) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    entries = _all_extensions(manager)
    configs: dict[str, dict[str, Any]] = {}
    for entry in entries:
        extension_id = _exact_id(entry)
        if extension_id:
            config = _config(manager, extension_id)
            if config is not None:
                configs[extension_id] = config
    return entries, configs


def _solver_plan(manager: Any, target: str) -> dict[str, Any]:
    # add_enabled=True makes the solver prove compatibility with the running app,
    # instead of solving the target in an unrealistically empty environment.
    solved, raw_plan, error = manager.solve_extensions(
        [target], add_enabled=True, return_only_disabled=False
    )
    plan = [dict(item) for item in raw_plan if isinstance(item, dict)]
    ids = [_exact_id(item) for item in plan]
    candidates = [item for item in plan if _exact_name(item) == target]
    local_complete = bool(solved) and bool(plan) and all(
        _exact_id(item) and item.get("path") and manager.get_extension_dict(_exact_id(item)) is not None
        for item in plan
    )
    return {
        "solved": bool(solved),
        "error": str(error or ""),
        "ids": ids,
        "solution": [
            {
                "id": _exact_id(item),
                "name": _exact_name(item),
                "path": item.get("path"),
                "enabled": _enabled(manager, _exact_name(item)) if _exact_name(item) else None,
            }
            for item in plan
        ],
        "candidate_ids": [_exact_id(item) for item in candidates],
        "local_complete": local_complete,
        "registry_calls_performed": False,
    }


def _active_reverse_graph(
    manager: Any, entries: list[dict[str, Any]], configs: dict[str, dict[str, Any]]
) -> tuple[dict[str, set[str]], bool]:
    reverse: dict[str, set[str]] = {}
    catalog_ids = {_exact_id(entry) for entry in entries if _exact_id(entry)}
    complete = True
    for entry in entries:
        name = _exact_name(entry)
        if not name or not _enabled(manager, name):
            continue
        extension_id = str(manager.get_enabled_extension_id(name) or _exact_id(entry))
        dependencies = _resolved_dependencies(configs.get(extension_id))
        if dependencies is None:
            complete = False
            continue
        for dependency_id in dependencies:
            if dependency_id not in catalog_ids or dependency_id not in configs:
                complete = False
                continue
            reverse.setdefault(dependency_id, set()).add(extension_id)
    return reverse, complete


def _transitive_reverse(reverse: dict[str, set[str]], target_id: str) -> list[str]:
    found: set[str] = set()
    queue = deque([target_id])
    while queue:
        current = queue.popleft()
        for dependent in reverse.get(current, set()):
            if dependent not in found:
                found.add(dependent)
                queue.append(dependent)
    return sorted(found)


def _protected_ids(manager: Any, configs: dict[str, dict[str, Any]]) -> tuple[set[str], bool]:
    root_id = manager.get_enabled_extension_id(BRIDGE_EXTENSION)
    if not root_id:
        return set(), False
    protected = {str(root_id)}
    queue = deque([str(root_id)])
    complete = True
    while queue:
        current = queue.popleft()
        dependencies = _resolved_dependencies(configs.get(current))
        if dependencies is None:
            complete = False
            continue
        for dependency_id in dependencies:
            if dependency_id not in protected:
                protected.add(dependency_id)
                queue.append(dependency_id)
    return protected, complete


def inspect_extension_impl(manager: Any, requested: str) -> dict[str, Any]:
    target = _normalize_target(requested)
    entries, configs = _catalog(manager)
    candidates = [entry for entry in entries if _exact_name(entry) == target]
    enabled_id = manager.get_enabled_extension_id(target)
    solver = _solver_plan(manager, target) if candidates else {
        "solved": False, "error": "No local candidates", "ids": [], "candidate_ids": [],
        "solution": [], "local_complete": False, "registry_calls_performed": False,
    }
    selected_id = str(enabled_id) if enabled_id else (
        solver["candidate_ids"][0] if len(solver["candidate_ids"]) == 1 else None
    )
    selected_config = configs.get(selected_id) if selected_id else None
    selected_state = selected_config.get("state", {}) if selected_config else {}
    reverse, reverse_complete = _active_reverse_graph(manager, entries, configs)
    protected, protection_complete = _protected_ids(manager, configs)
    active_dependents = _transitive_reverse(reverse, selected_id) if selected_id else []
    graph_complete = bool(
        selected_id and solver["local_complete"] and reverse_complete and protection_complete
    )
    protection_paths = []
    if selected_id in protected:
        protection_paths.append(f"{BRIDGE_EXTENSION} -> {selected_id}")
    if target == BRIDGE_EXTENSION:
        protection_paths.append("standalone Kit Lab self-disable/reload is forbidden")
    reloadable = selected_state.get("reloadable") if selected_config else None
    toggleable = (selected_config or {}).get("package", {}).get("toggleable", True)
    enabled = bool(enabled_id)
    restrictions = []
    if not candidates:
        restrictions.append("ABSENT")
    if candidates and not selected_id:
        restrictions.append("AMBIGUOUS_OR_UNSOLVED")
    if not graph_complete:
        restrictions.append("INCOMPLETE_SAFETY_GRAPH")
    if protection_paths:
        restrictions.append("PROTECTED_CONTROL_PLANE")
    if enabled and reloadable is False:
        restrictions.append("NON_RELOADABLE")
    if toggleable is False:
        restrictions.append("NON_TOGGLEABLE")
    if active_dependents:
        restrictions.append("ACTIVE_DEPENDENTS")
    candidate_payload = []
    for item in candidates:
        extension_id = _exact_id(item)
        config = configs.get(extension_id, {})
        candidate_payload.append({
            "id": extension_id,
            "package_id": item.get("package_id") or config.get("package", {}).get("packageId"),
            "name": target,
            "version": _version_text(item.get("version") or config.get("package", {}).get("version")),
            "path": item.get("path") or config.get("path"),
            "enabled": extension_id == enabled_id,
            "reloadable": config.get("state", {}).get("reloadable"),
            "toggleable": config.get("package", {}).get("toggleable", True),
            "failed": config.get("state", {}).get("failed"),
        })
    return {
        "requested": requested,
        "canonical_name": target,
        "installed": bool(candidates),
        "discoverable": bool(candidates),
        "enabled": enabled,
        "enabled_state": _enabled_snapshot(manager),
        "enabled_id": enabled_id,
        "selected_id": selected_id,
        "selection_reason": "enabled_exact_id" if enabled_id else (
            "installed_solver" if selected_id else "none"
        ),
        "candidates": candidate_payload,
        "solver": solver,
        "declared_dependencies": _declared_dependencies(selected_config),
        "resolved_active_dependencies": _resolved_dependencies(selected_config),
        "active_reverse_dependents": active_dependents,
        "graph_complete": graph_complete,
        "protection_complete": protection_complete,
        "protection_paths": protection_paths,
        "reloadable": reloadable,
        "toggleable": toggleable,
        "startup_failed": selected_state.get("failed") if selected_config else None,
        "restrictions": sorted(set(restrictions)),
        "self_reload": "RESTRICTED" if target == BRIDGE_EXTENSION else "NOT_APPLICABLE",
    }


async def extension_inspect(requested: str) -> dict[str, Any]:
    try:
        import omni.kit.app
        manager = omni.kit.app.get_app().get_extension_manager()
        return _success("extensions.inspect", inspect_extension_impl(manager, requested))
    except ValueError as exc:
        return _failure("extensions.inspect", "INVALID_EXTENSION_TARGET", str(exc))
    except Exception as exc:
        return _failure("extensions.inspect", "EXTENSION_INSPECTION_FAILED", f"{type(exc).__name__}: {exc}")


def _enabled_snapshot(manager: Any) -> dict[str, str]:
    result = {}
    for item in _all_extensions(manager):
        name = _exact_name(item)
        if name and _enabled(manager, name):
            extension_id = manager.get_enabled_extension_id(name)
            if extension_id:
                result[name] = str(extension_id)
    return result


async def _apply_state(manager: Any, extension_id: str, enabled: bool) -> dict[str, Any]:
    before = _enabled_snapshot(manager)
    changed = bool(manager.set_extension_enabled_immediate(extension_id, enabled))
    immediate = _enabled_snapshot(manager)
    import omni.kit.app
    await omni.kit.app.get_app().next_update_async()
    await omni.kit.app.get_app().next_update_async()
    settled = _enabled_snapshot(manager)
    interference = {
        name: {"immediate": immediate.get(name), "settled": settled.get(name)}
        for name in sorted(set(immediate) | set(settled))
        if immediate.get(name) != settled.get(name)
    }
    return {
        "changed": changed,
        "before": before,
        "immediate": immediate,
        "settled": settled,
        "interference": interference,
    }


async def _rollback_enabled_snapshot(
    manager: Any, baseline: dict[str, str], allowed_names: set[str]
) -> dict[str, Any]:
    current = _enabled_snapshot(manager)
    external = {
        name: {"before": baseline.get(name), "current": current.get(name)}
        for name in sorted(set(baseline) | set(current))
        if baseline.get(name) != current.get(name) and name not in allowed_names
    }
    restored = []
    refused = []
    # Disable newly enabled targets before their newly enabled dependencies.
    pending = {
        name: current[name] for name in allowed_names if name not in baseline and name in current
    }
    while pending:
        progressed = False
        for name in sorted(tuple(pending)):
            inspection = inspect_extension_impl(manager, name)
            active_dependents = set(inspection["active_reverse_dependents"])
            pending_ids = set(pending.values())
            if active_dependents & pending_ids:
                continue
            unsafe_dependents = sorted(active_dependents - pending_ids)
            if (not inspection["graph_complete"] or inspection["protection_paths"]
                    or unsafe_dependents or inspection["reloadable"] is not True):
                refused.append({"name": name, "reason": "no longer safe to disable"})
                pending.pop(name)
                continue
            await _apply_state(manager, pending.pop(name), False)
            restored.append({"name": name, "enabled": False})
            progressed = True
        if not progressed and pending:
            refused.extend(
                {"name": name, "reason": "owned dependency cycle prevented safe rollback"}
                for name in sorted(pending)
            )
            break
    current = _enabled_snapshot(manager)
    # Re-enable exact baseline IDs; Kit's local solver restores required dependencies.
    for name in sorted(allowed_names):
        if baseline.get(name) and current.get(name) != baseline[name]:
            await _apply_state(manager, baseline[name], True)
            restored.append({"name": name, "enabled_id": baseline[name]})
    final = _enabled_snapshot(manager)
    remaining = {
        name: {"before": baseline.get(name), "after": final.get(name)}
        for name in sorted(allowed_names)
        if baseline.get(name) != final.get(name)
    }
    return {
        "complete": not remaining,
        "restored": restored,
        "remaining": remaining,
        "refused": refused,
        "external_changes_preserved": external,
    }


async def _mutate_extension(action: str, requested: str) -> dict[str, Any]:
    operation_id = uuid.uuid4().hex
    operation = {
        "operation_id": operation_id,
        "action": action,
        "deadline_seconds": CONTROL_TIMEOUT_SECONDS,
        "phase_history": [],
        "bridge_generation": _bridge_generation,
        "process": {"pid": os.getpid()},
    }
    manager = None
    enabled_before = None
    allowed_names: set[str] = set()
    _phase(operation, "PREFLIGHT")
    try:
        async with _control_lock:
            import omni.kit.app
            manager = omni.kit.app.get_app().get_extension_manager()
            before = inspect_extension_impl(manager, requested)
            operation["before"] = before
            target = before["canonical_name"]
            if target == BRIDGE_EXTENSION:
                code = "SELF_DISABLE_FORBIDDEN" if action == "disable" else "SELF_RELOAD_UNSUPPORTED"
                return _failure(f"extensions.{action}", code,
                                "Kit Lab cannot disable or reload its own control channel in Stage E",
                                result={**operation, "terminal": "REFUSED"})
            if not before["installed"]:
                return _failure(f"extensions.{action}", "EXTENSION_ABSENT", "No local installed candidate",
                                result={**operation, "terminal": "REFUSED"})
            if not before["selected_id"]:
                return _failure(f"extensions.{action}", "AMBIGUOUS_EXTENSION",
                                "The local solver did not resolve exactly one installed candidate",
                                result={**operation, "terminal": "REFUSED"})
            if not before["graph_complete"]:
                return _failure(f"extensions.{action}", "INCOMPLETE_SAFETY_GRAPH",
                                "Complete local dependency and reverse-dependent evidence is required",
                                result={**operation, "terminal": "REFUSED"})
            if before["protection_paths"]:
                return _failure(f"extensions.{action}", "PROTECTED_CONTROL_PLANE",
                                "Target is required by the active Kit Lab control plane",
                                result={**operation, "terminal": "REFUSED"})
            if action == "enable" and before["enabled"]:
                operation.update(after=before, changed=False, terminal="SUCCEEDED", no_op=True)
                _phase(operation, "VERIFIED")
                return _success("extensions.enable", operation)
            if action == "disable" and not before["enabled"]:
                operation.update(after=before, changed=False, terminal="SUCCEEDED", no_op=True)
                _phase(operation, "VERIFIED")
                return _success("extensions.disable", operation)
            if action == "reload" and not before["enabled"]:
                return _failure("extensions.reload", "RELOAD_REQUIRES_ENABLED",
                                "Enable the extension before reloading it",
                                result={**operation, "terminal": "REFUSED"})
            if action in {"disable", "reload"} and before["active_reverse_dependents"]:
                return _failure(f"extensions.{action}", "ACTIVE_DEPENDENTS",
                                "Active reverse dependents prevent mutation",
                                result={**operation, "terminal": "REFUSED"})
            if action in {"disable", "reload"} and before["reloadable"] is not True:
                return _failure(f"extensions.{action}", "NON_RELOADABLE",
                                "Installed runtime metadata does not permit disable/reload",
                                result={**operation, "terminal": "REFUSED"})
            if before["toggleable"] is not True:
                return _failure(f"extensions.{action}", "NON_TOGGLEABLE",
                                "Extension metadata forbids runtime toggling",
                                result={**operation, "terminal": "REFUSED"})

            enabled_before = _enabled_snapshot(manager)
            planned_names = {
                _exact_name(item) for item in _all_extensions(manager)
                if _exact_id(item) in set(before["solver"]["ids"])
            }
            owned_names: set[str] = set()
            _phase(operation, "MUTATING", selected_id=before["selected_id"])
            expected_enabled_id = None
            if action == "enable":
                expected_enabled_id = before["selected_id"]
                transition = await _apply_state(manager, before["selected_id"], True)
            elif action == "disable":
                transition = await _apply_state(manager, before["enabled_id"], False)
            else:
                transition = await _apply_state(manager, before["enabled_id"], False)
                operation.setdefault("state_transitions", []).append(transition)
                owned_names.update(
                    name for name in set(transition["before"]) | set(transition["immediate"])
                    if transition["before"].get(name) != transition["immediate"].get(name)
                )
                allowed_names = set(owned_names)
                if transition["interference"]:
                    raise RuntimeError("Concurrent extension state change observed during disable")
                disabled = inspect_extension_impl(manager, target)
                operation["disabled"] = disabled
                if disabled["enabled"]:
                    raise RuntimeError("Fresh disabled-state verification failed during reload")
                _phase(operation, "REDISCOVERING")
                rediscovered = inspect_extension_impl(manager, target)
                operation["rediscovered"] = rediscovered
                if not rediscovered["selected_id"] or not rediscovered["graph_complete"]:
                    raise RuntimeError("Candidate could not be safely rediscovered after disable")
                expected_enabled_id = rediscovered["selected_id"]
                _phase(operation, "REENABLING", selected_id=rediscovered["selected_id"])
                transition = await _apply_state(manager, rediscovered["selected_id"], True)

            operation.setdefault("state_transitions", []).append(transition)
            owned_names.update(
                name for name in set(transition["before"]) | set(transition["immediate"])
                if transition["before"].get(name) != transition["immediate"].get(name)
            )
            if not owned_names.issubset(planned_names | {target}):
                raise RuntimeError("Immediate mutation exceeded the compatible local solver plan")
            allowed_names = owned_names
            if transition["interference"]:
                raise RuntimeError("Concurrent extension state change observed during Kit updates")

            _phase(operation, "VERIFYING")
            after = inspect_extension_impl(manager, target)
            expected = action != "disable"
            if after["enabled"] is not expected:
                raise RuntimeError("Fresh enabled-state verification did not match the requested state")
            if expected and after["enabled_id"] != expected_enabled_id:
                raise RuntimeError("Fresh exact-ID verification did not match the resolved local candidate")
            if expected and after["startup_failed"] is True:
                raise RuntimeError("Extension loader reported startup failure")
            enabled_after = _enabled_snapshot(manager)
            deltas = {
                name: {"before": enabled_before.get(name), "after": enabled_after.get(name)}
                for name in sorted(set(enabled_before) | set(enabled_after))
                if enabled_before.get(name) != enabled_after.get(name)
            }
            unexpected_deltas = sorted(set(deltas) - allowed_names)
            if unexpected_deltas:
                raise RuntimeError(
                    "Extension operation changed identities outside its local solver plan: "
                    + ", ".join(unexpected_deltas)
                )
            operation.update(
                after=after,
                changed=bool(transition["changed"] or deltas),
                actual_enabled_deltas=deltas,
                terminal="SUCCEEDED",
                same_process=True,
                process={"pid": os.getpid()},
            )
            _phase(operation, "SUCCEEDED")
            return _success(f"extensions.{action}", operation)
    except ValueError as exc:
        return _failure(f"extensions.{action}", "INVALID_EXTENSION_TARGET", str(exc),
                        result={**operation, "terminal": "REFUSED"})
    except asyncio.CancelledError:
        if manager is not None and enabled_before is not None:
            try:
                async def rollback_after_cancel() -> dict[str, Any]:
                    async with _control_lock:
                        return await _rollback_enabled_snapshot(
                            manager, enabled_before, allowed_names)

                operation["rollback"] = await asyncio.wait_for(
                    rollback_after_cancel(), CLEANUP_TIMEOUT_SECONDS
                )
            except Exception as rollback_error:
                operation["rollback"] = {
                    "complete": False,
                    "error": f"{type(rollback_error).__name__}: {rollback_error}",
                }
        rollback_complete = operation.get("rollback", {}).get("complete", False)
        operation["terminal"] = "CANCELLED_ROLLED_BACK" if rollback_complete else "CANCELLED_OUTCOME_UNKNOWN"
        _phase(operation, operation["terminal"])
        return _failure(
            f"extensions.{action}", "REQUEST_CANCELLED",
            "The bounded operation was cancelled; inspect rollback and terminal evidence",
            result=operation,
        )
    except Exception as exc:
        if manager is not None and enabled_before is not None:
            try:
                async def rollback_after_failure() -> dict[str, Any]:
                    async with _control_lock:
                        return await _rollback_enabled_snapshot(
                            manager, enabled_before, allowed_names)

                operation["rollback"] = await asyncio.wait_for(
                    rollback_after_failure(), CLEANUP_TIMEOUT_SECONDS
                )
            except Exception as rollback_error:
                operation["rollback"] = {
                    "complete": False,
                    "error": f"{type(rollback_error).__name__}: {rollback_error}",
                }
        operation["terminal"] = "FAILED"
        _phase(operation, "FAILED", error=f"{type(exc).__name__}: {exc}")
        return _failure(f"extensions.{action}", "EXTENSION_MUTATION_FAILED", f"{type(exc).__name__}: {exc}",
                        result=operation)


async def extension_mutation(action: str, requested: str) -> dict[str, Any]:
    async def bounded_owner() -> dict[str, Any]:
        task = asyncio.create_task(
            _mutate_extension(action, requested), name=f"kit-lab-extension-{action}"
        )
        done, _ = await asyncio.wait({task}, timeout=CONTROL_TIMEOUT_SECONDS)
        if done:
            return task.result()
        task.cancel()
        result = await task
        result["error"] = {
            "code": "OPERATION_TIMED_OUT",
            "message": "The runtime operation exceeded its monotonic deadline",
        }
        return result

    # Request cancellation cannot abandon the runtime owner or its bounded rollback.
    return await asyncio.shield(asyncio.create_task(bounded_owner()))


def _setting_snapshot(settings: Any, path: str) -> dict[str, Any]:
    present = settings.get_settings_dictionary(path) is not None
    return {"path": path, "present": present, "value": settings.get(path) if present else None}


def _restore_setting(settings: Any, snapshot: dict[str, Any]) -> None:
    if snapshot["present"]:
        settings.set(snapshot["path"], snapshot["value"])
    else:
        settings.destroy_item(snapshot["path"])


def profiler_status_impl() -> dict[str, Any]:
    import carb
    import omni.kit.app

    manager = omni.kit.app.get_app().get_extension_manager()
    names = ("omni.activity.profiler", "omni.kit.profiler.window", "omni.kit.profiler.tracy")
    extensions = {}
    for name in names:
        enabled_id = manager.get_enabled_extension_id(name)
        candidates = [item for item in _all_extensions(manager) if _exact_name(item) == name]
        extensions[name] = {
            "installed": bool(candidates),
            "enabled": bool(enabled_id),
            "enabled_id": enabled_id,
            "candidate_ids": [_exact_id(item) for item in candidates],
        }
    settings = carb.settings.get_settings()
    profiler = sys.modules.get("carb.profiler")
    save_profile = settings.get("/plugins/carb.profiler-cpu.plugin/saveProfile")
    capture_mask = None
    python_enabled = None
    if profiler:
        # Kit may select the mux profiler when no provider is named, while the
        # profile monitor is implemented by the CPU plugin. Bind both sides to
        # the same installed backend so emitted zones and completed snapshots
        # cannot silently cross providers.
        interface = profiler.acquire_profiler_interface(plugin_name=CPU_PROFILER_PLUGIN)
        capture_mask = int(interface.get_capture_mask())
        python_enabled = bool(interface.is_python_profiling_enabled())
    capture_active = bool(save_profile or capture_mask or python_enabled)
    installed_state = "available_requires_capture_association" if profiler else "unknown"
    return {
        "backend": "carb.profiler.cpu.in_memory" if profiler else None,
        "module_loaded": profiler is not None,
        "interface_active": bool(profiler.is_profiler_active()) if profiler else None,
        "capture_active": capture_active,
        "capture_mask": capture_mask,
        "python_profiling": python_enabled,
        "capture_detection": "saveProfile setting plus public IProfiler mask/Python state",
        "capture_owner": "external_or_profiler_window" if capture_active else None,
        "bridge_capture_active": any(
            value.get("phase") not in {"COMPLETE", "FAILED"} for value in _capture_operations.values()
        ),
        "extensions": extensions,
        "capabilities": {
            "cpu_events": installed_state,
            "bounded_capture": installed_state,
            "python_instrumentation": "available_requires_capture_association" if profiler and hasattr(
                getattr(profiler, "IProfiler", None), "set_python_profiling_enabled"
            ) else "unknown",
            "native_file_export": "unsupported",
            "headless": installed_state,
            "tracy": "unsupported",
            "gpu": "unsupported",
        },
        "limits": {"max_duration_seconds": MAX_CAPTURE_SECONDS, "max_events": MAX_CAPTURE_EVENTS},
        "bridge_generation": _bridge_generation,
        "mutated": False,
    }


async def profiler_status() -> dict[str, Any]:
    try:
        return _success("profiler.status", profiler_status_impl())
    except Exception as exc:
        return _failure("profiler.status", "PROFILER_STATUS_FAILED", f"{type(exc).__name__}: {exc}")


def _flatten_events(
    raw_events: Any,
    marker: str,
    *,
    visit_limit: int = MAX_NATIVE_EVENTS_VISITED,
    output_limit: int = MAX_CAPTURE_EVENTS,
) -> tuple[list[dict[str, Any]], bool, int]:
    bounded: list[dict[str, Any]] = []
    marker_found = False
    visited = 0
    initial = raw_events if isinstance(raw_events, (list, tuple)) else []
    # The association marker is emitted at the stop boundary. If a Python
    # profile generated more roots than the parse budget, sample both ends so
    # the recent boundary is not discarded merely because output is bounded.
    if len(initial) > visit_limit and visit_limit > 1:
        first_count = visit_limit // 2
        queue = deque(islice(initial, first_count))
        queue.extend(reversed(tuple(islice(reversed(initial), visit_limit - first_count))))
    else:
        queue = deque(islice(initial, visit_limit))
    while queue and visited < visit_limit:
        value = queue.popleft()
        visited += 1
        if not isinstance(value, dict):
            continue
        name = str(value.get("name", ""))
        is_marker = marker in name
        marker_found = marker_found or is_marker
        event = {
            key: value.get(key) for key in ("name", "duration", "indent", "threadId", "startTime")
            if key in value
        }
        if len(bounded) < output_limit:
            bounded.append(event)
        elif is_marker and bounded:
            bounded[-1] = event
        children = value.get("children")
        if isinstance(children, (list, tuple)):
            remaining = visit_limit - visited - len(queue)
            if remaining > 0:
                queue.extend(islice(children, remaining))
    return bounded, marker_found, visited


def _collect_events(snapshot: Any, marker: str) -> dict[str, Any]:
    advertised_thread_ids = tuple(snapshot.get_profile_thread_ids())
    thread_ids = [int(value) for value in islice(advertised_thread_ids, MAX_PROFILE_THREADS)]
    main_thread_id = int(snapshot.get_main_thread_id())
    ordered_thread_ids = []
    for thread_id in (main_thread_id, *thread_ids):
        if thread_id not in ordered_thread_ids:
            ordered_thread_ids.append(thread_id)

    bounded: list[dict[str, Any]] = []
    scanned: list[dict[str, int]] = []
    visited = 0
    marker_thread_id = None
    for index, thread_id in enumerate(ordered_thread_ids):
        remaining_visits = MAX_NATIVE_EVENTS_VISITED - visited
        if remaining_visits <= 0:
            break
        remaining_threads = len(ordered_thread_ids) - index
        thread_visit_limit = max(1, remaining_visits // remaining_threads)
        available_output = MAX_CAPTURE_EVENTS - len(bounded)
        raw = snapshot.get_profile_events(thread_id)
        events, found, count = _flatten_events(
            raw,
            marker,
            visit_limit=thread_visit_limit,
            output_limit=max(1, available_output),
        )
        for event in events:
            event.setdefault("profile_thread_id", thread_id)
        if available_output > 0:
            bounded.extend(events[:available_output])
        elif found and bounded:
            marker_event = next(
                (event for event in reversed(events) if marker in str(event.get("name", ""))),
                None,
            )
            if marker_event is not None:
                bounded[-1] = marker_event
        visited += count
        scanned.append({"thread_id": thread_id, "event_count": len(raw), "events_visited": count})
        if found:
            marker_thread_id = thread_id
            break

    return {
        "events": bounded,
        "marker_found": marker_thread_id is not None,
        "marker_thread_id": marker_thread_id,
        "events_visited": visited,
        "threads_scanned": scanned,
        "thread_ids": thread_ids,
        "main_thread_id": main_thread_id,
        "threads_truncated": len(advertised_thread_ids) > len(thread_ids),
    }


async def _capture(request: ProfilerCaptureRequest) -> dict[str, Any]:
    operation = {
        "capture_id": request.capture_id,
        "experiment_id": request.experiment_id,
        "phase": "BASELINE",
        "phase_history": [],
        "requested_duration_seconds": request.duration_seconds,
        "python_profile_requested": request.python_profile,
        "bridge_generation": _bridge_generation,
        "process": {"pid": os.getpid()},
        "started_monotonic": time.monotonic(),
    }
    _capture_operations[request.capture_id] = operation
    marker = f"KHL_STAGE_E_{request.capture_id}"
    profiler = None
    baseline_mask = None
    expected_mask = None
    baseline_python = None
    expected_python = None
    settings = None
    setting_snapshots: dict[str, dict[str, Any]] = {}
    restoration = {"complete": False, "conflicts": []}
    try:
        async with _control_lock:
            import carb
            import carb.profiler
            import omni.kit.app

            _phase(operation, "PREFLIGHT")
            if not math.isfinite(request.duration_seconds) or not 0 < request.duration_seconds <= MAX_CAPTURE_SECONDS:
                raise ValueError("duration_seconds must be finite and within the fixed bound")
            settings = carb.settings.get_settings()
            setting_snapshots = {
                path: _setting_snapshot(settings, path) for path in PROFILER_SETTING_PATHS
            }
            save_setting = setting_snapshots[PROFILER_SETTING_PATHS[0]]
            if bool(save_setting["value"]):
                raise RuntimeError("PREEXISTING_CAPTURE: native profiler saveProfile is already active")
            if not carb.profiler.is_profiler_active():
                raise RuntimeError("PROFILER_UNAVAILABLE: carb.profiler is not active")
            profiler = carb.profiler.acquire_profiler_interface(plugin_name=CPU_PROFILER_PLUGIN)
            monitor = carb.profiler.acquire_profile_monitor_interface(plugin_name=CPU_PROFILER_PLUGIN)
            baseline_mask = int(profiler.get_capture_mask())
            baseline_python = bool(profiler.is_python_profiling_enabled())
            if baseline_mask != 0 or baseline_python:
                raise RuntimeError(
                    "PREEXISTING_CAPTURE: profiler mask or Python instrumentation is already active"
                )
            expected_mask = baseline_mask | PROFILE_MASK
            expected_python = baseline_python or request.python_profile
            operation["baseline"] = {
                "capture_mask": baseline_mask,
                "python_profiling": baseline_python,
                "settings": setting_snapshots,
            }
            _phase(operation, "STARTING")
            profiler.set_capture_mask(expected_mask)
            if expected_python != baseline_python:
                profiler.set_python_profiling_enabled(expected_python)
            profiler.ensure_thread()
            _phase(operation, "CAPTURING")
            started = time.monotonic()
            deadline = started + request.duration_seconds
            frames = 0
            checksum = 0
            while time.monotonic() < deadline:
                profiler.begin(PROFILE_MASK, marker)
                try:
                    checksum = sum((value * 17) % 97 for value in range(256))
                finally:
                    profiler.end(PROFILE_MASK)
                frames += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.wait_for(omni.kit.app.get_app().next_update_async(), min(1.0, remaining + 0.1))
            _phase(operation, "STOPPING")
            # IProfileMonitor defines get_last_profile_events() relative to the
            # previous explicit mark_frame_end(). Emit the association marker
            # immediately before that native completion boundary.
            profiler.begin(PROFILE_MASK, marker)
            try:
                checksum = sum((value * 19) % 101 for value in range(256))
            finally:
                profiler.end(PROFILE_MASK)
            frames += 1
            monitor.mark_frame_end()
            events = monitor.get_last_profile_events()
            collected = _collect_events(events, marker)
            _phase(operation, "FINALIZING")
            operation["association_scan"] = {
                key: collected[key]
                for key in ("marker_thread_id", "threads_scanned", "events_visited", "threads_truncated")
            }
            if not collected["marker_found"]:
                raise RuntimeError("CAPTURE_ASSOCIATION_FAILED: unique marker was absent from native events")
            operation.update({
                "backend": "carb.profiler.cpu.in_memory",
                "mode": "cpu_python" if request.python_profile else "cpu",
                "marker": marker,
                "marker_found": collected["marker_found"],
                "marker_thread_id": collected["marker_thread_id"],
                "frames_observed": frames,
                "workload_checksum": checksum,
                "thread_ids": collected["thread_ids"],
                "main_thread_id": collected["main_thread_id"],
                "threads_scanned": collected["threads_scanned"],
                "threads_truncated": collected["threads_truncated"],
                "events": collected["events"],
                "events_returned": len(collected["events"]),
                "events_visited": collected["events_visited"],
                "events_truncated": collected["events_visited"] > len(collected["events"]),
                "native_output": None,
                "native_file_export": "unsupported",
                "observed_duration_seconds": time.monotonic() - started,
            })
    except asyncio.CancelledError:
        operation["error"] = {"code": "REQUEST_CANCELLED", "message": "Cleanup continues under bridge ownership"}
        raise
    except Exception as exc:
        message = str(exc)
        code = message.split(":", 1)[0] if ":" in message else "PROFILER_CAPTURE_FAILED"
        operation["error"] = {"code": code, "message": message}
    finally:
        _phase(operation, "RESTORING")
        try:
            if profiler is not None:
                current_mask = int(profiler.get_capture_mask())
                current_python = bool(profiler.is_python_profiling_enabled())
                if current_mask == expected_mask:
                    profiler.set_capture_mask(baseline_mask)
                elif current_mask != baseline_mask:
                    restoration["conflicts"].append("capture_mask_changed_concurrently")
                if current_python == expected_python:
                    profiler.set_python_profiling_enabled(baseline_python)
                elif current_python != baseline_python:
                    restoration["conflicts"].append("python_flag_changed_concurrently")
                restoration["final_capture_mask"] = int(profiler.get_capture_mask())
                restoration["final_python_profiling"] = bool(profiler.is_python_profiling_enabled())
                restoration["complete"] = not restoration["conflicts"] and (
                    restoration["final_capture_mask"] == baseline_mask
                    and restoration["final_python_profiling"] == baseline_python
                )
            else:
                restoration["complete"] = True
            if settings is not None:
                for path, snapshot in setting_snapshots.items():
                    current = _setting_snapshot(settings, path)
                    if current != snapshot:
                        restoration["conflicts"].append(
                            f"setting_changed_concurrently:{path}"
                        )
                if restoration["conflicts"]:
                    restoration["complete"] = False
        except Exception as exc:
            restoration["error"] = f"{type(exc).__name__}: {exc}"
        operation["restoration"] = restoration
        operation["finished_monotonic"] = time.monotonic()
        if operation.get("error") or not restoration["complete"]:
            operation["phase"] = "FAILED"
            _phase(operation, "FAILED")
        else:
            operation["phase"] = "COMPLETE"
            _phase(operation, "COMPLETE")
    if operation["phase"] != "COMPLETE":
        error = operation.get("error", {"code": "RESTORATION_FAILED", "message": "Profiler restoration failed"})
        return _failure("profiler.capture", error["code"], error["message"], result=operation)
    return _success("profiler.capture", operation)


async def profiler_capture(request: ProfilerCaptureRequest) -> dict[str, Any]:
    if request.capture_id in _capture_operations:
        return _failure("profiler.capture", "DUPLICATE_CAPTURE_ID", "Capture ID already exists")
    task = asyncio.create_task(_capture(request), name=f"kit-lab-profile-{request.capture_id}")
    if len(_capture_operations) > 128:
        completed = [
            key for key, value in _capture_operations.items()
            if key != request.capture_id and value.get("phase") in {"COMPLETE", "FAILED"}
        ]
        for key in completed[:len(_capture_operations) - 128]:
            _capture_operations.pop(key, None)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # The server-owned task keeps the monotonic bound and restoration ownership.
        raise


async def profiler_capture_status(capture_id: str) -> dict[str, Any]:
    operation = _capture_operations.get(capture_id)
    if operation is None:
        return _failure("profiler.capture_status", "CAPTURE_NOT_FOUND", "Unknown capture ID")
    return _success("profiler.capture_status", dict(operation))
