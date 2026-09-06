# KHL Kit Lab MCP Policy

Policy version: **1.0**  
Policy fingerprint: **d11cf0a1f76fe715**  
MCP package version: **0.3.0**

This document describes the model-visible advisory policy for the local KHL Kit Lab
runtime MCP. The canonical machine-readable entries live in
`src/khl_kit_lab_mcp/policy.py`; `kit_lab_policy` returns the same policy to agents.

Tool annotations are accurate hints, not authorization or security boundaries. The MCP
listener remains loopback-only. The server exposes no stage-save/export, arbitrary file
write, Nucleus mutation, Kit restart, or external destructive operation.

## Core behavior

- `READ_ONLY`: allow when relevant; no permission prompt is needed.
- `LIVE_MUTATION`: a clear request to build/change/remove live scene state is sufficient
  authorization. Do not ask before every deterministic operation.
- For a test or when the user did not choose a location, prefer
  `/World/AgentSceneLab` as a playground. This is guidance, not a path restriction.
- Live mutation never authorizes save/export, local filesystem persistence, or Nucleus
  writes.
- `ELEVATED_EXECUTION`: ask before the first `kit_execute_python` call unless general Kit
  Python was already authorized for this task/session.
- `RUNTIME_CONTROL`: ask unless the task clearly authorizes that runtime-control class.
- `EXPERIMENT_CONTROL`: allow without repeated confirmation when the task explicitly asks
  to run or record an experiment. Infrastructure-managed experiment files do not grant
  general filesystem permission.
- `PERSISTENT_WRITE` and `DESTRUCTIVE_EXTERNAL`: deny by default. No such tool is exposed
  in version 0.3.0.

## General Kit Python permission

Before first use, offer:

1. Allow once
2. Allow for this session
3. Deny once
4. Deny for this session
5. Continue without Kit Python / use a safer alternative

Allow-once covers one execution. Allow-for-session covers later Python calls in the same
agent/chat session. Deny-once skips only the current call. Deny-for-session means do not
ask again during the session. A material change in circumstances may justify asking again
only after a deny-once.

Authorization never permits Python to:

- save/export USD or write arbitrary local files;
- create/write/delete/rename/overwrite Nucleus content;
- call `omni.client` or equivalent persistence APIs for writes;
- use `/tmp` or another path as a policy escape;
- modify repository or Git state;
- inject arbitrary external virtual-environment packages into persistent Kit;
- block Kit with long synchronous loops, sleeps, or nested event-loop execution;
- bypass denial of another MCP tool or policy class.

Phase 2C experiment records are an exception because the experiment infrastructure—not
submitted Kit Python—owns those bounded writes.

## Complete tool classification

| Tool | Class | Side effects / persistence | Default | Authorization and safer alternative | Annotations |
|---|---|---|---|---|---|
| `kit_lab_policy` | READ_ONLY | None | Allow | Relevant inspection intent; no alternative needed | read-only, idempotent, closed-world |
| `kit_lab_status` | READ_ONLY | Loopback status read; none | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_runtime_info` | READ_ONLY | Loopback runtime read; none | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_stage_summary` | READ_ONLY | Bounded stage inspection; none | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_prim_inspect` | READ_ONLY | Bounded prim inspection; none | Allow | Use `kit_stage_summary` for a narrower overview | read-only, idempotent, closed-world |
| `kit_extensions_list` | READ_ONLY | Extension metadata read; none | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_setting_get` | READ_ONLY | Carb setting read; none | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_viewport_info` | READ_ONLY | Viewport metadata read; none | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_prim_create` | LIVE_MUTATION | Creates one new live prim; live stage/session only | Allow with clear build/change intent | Refuses existing prim; otherwise inspect first | mutating, non-destructive, non-idempotent, closed-world |
| `kit_prim_remove` | LIVE_MUTATION | Removes one exact live prim subtree; live stage/session only | Allow with clear removal/cleanup intent | Inspect exact prim first when uncertain | mutating, destructive, idempotent, closed-world |
| `kit_execute_python` | ELEVATED_EXECUTION | Arbitrary code in persistent Kit interpreter; session plus experiment evidence | Ask before first use | Use a deterministic tool when sufficient | mutating, potentially destructive, non-idempotent, closed-world |
| `kit_reset_python_session` | RUNTIME_CONTROL | Clears retained Python namespace; Kit session | Ask unless clearly requested | Avoid reset if inspection is sufficient | mutating, destructive, idempotent, closed-world |
| `kit_experiment_start` | EXPERIMENT_CONTROL | Creates active experiment record; local experiment disk | Allow for explicit experiment | Check `kit_experiment_current` first | mutating, non-destructive, non-idempotent, closed-world |
| `kit_experiment_current` | READ_ONLY | Reads current record; none | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_experiment_list` | READ_ONLY | Bounded experiment-list read; none | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_experiment_get` | READ_ONLY | Bounded manifest/event read; none | Allow | Use list first if ID is unknown | read-only, idempotent, closed-world |
| `kit_experiment_note` | EXPERIMENT_CONTROL | Appends bounded note; local experiment disk | Allow for explicit experiment | Check current experiment if uncertain | mutating, non-destructive, non-idempotent, closed-world |
| `kit_experiment_finish` | EXPERIMENT_CONTROL | Finishes record and writes summary; local experiment disk | Allow for explicit experiment | Check current experiment if uncertain | mutating, non-destructive, non-idempotent, closed-world |

## Controlled live prim operations

`kit_prim_create` and `kit_prim_remove` call the existing Kit Python HTTP endpoint with
server-generated source. The agent cannot supply source through these tools. Inputs are
limited to validated absolute USD prim paths and an enumerated schema type. They do not
call save/export or filesystem/Nucleus APIs.

Creation supports `Xform`, `Scope`, `Cube`, `Sphere`, `Cylinder`, `Cone`, `Capsule`,
`Camera`, `DistantLight`, `SphereLight`, `RectLight`, and `DiskLight`. It requires the
parent prim to exist and refuses to overwrite an existing prim. This prevents one request
from silently authoring an unspecified ancestor hierarchy. Removal refuses `/` and
`/World`; otherwise it operates on the exact requested live prim subtree and reports a
missing prim as a no-op.

## Maintenance rule

Every tool addition, removal, rename, or behavior change must update all applicable policy
surfaces in the same change:

1. `policy.py` classification, description, annotations, and `_meta`;
2. the tool registration in `server.py`;
3. this `MCP_POLICY.md` table and behavior text;
4. README tool inventory and safety notes;
5. verifier expected names/metadata checks;
6. policy synchronization tests.

`tests/test_policy.py` fails when registered tools and policy entries diverge, when exposed
descriptions/annotations/metadata differ from the canonical policy, or when this manual
does not name every registered tool. It also verifies the policy fingerprint in this
manual and README, so any policy/tool-description change requires both documents to be
reviewed and refreshed.
