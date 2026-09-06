"""Authoritative model-visible policy metadata for KHL Kit Lab MCP tools."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


POLICY_VERSION = "1.0"
PLAYGROUND_ROOT = "/World/AgentSceneLab"
PYTHON_PERMISSION_CHOICES = [
    "Allow once",
    "Allow for this session",
    "Deny once",
    "Deny for this session",
    "Continue without Kit Python / use a safer alternative",
]

SERVER_INSTRUCTIONS = f"""KHL Kit Lab controls one local development Kit runtime.
Read-only inspection tools may be used without asking. Live scene mutation tools may be
used when the user's task clearly asks to build, change, remove, or clean live stage state;
if a test or unspecified location needs a playground, prefer {PLAYGROUND_ROOT}. Live
mutation never authorizes stage save/export, local file persistence, or Nucleus writes.
Before the first kit_execute_python call, ask unless the user already authorized general
Kit Python for this task/session. Offer allow once, allow for session, deny once, deny for
session, or continue with a safer alternative. Session authorization does not permit file,
Nucleus, Git, external-package, long-blocking-loop, or policy-bypass behavior. Ask before
runtime/session reset unless clearly authorized. Experiment-control tools may be used
without repeated confirmation when the task explicitly asks to record an experiment.
No persistent-write or destructive-external tool is exposed by this server. Tool
annotations are hints; descriptions and the kit_lab_policy result define usage policy.
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
        side_effects="bounded inspection of the active USD stage",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="none",
        description="Read a bounded summary of the active USD stage without modifying it.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
    ),
    "kit_prim_inspect": _entry(
        "READ_ONLY",
        side_effects="bounded inspection of one active USD prim",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="kit_stage_summary",
        description=(
            "Inspect one existing prim's state, properties, metadata, and transform ops "
            "without modifying the stage."
        ),
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
    "kit_setting_get": _entry(
        "READ_ONLY",
        side_effects="read one Carb setting from the active Kit process",
        persistence="none",
        default_behavior="allow",
        implicit_authorization=_READ_ONLY_AUTH,
        explicit_authorization=_NO_EXPLICIT_AUTH,
        safer_alternative="none",
        description="Read one Carb setting from the active Kit process without changing it.",
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
    "kit_prim_create": _entry(
        "LIVE_MUTATION",
        side_effects="create one new prim in the currently open live USD stage",
        persistence="live stage/session only; this tool never saves or exports",
        default_behavior="allow",
        implicit_authorization=(
            "A clear request to build/change the live scene or create the named prim is sufficient."
        ),
        explicit_authorization="Required only when the current task does not clearly request live mutation.",
        safer_alternative="kit_stage_summary or kit_prim_inspect",
        description=(
            f"Create one new live USD prim at an absolute path whose parent exists. Refuse "
            f"an existing prim. "
            f"For tests or unspecified locations, prefer {PLAYGROUND_ROOT}. Clear scene-building "
            "intent authorizes this live mutation without a separate prompt. This tool never "
            "saves/exports the stage or writes local/Nucleus content."
        ),
        annotations=_annotations(read_only=False, destructive=False, idempotent=False),
    ),
    "kit_prim_remove": _entry(
        "LIVE_MUTATION",
        side_effects="remove one exact prim subtree from the currently open live USD stage",
        persistence="live stage/session only; this tool never saves or exports",
        default_behavior="allow",
        implicit_authorization=(
            "A clear request to remove the named live prim or clean temporary test content is sufficient."
        ),
        explicit_authorization="Required when removal/cleanup intent is not clear from the current task.",
        safer_alternative="kit_prim_inspect before removal",
        description=(
            "Remove one exact live USD prim subtree after clear removal/cleanup intent. Root and "
            "/World removal are refused. This changes only live stage state and never saves/exports "
            "the stage or deletes local/Nucleus content."
        ),
        annotations=_annotations(read_only=False, destructive=True, idempotent=True),
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
        safer_alternative="Use a deterministic read-only or live-mutation Kit Lab tool when sufficient.",
        description=(
            "Execute unrestricted Python inside the persistent local development Kit. Before first "
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
            "clearly authorizes session reset; this may invalidate live development state."
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
