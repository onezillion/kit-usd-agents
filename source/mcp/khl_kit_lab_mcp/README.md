# KHL Kit Lab Runtime MCP

Version 0.5.0 is the local runtime adapter for the persistent
`omni.khl.kit_lab` extension. It preserves the Phase 2C experiment layer and
focuses on developing, executing, observing, and debugging real Kit/USD Python.
Stage C+D adds persistent-identity lifecycle management and native log-path discovery.
The bridge API/extension is 0.4.0; the existing 14-tool Stage A+B baseline is preserved.

The package contains no model, embedding, retrieval, reranking, hosted API,
Kubernetes or LLM-provider logic. It does not depend on the Kit knowledge MCP.

## Boundaries

- MCP endpoint: `http://127.0.0.1:9910/mcp`
- Kit Lab endpoint: `http://127.0.0.1:8011`
- Both listeners remain loopback-only.
- Remote Kit HTTP targets are rejected unless `KIT_LAB_ALLOW_REMOTE=true` is
  explicitly set. The MCP listener itself cannot be exposed remotely.
- The caller cannot choose an arbitrary persistence root through an MCP tool.
- No generic local-file read/write/copy/upload/delete, stage-save, Nucleus-save or
  arbitrary-process-control tool is exposed.
- No credentials are required.
- Tool policy is advisory/model-visible in this phase. It does not replace the
  loopback boundary or user/host authorization.

## Architecture

The MCP forwards seven runtime operations to Kit. While an
experiment is active, one common invocation path records those operations.
Experiment-management calls are handled entirely by the MCP
process and do not require a Kit extension change or Kit restart.

Each generated experiment ID maps to:

```text
<experiment-id>/
├── manifest.json
├── events.jsonl
├── scripts/
├── results/
├── artifacts/
└── summary.md
```

`events.jsonl` is a compact chronological index. Bounded complete operation
records are stored under `results/`. Submitted `kit_execute_python` source is
stored under `scripts/`. `artifacts/` is reserved and intentionally unused in
this phase.

Manifests, summaries and the current pointer use same-directory temporary files
and `os.replace()`. Event appends are flushed and synced. An in-process lock
protects sequence allocation and persistence writes. Directories use mode
`0700` and files use `0600` where the filesystem permits.

Only one runtime MCP process may use a given experiment root. This lock is not
an inter-process lock; do not run two servers against the same root.

## MCP policy

The server exposes one policy through four synchronized surfaces:

- server-level MCP instructions;
- exact descriptions, annotations, and `khl_policy` metadata on every tool;
- the read-only `kit_lab_policy` tool;
- [`MCP_POLICY.md`](MCP_POLICY.md), the repo-local human-readable manual.

Policy fingerprint: `09469759cd1b6289`

Read-only context inspection is allowed freely. Develop reusable installed-version
Kit/USD scripts, extensions, and scripting-component source through authorized Python.
Use Python for scene, attribute, and settings investigation; a successful MCP shortcut
is not evidence that a standalone source deliverable is correct. For tests or an
unspecified location, prefer `/World/AgentSceneLab`. Live mutation never authorizes
save/export, arbitrary local files, or Nucleus writes.

Before first use of `kit_execute_python`, an agent must ask unless general Kit
Python was already authorized for the task/session. The required choices are
allow once, allow for session, deny once, deny for session, or continue with a
safer alternative. See `MCP_POLICY.md` for the exact semantics and mandatory
restrictions.

Tool annotations are hints rather than enforcement boundaries. The policy
synchronization tests deliberately fail if a tool is added without updating its
canonical policy metadata and manual.

## Tools

Read-only policy and inspection tools:

- `kit_lab_policy`

- `kit_lab_status`
- `kit_runtime_info`
- `kit_stage_summary`
- `kit_extensions_list`
- `kit_viewport_info`

Elevated/runtime-control tools:

- `kit_execute_python`
- `kit_reset_python_session`

Experiment tools:

- `kit_experiment_start`
- `kit_experiment_current`
- `kit_experiment_list`
- `kit_experiment_get`
- `kit_experiment_note`
- `kit_experiment_finish`
- `kit_lifecycle_config`
- `kit_status`
- `kit_start`
- `kit_stop`
- `kit_restart`
- `kit_log_paths`

`kit_stage_summary` defaults to `include_statistics=false`. It returns basic stage
context without traversing prims: `statistics_computed=false`, `prim_count=null`, and
`type_counts=null`. Root-child paths are limited to 256 with an explicit truncation flag.
Pass `{"include_statistics": true}` only when a full `Usd.Stage.Traverse()` count is
needed; it may be expensive and uses USD's default traversal predicate. Counts are
computed without building a list of every prim. No stage also returns uncomputed null
counts; an empty stage with statistics enabled returns zero and an empty type map.

Only one experiment can be active. Starting another produces a clear conflict
until the current experiment is finished. `kit_experiment_list` supports
bounded status/tag filtering and offset pagination. `kit_experiment_get`
returns only a bounded recent event list.

Python exceptions returned by Kit remain legitimate tool results and are
recorded with `result_class=python_exception_result`. Transport or MCP-side
failures use `result_class=transport_or_server_failure`.

## Install and static tests

From the repository root:

```bash
source/mcp/khl_kit_lab_mcp/setup-user-local.sh
source/mcp/khl_kit_lab_mcp/test-user-local.sh
```

The Python environment remains under:

```text
/home/ubuntu/kit-ai/venvs/khl-kit-lab-mcp
```

## Run and live verification

Keep Kit and the knowledge MCP running. Start only this runtime MCP in another
terminal:

```bash
source/mcp/khl_kit_lab_mcp/run-user-local.sh
```

From a third terminal:

```bash
source/mcp/khl_kit_lab_mcp/verify-user-local.sh
```

Default verification is read-only: it checks exactly 20 tools, synchronized metadata,
runtime status, lightweight summary, and the current experiment. An old loaded bridge
will fail the POST summary check (HTTP 405); activate bridge API 0.3.0 or newer separately (0.4.0 for lifecycle).

Explicitly opt in to durable experiment writes and a `2 + 2` Python execution:

```bash
source/mcp/khl_kit_lab_mcp/verify-user-local.sh --full
```

The full check preserves unrelated active experiments by refusing to start when one
exists. It records source/results and verifies finished-record retrieval. It does not
validate scene-editing source. Stage F requires separate authorization to execute a
reusable Kit/USD script, inspect its temporary scene changes independently with Python,
and verify cleanup, runtime health, and durable source/result evidence.

After restarting only the runtime MCP, use the printed ID:

```bash
source/mcp/khl_kit_lab_mcp/verify-user-local.sh --post-restart <experiment-id>
```

## Environment variables

| Name | Default | Purpose |
|---|---|---|
| `KIT_LAB_BASE_URL` | `http://127.0.0.1:8011` | Kit HTTP service |
| `KIT_LAB_TIMEOUT_SECONDS` | `30` | Individual Kit request timeout |
| `KIT_LAB_MCP_HOST` | `127.0.0.1` | MCP bind host; loopback enforced |
| `KIT_LAB_MCP_PORT` | `9910` | MCP port |
| `KIT_LAB_ALLOW_REMOTE` | unset | Explicit remote Kit-target opt-in |
| `KIT_LAB_EXPERIMENT_ROOT` | `/home/ubuntu/kit-ai/lab/experiments` | Durable experiment storage |

## Safety notes

`kit_execute_python` executes real code in persistent Kit and is marked potentially
destructive even for inspection. Follow its once/session permission rules and honor
existing task/session authorization without repeated prompts. Authorization does not
permit stage save/export, arbitrary filesystem or Nucleus writes, repository
mutation, external-package injection, blocking loops, or policy bypass. Its
source is intentionally stored verbatim whenever an experiment is active.
Never put credentials in submitted Python source, titles, objectives, tags,
notes, summaries, arguments or generated output.

The Python executor preserves top-level await, last-expression results, and captured
stdout/stderr/traceback. Namespace reset is not a Kit restart, task cancellation,
module unload, or full cleanup. A client timeout does not prove execution stopped.

Stored arguments and results are bounded and apply conservative key/pattern
scrubbing. That scrubbing is defense in depth, not a perfect secret-redaction
system. The persistence code never enumerates or captures the process
environment, and users or agents must not use Python execution to place
environment contents in experiment output.

Experiment IDs are generated internally. All ID-derived paths are validated,
resolved and checked for containment under the configured root. Malformed IDs,
unsafe paths and corrupt manifests produce explicit errors. Experiment deletion
is intentionally not implemented.

## VS Code

Preserve the existing `kit-dev-mcp` entry and keep:

```json
"kit-lab-runtime": {
  "type": "http",
  "url": "http://127.0.0.1:9910/mcp"
}
```

## Kit lifecycle and native log paths (Stage C+D)

The six additions bring the exact inventory to **20 tools**. Every lifecycle tool
accepts `kit_id`, default/only value `nchc-kit-dev-main`; callers cannot supply a PID,
command, path, environment dictionary, or port.

| Tool | Additional arguments | Result |
|---|---|---|
| `kit_lifecycle_config` | none | Approved configuration, service UID and inspection diagnostics |
| `kit_status` | none | Fresh OS identification plus readiness state and process metadata |
| `kit_start` | `readiness_timeout: number|null` | Existing status or launch/readiness result, captured output path |
| `kit_stop` | `force: boolean=false` | Graceful shutdown result; force explicitly permits bounded SIGKILL |
| `kit_restart` | `force=false`, `readiness_timeout=null` | Confirmed stop then launch result; stable identity, new PID |
| `kit_log_paths` | none | Native candidates, selected path, association, launch output paths, `contents_read=false` |

Readiness overrides must be greater than zero and at most 3600 seconds. The SDK's
injected context sends MCP progress notifications during polling; it is not a public
argument. Errors such as invalid config, ambiguity during control, or denied inspection
return MCP tool errors with stable error codes. Status reports these discovery conditions
as states. No lifecycle call relies on experiment state or records experiment events.

Configure the existing local environment; no installation is needed:

```bash
source/mcp/khl_kit_lab_mcp/lifecycle-user-local.sh setup
source/mcp/khl_kit_lab_mcp/lifecycle-user-local.sh config
source/mcp/khl_kit_lab_mcp/lifecycle-user-local.sh status
source/mcp/khl_kit_lab_mcp/lifecycle-user-local.sh log-paths
# With authorization for the corresponding operation:
source/mcp/khl_kit_lab_mcp/lifecycle-user-local.sh start --timeout 180
source/mcp/khl_kit_lab_mcp/lifecycle-user-local.sh stop
source/mcp/khl_kit_lab_mcp/lifecycle-user-local.sh restart
```

Setup writes private machine configuration to `~/kit-ai/lab/lifecycle.json` (0600).
The optional global CLI `--config /absolute/path` or `KIT_LAB_LIFECYCLE_CONFIG` selects
another operator-controlled file outside Git. Setup accepts `--launcher`, `--cwd`,
`--kit-executable`, repeated `--argument=--literal-flag`, `--log-root`, `--capture-root`,
`--readiness-timeout`, and `--shutdown-timeout`. Configuration changes are picked up
by the next MCP lifecycle call; they are not exposed as arbitrary MCP tool arguments.

Defaults are the existing absolute `nchc.khl.editor.full.sh` launcher, repository cwd,
SDK `kit` executable, empty extra argument array, loopback bridge 8011, and native
log root `~/.nvidia-omniverse/logs`. Launch output goes to private per-launch files under
`~/kit-ai/lab/launches`. `Popen` receives an argv array and injected `KHL_KIT_ID`; no eval,
new env file, or launcher rewrite is used. Existing launcher `$EXTRA_FLAGS` behavior
is preserved. MCP Python-path/virtualenv settings and `NVIDIA_API_KEY` are removed from
the child environment. Other inherited launch settings remain the operator's concern.
Manual launches must also export `KHL_KIT_ID="nchc-kit-dev-main"`; the user added that
line to the existing launcher. The export affects subsequent launches, not running Kit.

Every operation scans candidate processes by exact configured executable and checks only
the NUL-delimited `KHL_KIT_ID` entry in `/proc/<pid>/environ`. It does not expose full
environments. Zero matches means STOPPED; one is identified; multiple means AMBIGUOUS.
Unreadable candidates mean INSPECTION_DENIED, never STOPPED. An untagged Kit is never
signalled and prevents a new start. Identification does not depend on HTTP, parentage,
MCP startup order, remembered PIDs, or a persistent ownership registry. A private advisory
lock serializes CLI/MCP mutations; it contains no process identity. Manually launched
Kit remains discoverable when MCP restarts and vice versa.

A matching bridge response must contain the same stable identity, PID and kernel start
ticks. `ready=true` yields READY. Unavailable/not-ready probes produce STARTING during
the default startup window, then UNRESPONSIVE. Identity mismatches are immediately
UNRESPONSIVE. Inspection/process races are explicit errors requiring rediscovery.
A probe takes at most 2 seconds by default; elapsed deadlines can overshoot by the final
probe and OS inspection. Start waits 180 seconds by default and preserves an identified
alive-but-not-ready process on timeout. A launcher that exits without identifiable Kit
reports STOPPED/launch failure; an alive launcher without Kit reports STARTING with
`KIT_NOT_IDENTIFIED_YET` and timeout, without claiming a managed Kit was identified.

Stop validates the unique process and opens a pidfd, revalidating executable, start time,
UID and environment before each signal. SIGTERM waits a separate **30 seconds** by default.
Timeout preserves the process. Only explicit `force=true` / CLI `--force` allows SIGKILL,
with another **5 seconds** of bounded waiting. A replaced or ambiguous process is never
arbitrarily selected; restart launches only after confirmed exit. Readiness failure does
not authorize forced shutdown. Configured probe/poll/force waits are bounded to (0,3600].

`kit_log_paths` prefers Carbonite `/log/file` from the matched bridge. It cross-checks
open descriptors and, when HTTP is unavailable, can return the identified process's single
native `kit_*.log` descriptor under configured roots. Multiple candidates remain explicit
ambiguity. Stdout/stderr descriptor paths under the capture root identify launch output.
No file contents, directory mtime ranking, newest-log guesses, or generic log reader are
used. Host access and separate task/session native-log consent remain necessary for any
later content read. Setup checks currently observable process permissions but cannot
grant OS/sandbox access or predict future permission changes.

The default verifier checks the 20-tool metadata and unchanged Stage A+B read-only
runtime behavior. Add `--lifecycle` for configured READY identity/log association checks;
this remains read-only and never starts, stops, or restarts Kit.
