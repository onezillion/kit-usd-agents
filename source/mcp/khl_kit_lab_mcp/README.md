# KHL Kit Lab Runtime MCP

Version 0.2.0 is the local runtime adapter for the persistent
`omni.khl.kit_lab` extension. It preserves the original nine Kit runtime tools
and adds six durable experiment-record tools.

The package contains no model, embedding, retrieval, reranking, hosted API,
Kubernetes or LLM-provider logic. It does not depend on the Kit knowledge MCP.

## Boundaries

- MCP endpoint: `http://127.0.0.1:9910/mcp`
- Kit Lab endpoint: `http://127.0.0.1:8011`
- Both listeners remain loopback-only.
- Remote Kit HTTP targets are rejected unless `KIT_LAB_ALLOW_REMOTE=true` is
  explicitly set. The MCP listener itself cannot be exposed remotely.
- The caller cannot choose an arbitrary persistence root through an MCP tool.
- No file read, copy, upload, delete, stage-save, Nucleus-save or Kit-restart
  tool is exposed.
- No credentials are required.

## Architecture

The MCP forwards the original typed runtime operations to Kit. While an
experiment is active, one common invocation path records every one of those
nine operations. Experiment-management calls are handled entirely by the MCP
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

## Tools

The original nine tools retain their annotations and behavior:

- `kit_lab_status`
- `kit_runtime_info`
- `kit_stage_summary`
- `kit_prim_inspect`
- `kit_extensions_list`
- `kit_setting_get`
- `kit_viewport_info`
- `kit_execute_python`
- `kit_reset_python_session`

Experiment tools:

- `kit_experiment_start`
- `kit_experiment_current`
- `kit_experiment_list`
- `kit_experiment_get`
- `kit_experiment_note`
- `kit_experiment_finish`

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

The live verifier discovers exactly 15 tools, creates a clearly named Phase 2C
verification experiment, adds a note, records stage inspection and `2 + 2`,
checks event/result/source files, finishes the experiment and prints its ID.
It deliberately retains that harmless experiment.

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

`kit_execute_python` remains an unrestricted development escape hatch and is
marked potentially destructive in its MCP annotations. Prefer deterministic
tools. Its source is intentionally stored verbatim whenever an experiment is
active. Never put credentials in submitted Python source, titles, objectives,
tags, notes, summaries, arguments or generated output.

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
