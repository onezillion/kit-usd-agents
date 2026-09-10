"""Authoritative model-visible policy metadata for KHL Kit Lab MCP tools."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


POLICY_VERSION = "2.2"
PLAYGROUND_ROOT = "/World/AgentSceneLab"
PYTHON_PERMISSION_CHOICES = [
    "Allow once",
    "Allow for this session",
    "Deny once",
    "Deny for this session",
    "Continue without Kit Python / use a safer alternative",
]

SERVER_INSTRUCTIONS = f"""KHL Kit Lab develops, executes, observes, and debugs real Python
inside one local development Kit runtime. Write installed-version Kit/USD source that can
be reused as a script, extension, or scripting component. Use Python for scene, attribute,
and settings investigation when needed; MCP shortcut success does not validate a source
deliverable. Read-only inspection tools may be used without asking. General Python remains
ELEVATED_EXECUTION even when its intended use is inspection.
Before the first kit_execute_python call, ask unless the user already authorized general
Kit Python for this task/session. Offer allow once, allow for session, deny once, deny for
session, or continue with a safer alternative. Honor existing session authorization without
repeated prompts and keep calls within its scope. Session authorization does not permit
file, Nucleus, Git, external-package, long-blocking-loop, or policy-bypass behavior.
Live mutation never authorizes stage save/export, local file persistence, or Nucleus writes.
For tests or unspecified locations, prefer {PLAYGROUND_ROOT}.
Ask before namespace reset unless clearly authorized. Reset is not a Kit restart, task
cancellation, module unload, or full cleanup. Client timeout does not prove execution stopped.
Experiment-control tools may be used without repeated confirmation when the task explicitly
asks to record an experiment. Preserve unrelated active experiments and existing records.
Extension inspection and profiler status are read-only. Extension enable/disable/reload and
bounded profiler capture are RUNTIME_CONTROL and require clear task/session authorization.
Extension targets are canonical unversioned identities; general extension control is local-only,
never installs packages, never cascades, and protects the Kit Lab control plane. Standalone
Kit Lab self-disable is forbidden and self-reload is restricted. Profiler capture uses a fixed
bounded duration and infrastructure-managed evidence root; callers cannot select output paths.
It preserves pre-existing capture state and does not enable Tracy, GPU profiling, or external tools.
Lifecycle status/log discovery identify only KHL_KIT_ID=nchc-kit-dev-main independently
of HTTP, parentage, or saved PID. Start/stop/restart require task/session authorization
for that process-control class; a clear lifecycle request suffices without repeated prompts.
Stop/restart can discard unsaved runtime state. Graceful shutdown is the default; force=true
requires explicit forced-termination authorization. Never signal ambiguous or untagged Kit.
Setup is an explicit local CLI step, not a general MCP file-write tool. Start uses only the
approved launcher/config, can produce Kit-managed runtime files and launch-output logs,
and never grants arbitrary submitted-Python persistence. Readiness timeout preserves Kit.
Log discovery returns paths only; reading native log contents requires task/session consent
for the permitted root or file and remains subject to host/harness permissions.
No arbitrary-file-write or destructive-external tool is exposed by this server. Tool annotations
are hints; descriptions and the kit_lab_policy result define usage policy.
"""


def _annotations(
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
) -> dict[str, bool]:
    return {
        "read_only_hint": read_only,
        "destructive_hint": destructive,
        "idempotent_hint": idempotent,
        "open_world_hint": False,
    }


def _entry(
    policy_class: str,
    *,
    side_effects: str,
    persistence: str,
    default_behavior: str,
    implicit_authorization: str,
    explicit_authorization: str,
    safer_alternative: str,
    description: str,
    annotations: dict[str, bool],
) -> dict[str, Any]:
    return {
        "class": policy_class,
        "side_effects": side_effects,
        "persistence": persistence,
        "default_behavior": default_behavior,
        "implicit_authorization": implicit_authorization,
        "explicit_authorization": explicit_authorization,
        "safer_alternative": safer_alternative,
        "description": description,
        "annotations": annotations,
    }


_READ_ONLY_AUTH = "Relevant inspection intent in the current task is sufficient."
_NO_EXPLICIT_AUTH = "None."


TOOL_POLICIES: dict[str, dict[str, Any]] = {
    "kit_lab_policy": _entry(
        "READ_ONLY",
        side_effects="none",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="none",
        description=(
            "Return the authoritative KHL Kit Lab policy, permission choices, "
            "playground guidance, and per-tool classifications without contacting Kit."
        ),
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),
    "kit_lab_status": _entry(
        "READ_ONLY",
        side_effects="one loopback HTTP read from the local Kit laboratory",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="none",
        description="Check whether the local Kit laboratory is reachable and ready; do not modify it.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),
    "kit_runtime_info": _entry(
        "READ_ONLY",
        side_effects="one loopback HTTP read from the local Kit runtime",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="none",
        description="Read Kit/app versions and active renderer or multi-GPU settings without modification.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),
    "kit_stage_summary": _entry(
        "READ_ONLY",
        side_effects="lightweight stage context by default; opt-in full prim traversal may be expensive",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="none",
        description="Read basic stage context without full prim traversal. Set include_statistics=true to compute full Traverse() counts; this may be expensive. Otherwise statistics_computed is false and counts are null.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),

    "kit_extensions_list": _entry(
        "READ_ONLY",
        side_effects="bounded inspection of installed Kit extension metadata",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="none",
        description="List installed Kit extensions with optional filters; do not enable or disable them.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),

    "kit_extension_inspect": _entry(
        "READ_ONLY",
        side_effects="complete local extension catalog, solver, dependency and reverse-dependent inspection",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="kit_extensions_list",
        description=(
            "Inspect one canonical unversioned local Kit extension identity, its exact installed "
            "candidates, enabled ID, solver result, dependencies, active reverse dependents, "
            "reloadability and protection reasons. Refuse uncertainty rather than fetching registry data."
        ),
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),
    "kit_extension_enable": _entry(
        "RUNTIME_CONTROL",
        side_effects="enable one locally installed extension and its already-local compatible dependency plan",
        persistence="current Kit process/session",
        default_behavior="ask",
        implicit_authorization="A clear request to enable the named local extension is sufficient.",
        explicit_authorization="Ask unless extension mutation is already authorized for the task/session.",
        safer_alternative="kit_extension_inspect",
        description=(
            "Enable one canonical unversioned extension only after complete local solver and protection "
            "checks. Never search, download, install, change search paths, restart, or replace Kit. "
            "Already-enabled is a verified no-op."
        ),
        annotations=_annotations(read_only=False, destructive=False, idempotent=True),
    ),
    "kit_extension_disable": _entry(
        "RUNTIME_CONTROL",
        side_effects="disable one reloadable installed extension after active-dependent checks",
        persistence="current Kit process/session",
        default_behavior="ask",
        implicit_authorization="A clear request to disable the named local extension is sufficient.",
        explicit_authorization="Ask unless extension mutation is already authorized for the task/session.",
        safer_alternative="kit_extension_inspect",
        description=(
            "Disable one safe canonical extension without cascade or force. Refuse active dependents, "
            "incomplete graphs, non-reloadable targets, Kit Lab self-disable, and protected control-plane dependencies."
        ),
        annotations=_annotations(read_only=False, destructive=True, idempotent=True),
    ),
    "kit_extension_reload": _entry(
        "RUNTIME_CONTROL",
        side_effects="disable, rediscover and re-enable one reloadable extension in the same Kit process",
        persistence="current Kit process/session; extension-owned runtime state may be recreated",
        default_behavior="ask",
        implicit_authorization="A clear request to reload the named local extension is sufficient.",
        explicit_authorization="Ask unless extension mutation is already authorized for the task/session.",
        safer_alternative="kit_extension_inspect",
        description=(
            "Reload one enabled safe extension by exact-ID disable, fresh unversioned rediscovery and "
            "verified re-enable. No restart fallback. Kit Lab self-reload is explicitly restricted."
        ),
        annotations=_annotations(read_only=False, destructive=True, idempotent=False),
    ),
    "kit_profiler_status": _entry(
        "READ_ONLY",
        side_effects="inspect already-loaded Carbonite profiler state and installed profiler extension metadata",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="none",
        description=(
            "Report truthful installed and already-loaded built-in profiler capabilities without enabling "
            "extensions, acquiring a capture-starting backend, changing settings, or writing capture files."
        ),
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),
    "kit_profiler_capture": _entry(
        "RUNTIME_CONTROL",
        side_effects="temporarily adjust Carbonite CPU mask/Python instrumentation and write bounded infrastructure evidence",
        persistence="private infrastructure-managed JSON under the fixed profile root",
        default_behavior="ask",
        implicit_authorization="A clear bounded built-in profiler capture request is sufficient.",
        explicit_authorization="Ask unless profiler runtime control and infrastructure output are authorized.",
        safer_alternative="kit_profiler_status",
        description=(
            "Run a server-owned Carbonite CPU in-memory capture for at most 10 seconds, verify a unique "
            "native event marker, restore exact prior mask/Python state, and persist bounded private evidence. "
            "Optional Python instrumentation is part of the Carbonite trace, not cProfile .prof output. "
            "No caller path, Tracy, GPU backend, external viewer, registry installation, or restart is used."
        ),
        annotations=_annotations(read_only=False, destructive=True, idempotent=False),
    ),
    "kit_profiler_capture_status": _entry(
        "READ_ONLY",
        side_effects="read one generated capture's bounded infrastructure evidence or bridge operation state",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="kit_profiler_status",
        description=(
            "Read status and bounded evidence for one generated profiler capture ID. This is not a generic "
            "file reader and cannot select paths or cancel unrelated work."
        ),
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),

    "kit_viewport_info": _entry(
        "READ_ONLY",
        side_effects="inspect active viewport metadata",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="none",
        description="Read the active viewport's camera, resolution, and render-product path without modification.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),


    "kit_execute_python": _entry(
        "ELEVATED_EXECUTION",
        side_effects="unrestricted source executes inside the persistent local Kit interpreter",
        persistence="Kit process/session; experiment infrastructure may record source and results",
        default_behavior="ask",
        implicit_authorization="None; a task needing scene changes does not implicitly authorize Python.",
        explicit_authorization=(
            "Allow once or allow for this session. Also honor deny once, deny for this session, "
            "or continue with a safer alternative."
        ),
        safer_alternative="Use retained read-only context tools or inspect installed source/documentation when sufficient.",
        description=(
            "Develop and debug reusable installed-version Kit/USD Python in the persistent local "
            "Kit. Use it for scene, attribute, and settings investigation; inspection intent does not "
            "make arbitrary Python read-only. Preserves top-level await, last-expression results, "
            "stdout/stderr/traceback capture, and experiment source recording. Before first "
            "use, ask unless the user already allowed it once/session; offer: Allow once, Allow for "
            "this session, Deny once, Deny for this session, or Continue without Python/use a safer "
            "alternative. Authorization never permits local/Nucleus/Git writes, save/export, external "
            "package injection, long blocking loops, or bypassing another policy denial."
        ),
        annotations=_annotations(read_only=False, destructive=True, idempotent=False),
    ),
    "kit_reset_python_session": _entry(
        "RUNTIME_CONTROL",
        side_effects="clear variables retained in the persistent Kit Python namespace",
        persistence="current Kit process/session",
        default_behavior="ask",
        implicit_authorization="A clear request to reset/clear the Kit Python session is sufficient.",
        explicit_authorization="Ask once/session when reset intent is not already clear.",
        safer_alternative="Inspect kit_lab_status and avoid resetting when not required.",
        description=(
            "Clear retained variables in the persistent Kit Python namespace. Ask unless the task "
            "clearly authorizes session reset; this may invalidate live development state. "
            "This is not a Kit restart, task cancellation, module unload, or full cleanup."
        ),
        annotations=_annotations(read_only=False, destructive=True, idempotent=True),
    ),
    "kit_experiment_start": _entry(
        "EXPERIMENT_CONTROL",
        side_effects="create and activate an infrastructure-managed experiment directory",
        persistence="local experiment disk",
        default_behavior="allow",
        implicit_authorization="An explicit recorded-experiment task is sufficient.",
        explicit_authorization="Required when no experiment was requested.",
        safer_alternative="kit_experiment_current",
        description=(
            "Create and activate one durable infrastructure-managed experiment record. Use without "
            "repeated confirmation only when the task explicitly asks to run or record an experiment."
        ),
        annotations=_annotations(read_only=False, destructive=False, idempotent=False),
    ),
    "kit_experiment_current": _entry(
        "READ_ONLY",
        side_effects="read current experiment metadata",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="none",
        description="Return the active experiment, or a clean inactive result, without modifying records.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),
    "kit_experiment_list": _entry(
        "READ_ONLY",
        side_effects="bounded read of experiment summaries",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="none",
        description="List bounded experiment summaries with optional filters without modifying records.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),
    "kit_experiment_get": _entry(
        "READ_ONLY",
        side_effects="bounded read of one experiment manifest and recent events",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="kit_experiment_list",
        description="Return one experiment manifest and bounded recent events without modifying records.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),
    "kit_experiment_note": _entry(
        "EXPERIMENT_CONTROL",
        side_effects="append a bounded note to the active infrastructure-managed experiment",
        persistence="local experiment disk",
        default_behavior="allow",
        implicit_authorization="An explicit recorded-experiment task is sufficient.",
        explicit_authorization="Required when no experiment was requested.",
        safer_alternative="kit_experiment_current",
        description=(
            "Append a bounded plain-text note to the active infrastructure-managed experiment. "
            "An explicit recorded-experiment task authorizes this without repeated confirmation."
        ),
        annotations=_annotations(read_only=False, destructive=False, idempotent=False),
    ),
    "kit_experiment_finish": _entry(
        "EXPERIMENT_CONTROL",
        side_effects="finish the active experiment and write its infrastructure-managed summary",
        persistence="local experiment disk",
        default_behavior="allow",
        implicit_authorization="An explicit recorded-experiment task is sufficient.",
        explicit_authorization="Required when no experiment was requested.",
        safer_alternative="kit_experiment_current",
        description=(
            "Finish the active infrastructure-managed experiment, write its summary, and clear the "
            "active pointer. An explicit recorded-experiment task authorizes this action."
        ),
        annotations=_annotations(read_only=False, destructive=False, idempotent=False),
    ),
    "kit_lifecycle_config": _entry(
        "READ_ONLY", side_effects="read local lifecycle config and candidate process identity metadata",
        persistence="none", default_behavior="allow", implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH, safer_alternative="none",
        description="Read the approved launch configuration and /proc inspection diagnostics for nchc-kit-dev-main. Setup uses lifecycle-user-local.sh setup; this tool changes no configuration.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),
    "kit_status": _entry(
        "READ_ONLY", side_effects="inspect Kit candidate executable/environment identity and loopback readiness",
        persistence="none", default_behavior="allow", implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH, safer_alternative="none",
        description="Rediscover nchc-kit-dev-main by KHL_KIT_ID in the OS process environment, then correlate bridge PID/start time. Report STOPPED, STARTING, READY, UNRESPONSIVE, AMBIGUOUS or inspection failure without requiring Kit to respond. Never adopt an untagged Kit.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),
    "kit_start": _entry(
        "RUNTIME_CONTROL", side_effects="launch the approved local Kit with persistent identity and poll readiness",
        persistence="Kit-managed runtime files and infrastructure-managed launch output",
        default_behavior="ask", implicit_authorization="A clear request to start this development Kit is sufficient.",
        explicit_authorization="Ask unless task/session already authorizes starting Kit.",
        safer_alternative="kit_status",
        description="Start nchc-kit-dev-main using the approved absolute launcher, cwd and structured arguments. Refuse ambiguity or untagged Kit. Default readiness wait is 180 seconds with progress; timeout preserves the process. Honor existing lifecycle authorization; this may create Kit runtime and launch-output files.",
        annotations=_annotations(read_only=False, destructive=False, idempotent=True),
    ),
    "kit_stop": _entry(
        "RUNTIME_CONTROL", side_effects="terminate exactly one freshly identified Kit, potentially losing unsaved state",
        persistence="Kit may write its normal shutdown state", default_behavior="ask",
        implicit_authorization="A clear request to stop this development Kit is sufficient for SIGTERM.",
        explicit_authorization="Ask unless stop is authorized; force=true requires explicit forced-termination authorization.",
        safer_alternative="kit_status",
        description="Stop nchc-kit-dev-main by its environment identity, independent of HTTP and MCP parentage. Validate a pidfd before signalling. SIGTERM waits 30 seconds by default; force=false preserves Kit on timeout. Only explicit force=true permits SIGKILL and a separate bounded wait. May lose unsaved state; never signal unrelated or ambiguous processes.",
        annotations=_annotations(read_only=False, destructive=True, idempotent=True),
    ),
    "kit_restart": _entry(
        "RUNTIME_CONTROL", side_effects="stop identified Kit and launch a new process with the same persistent identity",
        persistence="Kit-managed runtime files and infrastructure-managed launch output", default_behavior="ask",
        implicit_authorization="A clear request to restart this development Kit is sufficient for graceful restart.",
        explicit_authorization="Ask unless restart is authorized; force=true requires explicit forced-termination authorization.",
        safer_alternative="kit_status",
        description="Rediscover and stop nchc-kit-dev-main, confirm exit, then launch with the same KHL_KIT_ID and wait for readiness. PID may change and MCP need not have launched the prior Kit. Refuse ambiguity and process replacement; no new launch after failed shutdown. Readiness timeout preserves the new process. May lose unsaved state; honor existing lifecycle authorization.",
        annotations=_annotations(read_only=False, destructive=True, idempotent=False),
    ),
    "kit_log_paths": _entry(
        "READ_ONLY", side_effects="read native log-path setting or identified process descriptor links; no log contents",
        persistence="none", default_behavior="allow", implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH, safer_alternative="kit_status",
        description="Discover native Kit and captured launch-output paths for nchc-kit-dev-main using matching bridge identity or open process descriptors. Enforce configured roots, report association/ambiguity, and never choose by newest mtime. Returns paths only. Read contents separately through normal tools after task/session consent; this tool grants no host permissions.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),

}

POLICY_FINGERPRINT = hashlib.sha256(
    json.dumps(
        {
            "policy_version": POLICY_VERSION,
            "server_instructions": SERVER_INSTRUCTIONS,
            "tool_policies": TOOL_POLICIES,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()[:16]


def tool_description(tool_name: str) -> str:
    return str(TOOL_POLICIES[tool_name]["description"])


def tool_annotations(tool_name: str) -> dict[str, bool]:
    return dict(TOOL_POLICIES[tool_name]["annotations"])


def tool_meta(tool_name: str) -> dict[str, Any]:
    policy = TOOL_POLICIES[tool_name]
    return {
        "khl_policy": {
            "version": POLICY_VERSION,
            "fingerprint": POLICY_FINGERPRINT,
            "class": policy["class"],
            "default_behavior": policy["default_behavior"],
            "persistence": policy["persistence"],
        }
    }


def policy_payload() -> dict[str, Any]:
    return {
        "policy_version": POLICY_VERSION,
        "policy_fingerprint": POLICY_FINGERPRINT,
        "advisory": True,
        "annotations_are_security_boundaries": False,
        "default_playground_root": PLAYGROUND_ROOT,
        "python_permission_choices": list(PYTHON_PERMISSION_CHOICES),
        "persistent_write_tools_exposed": False,
        "destructive_external_tools_exposed": False,
        "server_instructions": SERVER_INSTRUCTIONS,
        "tools": [
            {"tool": tool_name, **deepcopy(policy)}
            for tool_name, policy in sorted(TOOL_POLICIES.items())
        ],
    }
