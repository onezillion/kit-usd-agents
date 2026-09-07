# User-local MCP stack

This repository owns five loopback-only MCP services. The root manager supervises
only process groups it started and records its state outside Git.

| Service | Address | Environment | Tools | Policy |
|---|---|---|---:|---|
| OmniUI MCP | `127.0.0.1:9901/mcp` | shared NVIDIA | 10 | read-only knowledge/retrieval |
| NVIDIA Kit MCP | `127.0.0.1:9902/mcp` | shared NVIDIA | 12 | read-only knowledge/retrieval |
| USD Code MCP | `127.0.0.1:9903/mcp` | shared NVIDIA | 7 | read-only knowledge/retrieval |
| Isaac Sim MCP | `127.0.0.1:9904/mcp` | shared NVIDIA | 5 | read-only knowledge/retrieval |
| KHL Kit Lab MCP | `127.0.0.1:9910/mcp` | separate Kit Lab | 18 | mixed, governed by its own policy |

The default shared environment is `/home/ubuntu/kit-ai/venvs/kit-usd-mcp`.
Set `KIT_USD_MCP_VENV` or pass `--venv ABSOLUTE_PATH` to override it. Kit Lab
remains in `/home/ubuntu/kit-ai/venvs/khl-kit-lab-mcp`; it is never installed
into the shared environment. The older `/home/ubuntu/kit-ai/venvs/nvidia-kit-mcp`
environment is not changed or removed.

## Setup and operation

Review setup without changes, then run it explicitly:

```bash
./setup-mcps-user-local.sh --dry-run
./setup-mcps-user-local.sh --yes
```

Setup uses user-space `uv`, Python 3.12, the fully resolved constraints in
`requirements-mcps-user-local.txt`, and editable packages from this checkout.
It materializes only the four relevant Git LFS data trees. The complete resolved
package set is saved at
`$KIT_USD_MCP_VENV/.kit-usd-mcp-resolved.txt` and reused as constraints on later
setup runs. Rerun setup after changing package metadata or when an editable
entrypoint must be refreshed. Normal `start` never installs or upgrades anything.
If the configured Git remote needs unavailable credentials but the same object
IDs exist on another trusted LFS server, set `KIT_USD_MCP_LFS_URL` for that setup
run. The override is passed to Git without being printed or persisted in Git
configuration.

```bash
./manage-mcps-user-local.sh start
./manage-mcps-user-local.sh status
./manage-mcps-user-local.sh verify
./manage-mcps-user-local.sh restart
./manage-mcps-user-local.sh stop
```

Append one or more service ids for selective operations, for example
`./manage-mcps-user-local.sh restart kit`. Valid ids are `omni-ui`, `kit`,
`usd-code`, `isaacsim`, and `kit-lab`. `manage-mcps-user-local.sh setup --yes`
is a convenience delegate to the explicit setup script.

`verify` initializes every endpoint, requires the exact inventory, checks every
description and input schema, checks annotation/`_meta` policy, and runs one
deterministic read-only call. Add `--semantic` to test the configured embedder
and reranker paths. Add `--full-kit-lab` only when a mutating Kit Lab verification
is intended: that existing verifier creates a durable experiment and briefly
creates, inspects, and removes a temporary prim.

## Credentials, external dependencies, and logs

Wrappers load the untracked `source/mcp/.env`; they never print its values.
Supported variables include `NVIDIA_API_KEY`, `KIT_EMBEDDER_BACKEND`,
`KIT_LOCAL_EMBEDDER_URL`, `KIT_RERANKER_BACKEND`, and
`KIT_LOCAL_RERANKER_URL`. Keep credentials out of scripts and Git.

The official services can depend on local or NVIDIA API embedding/reranking.
Kit Lab separately depends on the local Kit runtime bridge, normally
`127.0.0.1:8011`. MCP endpoint health, local dataset health, embedder reachability,
reranker configuration, cloud authentication, and the Kit bridge are reported
as distinct verification categories.

Usage analytics are disabled with both documented and implementation-observed
environment variables. No per-invocation logger is added. Bounded operational
logs live in `/home/ubuntu/kit-ai/logs/kit-usd-mcps`; likely usage/tool-input
lines are discarded, and each service retains at most three 5 MiB files.
Manager state lives in `/home/ubuntu/kit-ai/run/kit-usd-mcps`; cache and temporary
files live under `/home/ubuntu/kit-ai/cache/kit-usd-mcps` and
`/home/ubuntu/kit-ai/tmp/kit-usd-mcps`. The latest metadata-only verification
report is `verification-last.json` in the log directory.

## Tool policy and exact inventories

The four official servers select function tools explicitly in their launchers.
Source inspection classifies them as knowledge/data inspection functions without
filesystem, USD-stage, runtime-control, or destructive operations. NAT currently
emits descriptions and object input schemas but no explicit MCP annotations,
`_meta`, or server instructions for these upstream tools. The verifier records
that limitation; this management layer does not add a proxy merely to manufacture
metadata.

OmniUI MCP:

- `search_ui_code_examples`
- `search_ui_window_examples`
- `list_ui_classes`
- `list_ui_modules`
- `get_ui_class_detail`
- `get_ui_module_detail`
- `get_ui_method_detail`
- `get_ui_instructions`
- `get_ui_class_instructions`
- `get_ui_style_docs`

NVIDIA Kit MCP:

- `get_kit_instructions`
- `search_kit_extensions`
- `get_kit_extension_details`
- `get_kit_extension_dependencies`
- `get_kit_extension_apis`
- `get_kit_api_details`
- `search_kit_code_examples`
- `search_kit_test_examples`
- `search_kit_settings`
- `search_kit_app_templates`
- `get_kit_app_template_details`
- `search_kit_knowledge`

USD Code MCP:

- `search_usd_code_examples`
- `search_usd_knowledge`
- `list_usd_modules`
- `list_usd_classes`
- `get_usd_module_detail`
- `get_usd_class_detail`
- `get_usd_method_detail`

Isaac Sim MCP:

- `get_isaac_sim_instructions`
- `search_isaac_sim_extensions`
- `get_isaac_sim_extension_details`
- `search_isaac_sim_code_examples`
- `search_isaac_sim_settings`

KHL Kit Lab MCP:

- `kit_lab_policy`
- `kit_lab_status`
- `kit_runtime_info`
- `kit_stage_summary`
- `kit_prim_inspect`
- `kit_extensions_list`
- `kit_setting_get`
- `kit_viewport_info`
- `kit_prim_create`
- `kit_prim_remove`
- `kit_execute_python`
- `kit_reset_python_session`
- `kit_experiment_start`
- `kit_experiment_current`
- `kit_experiment_list`
- `kit_experiment_get`
- `kit_experiment_note`
- `kit_experiment_finish`

Kit Lab annotations, descriptions, `_meta`, and server instructions must match
`source/mcp/khl_kit_lab_mcp/MCP_POLICY.md` and its canonical Python policy.
Any future tool addition or removal requires synchronized updates to the source
launcher/config, this document, `mcp-services-user-local.json`, and
`tests/test_mcp_user_local.py`; the synchronization test fails otherwise.

## Status and troubleshooting

- `HEALTHY`: manager-owned process group is alive and MCP initialization plus
  exact `tools/list` metadata checks pass.
- `PROCESS_ALIVE_ENDPOINT_UNHEALTHY`: inspect the bounded service log; restart
  explicitly after correcting configuration or dependencies.
- `MISSING_STOPPED`: run `start` after setup.
- `STALE_MANAGER_STATE`: the recorded process is gone or ownership validation
  failed. A valid dead-PID record is removed on the next start/stop; unsafe or
  malformed state must be inspected manually.
- `PORT_OCCUPIED_UNOWNED`: another process owns the loopback port. The manager
  intentionally sends no signal; stop that process through its own owner.
- `EXTERNAL_DEPENDENCY_UNAVAILABLE`: the MCP endpoint is healthy but the Kit
  bridge or another classified backend is unavailable. Restore that dependency
  without restarting unrelated services.

Stop sends `TERM` only to a token-validated manager-owned process group, waits
15 seconds by default, then revalidates ownership before `KILL`. Startup waits
90 seconds by default. If a later start fails, only services newly started by
that same invocation are rolled back; previously healthy services are preserved.
