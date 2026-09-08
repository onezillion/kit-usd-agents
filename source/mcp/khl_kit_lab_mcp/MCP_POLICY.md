# KHL Kit Lab MCP Policy

Policy version: **2.1**

Policy fingerprint: **09469759cd1b6289**

MCP package version: **0.5.0**

This document describes the model-visible advisory policy for the local KHL Kit Lab
runtime MCP. The canonical machine-readable entries live in
`src/khl_kit_lab_mcp/policy.py`; `kit_lab_policy` returns the same policy to agents.

Tool annotations are accurate hints, not authorization or security boundaries. The MCP
listener remains loopback-only. The server exposes no stage-save/export, arbitrary file
write, Nucleus mutation, or arbitrary process control. Scoped Kit lifecycle operations
require runtime-control authorization and can lose unsaved state.

## Core behavior

- `READ_ONLY`: allow when relevant; no permission prompt is needed.
- Develop, execute, observe, and debug reusable installed-version Kit/USD Python.
  Use Python for scene, attribute, and settings investigation as needed.
  Success through a scene shortcut does not validate a standalone source deliverable.
- Arbitrary Python remains `ELEVATED_EXECUTION` even for inspection.
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
  in version 0.5.0. Scoped lifecycle operations are RUNTIME_CONTROL.

## General Kit Python permission

If general Kit Python has not already been authorized for this task/session, before first use offer:

1. Allow once
2. Allow for this session
3. Deny once
4. Deny for this session
5. Continue without Kit Python / use a safer alternative

Allow-once covers one execution. Allow-for-session covers later Python calls in the same
agent/chat session within the authorized scope, without repeated prompts. Deny-once
skips only the current call. Deny-for-session means do not ask again during the session. A material change in circumstances may justify asking again
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
| `kit_stage_summary` | READ_ONLY | Lightweight context; opt-in full traversal; none | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_extensions_list` | READ_ONLY | Extension metadata read; none | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_viewport_info` | READ_ONLY | Viewport metadata read; none | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_execute_python` | ELEVATED_EXECUTION | Arbitrary code in persistent Kit interpreter; session plus experiment evidence | Ask before first use | Use retained context tools or installed source/docs when sufficient | mutating, potentially destructive, non-idempotent, closed-world |
| `kit_reset_python_session` | RUNTIME_CONTROL | Clears retained Python namespace; Kit session | Ask unless clearly requested | Avoid reset if inspection is sufficient | mutating, destructive, idempotent, closed-world |
| `kit_experiment_start` | EXPERIMENT_CONTROL | Creates active experiment record; local experiment disk | Allow for explicit experiment | Check `kit_experiment_current` first | mutating, non-destructive, non-idempotent, closed-world |
| `kit_experiment_current` | READ_ONLY | Reads current record; none | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_experiment_list` | READ_ONLY | Bounded experiment-list read; none | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_experiment_get` | READ_ONLY | Bounded manifest/event read; none | Allow | Use list first if ID is unknown | read-only, idempotent, closed-world |
| `kit_experiment_note` | EXPERIMENT_CONTROL | Appends bounded note; local experiment disk | Allow for explicit experiment | Check current experiment if uncertain | mutating, non-destructive, non-idempotent, closed-world |
| `kit_experiment_finish` | EXPERIMENT_CONTROL | Finishes record and writes summary; local experiment disk | Allow for explicit experiment | Check current experiment if uncertain | mutating, non-destructive, non-idempotent, closed-world |

| `kit_lifecycle_config` | READ_ONLY | Read approved configuration and process-inspection diagnostics | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_status` | READ_ONLY | Rediscover OS identity and probe matching bridge | Allow | Relevant inspection intent | read-only, idempotent, closed-world |
| `kit_start` | RUNTIME_CONTROL | Launch approved Kit; Kit runtime files and captured output | Ask unless authorized | Clear start intent; safer alternative: status | mutating, non-destructive, idempotent, closed-world |
| `kit_stop` | RUNTIME_CONTROL | Signal identified Kit; unsaved state may be lost | Ask unless authorized | Clear stop intent; forced kill requires explicit authorization | mutating, destructive, idempotent, closed-world |
| `kit_restart` | RUNTIME_CONTROL | Confirm exit then relaunch with stable identity | Ask unless authorized | Clear restart intent; forced kill requires explicit authorization | mutating, destructive, non-idempotent, closed-world |
| `kit_log_paths` | READ_ONLY | Read path setting/descriptor links, no file contents | Allow | Relevant inspection intent; content access needs separate task/session consent | read-only, idempotent, closed-world |

## Stage summary and Python state

`kit_stage_summary(include_statistics=false)` reads stage layers, default prim,
units, timing, and at most 256 root-child paths, with `root_children_truncated`.
It does not traverse prims by default: `statistics_computed=false`, `prim_count=null`,
and `type_counts=null`, including when no stage exists. With `include_statistics=true`,
counts use a full `Usd.Stage.Traverse()` with its default predicate (not `TraverseAll`).
This traversal may be expensive and is not bounded. Counts are accumulated without
retaining every prim. An empty traversed stage has a computed count of zero.

MCP uses the bridge's typed POST summary route. An old loaded bridge rejects this
request instead of silently performing its former expensive GET. Activate bridge API
0.3.0 or newer before checking live summary behavior; source edits alone do not reload Kit.
The compatibility GET now accepts the same optional `include_statistics` boolean.

The Python executor preserves its namespace, top-level await, last-expression result,
stdout/stderr/traceback capture, and experiment source recording. Namespace reset is
not a Kit restart, task cancellation, module unload, or full cleanup. A client timeout
does not prove in-Kit execution stopped. Preserve unrelated active experiments and
historical records, including events naming retired tools.

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

## Lifecycle and log-path scope

The only managed identity is `nchc-kit-dev-main`. Each call rediscovers the exact
configured Kit executable and its `KHL_KIT_ID` environment entry. There is no saved
PID or parent/child ownership requirement. Duplicate matches and unreadable candidates
prevent control. A pidfd and fresh executable/environment/start-time validation protect
signals against PID reuse. Host permissions remain necessary.

Graceful stop waits 30 seconds by default. Only explicitly authorized `force=true`
allows SIGKILL, followed by at most 5 seconds of waiting. Restart does not launch after
failed shutdown. Start waits 180 seconds by default (override up to 3600) and preserves
a process that is alive but not ready. Lifecycle configuration and captured launch output
are bounded infrastructure persistence, not permission for arbitrary filesystem writes.

`kit_log_paths` associates `/log/file` with matching OS identity, PID and start time;
open descriptors provide a fallback within configured roots. No newest-mtime selection
or log-content read occurs. Reading returned files through normal tools still requires
separate task/session native-log consent and host access. Lifecycle calls do not append
experiment events; existing runtime/experiment recording remains unchanged.
