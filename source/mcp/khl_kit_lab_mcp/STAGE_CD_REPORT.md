# Kit Lab Stage C+D completion — 2026-09-08

Stage C+D is implemented and activated. Kit Lab MCP is **0.5.0**, bridge/API is
**0.4.0**, and the exact MCP inventory is **20 tools**: all 14 Stage A+B tools plus
six lifecycle/log-path tools. Policy is **2.1**, fingerprint `09469759cd1b6289`.
The completed A+B report and behavior remain intact. No commit or push was made.

## Exact repository files changed in C+D

Paths below are relative to `/home/ubuntu/Documents/kit-usd-agents` and describe
C+D relative to the existing, uncommitted A+B checkpoint, rather than combining
both stages into a misleading Git diff.

Modified:

- `.github/agents/glm-reviewer.agent.md`
- `.github/agents/kimi-manager.agent.md`
- `MCP_USER_LOCAL.md`
- `mcp-services-user-local.json`
- `source/extensions/omni.khl.kit_lab/README.md`
- `source/extensions/omni.khl.kit_lab/config/extension.toml`
- `source/extensions/omni.khl.kit_lab/omni/khl/kit_lab/service.py`
- `source/mcp/khl_kit_lab_mcp/MCP_POLICY.md`
- `source/mcp/khl_kit_lab_mcp/README.md`
- `source/mcp/khl_kit_lab_mcp/pyproject.toml`
- `source/mcp/khl_kit_lab_mcp/src/khl_kit_lab_mcp/__init__.py`
- `source/mcp/khl_kit_lab_mcp/src/khl_kit_lab_mcp/client.py`
- `source/mcp/khl_kit_lab_mcp/src/khl_kit_lab_mcp/policy.py`
- `source/mcp/khl_kit_lab_mcp/src/khl_kit_lab_mcp/server.py`
- `source/mcp/khl_kit_lab_mcp/tests/test_policy.py`
- `source/mcp/khl_kit_lab_mcp/verify.py`
- `tests/test_mcp_user_local.py`
- `source/mcp/khl_kit_lab_mcp/tests/test_bridge.py`

Added:

- `source/mcp/khl_kit_lab_mcp/lifecycle-user-local.sh`
- `source/mcp/khl_kit_lab_mcp/src/khl_kit_lab_mcp/lifecycle.py`
- `source/mcp/khl_kit_lab_mcp/src/khl_kit_lab_mcp/lifecycle_cli.py`
- `source/mcp/khl_kit_lab_mcp/tests/test_lifecycle.py`
- `source/mcp/khl_kit_lab_mcp/STAGE_CD_REPORT.md`

`manage-mcps-user-local.sh`, the deleted `live_stage.py`, `tests/test_verifier.py`,
and `STAGE_AB_REPORT.md` already had their A+B state and were not changed by C+D.
Only Kit Lab-specific entries in shared registry/docs/agent files changed. The four
NVIDIA service entries are identical to the checkpoint, including their formatting;
their source, launchers, environments and running processes were preserved.

The user independently added `export KHL_KIT_ID="nchc-kit-dev-main"` to
`/home/ubuntu/kit-sdk-110.1.3/nchc.khl.editor.full.sh`. C+D preserved that launcher.

## Architecture and configuration

`lifecycle.py` is the shared Linux implementation for the MCP and repository operator
CLI. `lifecycle_cli.py` provides setup/config/status/start/stop/restart/log-paths;
`lifecycle-user-local.sh` uses the existing Kit Lab virtualenv without installation.
The bridge adds only a small read-only identity/readiness/log-path endpoint.

Setup successfully wrote `/home/ubuntu/kit-ai/lab/lifecycle.json`, mode 0600,
outside Git. It validates the configured launcher and Kit executable, execute/read
permissions, cwd, fixed identity, argv array, private configuration ownership, loopback
bridge origin, timeout bounds, and current process-inspection permissions. The config
is loaded afresh for each MCP lifecycle call. Setup cannot grant host permissions.

Configured defaults:

| Item | Value |
|---|---|
| Stable identity | `nchc-kit-dev-main` |
| Launcher | `/home/ubuntu/kit-sdk-110.1.3/nchc.khl.editor.full.sh` |
| Kit executable | `/home/ubuntu/kit-sdk-110.1.3/kit` |
| cwd | `/home/ubuntu/Documents/kit-usd-agents` |
| Additional arguments | Empty JSON array |
| Bridge | `http://127.0.0.1:8011` |
| Native log root | `/home/ubuntu/.nvidia-omniverse/logs` |
| Launch output root | `/home/ubuntu/kit-ai/lab/launches` |
| Readiness / graceful shutdown / forced shutdown wait | 180 / 30 / 5 seconds |
| HTTP probe / polling interval | 2 / 1 seconds |

Launch uses `Popen([launcher, *arguments], cwd=..., env=...)` and a new session,
with private per-launch stdout/stderr capture. It injects the stable identity and
removes MCP Python-path/virtualenv settings and `NVIDIA_API_KEY`. Existing launcher
`$EXTRA_FLAGS` behavior remains unchanged. There is no eval, new env file, launcher
rewrite, package installation, or persistent PID/ownership registry. A private advisory
lock serializes independent CLI/MCP lifecycle mutations, without storing process identity.
Child handles are retained only for reaping/launcher-exit diagnostics, never identification.

## Tool and API contracts

Every new MCP tool accepts `kit_id`, default and only permitted value
`nchc-kit-dev-main`. Callers do not provide PID, arbitrary executable, path, environment,
or port. Setup remains an operator CLI step.

| Tool | Other arguments | Main behavior / class |
|---|---|---|
| `kit_lifecycle_config` | None | Approved config and service inspection diagnostics; READ_ONLY |
| `kit_status` | None | Fresh OS discovery and matching bridge readiness; READ_ONLY |
| `kit_start` | `readiness_timeout: number|null = null` | Approved launch or already-running result; RUNTIME_CONTROL |
| `kit_stop` | `force: boolean = false` | Freshly identify, validate and gracefully stop; RUNTIME_CONTROL |
| `kit_restart` | `force=false`, `readiness_timeout=null` | Confirm shutdown, relaunch same identity, wait; RUNTIME_CONTROL |
| `kit_log_paths` | None | Associated native/output paths, never contents; READ_ONLY |

Readiness overrides are (0,3600] seconds. Stop/restart are annotated destructive because
unsaved state may be lost. Existing task/session lifecycle authorization is honored;
forced termination requires explicit authorization. MCP progress remains monotonic across
stop/launch/readiness phases; messages identify the phase and its deadline. The context
parameter is injected by the SDK and excluded from public schemas.

`GET /khl/lab/runtime/identity` returns the usual `ok`/`result` envelope with
`kit_id`, `pid`, `start_ticks`, `api_version`, `ready`, and `native_log_path`.
Readiness uses installed `IApp.is_app_ready()`; the path comes from Carbonite `/log/file`.
The endpoint does not acquire the Python execution lock. `/khl/lab/status` adds PID/Kit ID
and advertises the identity capability. Existing A+B and `/khl/ai/*` routes are preserved.
No in-Kit launch/signal route exists. Lifecycle calls do not append experiment events;
the original runtime and experiment recording behavior is unchanged.

## Identity, discovery and timeout semantics

Candidate selection uses the exact configured executable through `/proc/<pid>/exe`.
Identification checks the exact NUL-delimited `KHL_KIT_ID` entry in its environment.
The full environment is neither serialized nor logged. PID, UID, executable and kernel
start ticks are supporting metadata. Environment discovery works without Kit HTTP.

Zero matches reports STOPPED; one is identified; multiple report AMBIGUOUS and prevent
control. Unreadable candidates report INSPECTION_DENIED, not STOPPED. Untagged Kit is
never signalled and prevents a new start. Bridge identity must match the OS-selected
Kit ID, PID and start ticks; HTTP alone never identifies the managed process.

READY requires a matching ready bridge. Before the configured startup window expires,
unavailable/not-ready bridge probes report STARTING; subsequently they report
UNRESPONSIVE. A bridge identity mismatch reports UNRESPONSIVE immediately. Process
replacement during probes or readiness is explicit PROCESS_CHANGED. Every new operation
rediscovers, so manual restarts and either MCP/Kit startup order work without stale PIDs.

Stop opens a pidfd and revalidates unique executable/environment/start-time identity and
UID before signalling. SIGTERM is first, with a separate default 30-second deadline.
Without explicit force, timeout preserves the process. `force=true` permits SIGKILL
with another 5-second wait and another identity check. Restart never launches after
failed shutdown or unexpected replacement. Pidfds prevent signalling a reused PID.

Start rechecks after HTTP preflight to catch concurrent manual launches. Readiness
timeout preserves an alive process. A launcher that exits before Kit is identified
reports launch failure/STOPPED; an alive launcher without identifiable Kit reports
STARTING with `KIT_NOT_IDENTIFIED_YET` and timeout. It is not falsely adopted as Kit.
Deadlines may overshoot by the final bounded probe/OS inspection. Cancellation does not
undo a completed launch or signal; follow up with fresh status. Resource acquisition
is synchronous so cancelled tasks do not leave background launch/pidfd acquisition
running after releasing the mutation lock.

## Native log discovery

Responsive discovery prefers `/log/file` from the matching bridge, cross-checked against
open descriptors of that exact OS process. When HTTP is unavailable, a single native
`kit_*.log` descriptor under configured roots can be selected. Multiple candidates remain
ambiguous. Stdout/stderr descriptor links under the capture root identify launch output.
Resolved paths must remain within configured roots. No contents, newest-mtime selection,
or generic log reader are involved. Log content access still needs separate task/session
consent and ordinary host permissions. C+D did not read native or launch-output contents.

## Tests and live evidence

- `source/mcp/khl_kit_lab_mcp/test-user-local.sh`: **60 tests passed** on the final code.
- `python3 -m unittest discover -s tests -v`: **10 tests passed**.
- `git diff --check`: passed.
- `verify-user-local.sh --lifecycle`: exact 20-tool names, schemas, annotations,
  descriptions, instructions and policy metadata passed; A+B lightweight summary and
  experiment-current checks passed; READY identity/native log association passed.
- `./manage-mcps-user-local.sh verify`: all five services passed transport, exact
  inventories and read-only calls (10 / 12 / 7 / 5 / 20 tools).

Tests cover actual isolated Linux sleep executables with environment identity and pidfds,
plus stub bridge/Kit modules. They cover zero/one/multiple matches, unrelated processes,
permission failure, ID injection, manual wrapper restart, both startup orders, manual
launch during preflight, PID reuse/replacement, failed launcher, frozen-HTTP simulation,
readiness preservation, graceful shutdown, explicit force, restart, serialized mutations,
progress, log association/root containment, bridge identity, and all A+B regressions.
No real Kit was frozen or forcibly terminated.

Live sequence:

1. Initial Kit PID **766258** lacked the identity because it predated the launcher edit.
   Its environment was readable outside the agent sandbox; it was not adopted or killed
   through the new identity-only tools.
2. The user manually restarted the edited existing launcher while the pre-existing MCP
   remained running. Kit PID **1748890**, start ticks **216369247**, carried
   `nchc-kit-dev-main`, matched bridge **0.4.0**, and reported READY. Configuration setup
   and actual MCP lifecycle calls ran as UID **1000**, with no denied candidates.
3. `kit_start` against that live process returned `started=false` with the same process.
   No experiment was active. A local client stub made only the observer's HTTP fail:
   status reported UNRESPONSIVE for PID 1748890 and open-descriptor fallback selected
   its correct native log. The real bridge remained responsive throughout.
4. With MCP PID **1767343** staying up, the repository CLI performed one graceful
   restart. SIGTERM completed; `forced=false`. It launched PID **1770712**, start ticks
   **216411708**, with the same stable identity. READY arrived in about **10 seconds**.
   This launch was outside MCP; the same MCP rediscovered it independently.
5. The native log changed from `kit_20260908_133557.log` to
   `kit_20260908_134302.log`. Both were associated by process identity and open descriptors,
   not file timestamps. Final read-only verification passed after loading the final MCP
   progress fix; Kit PID 1770712 stayed running during that MCP restart.

Current native log:
`/home/ubuntu/.nvidia-omniverse/logs/Kit/omni.app.editor.full/0.0/kit_20260908_134302.log`

Captured output from the CLI-managed launch:
`/home/ubuntu/kit-ai/lab/launches/nchc-kit-dev-main-20260908T134302146952Z-mjbtgf3p.log`

Final association: `matching_bridge_identity`, `open_descriptor_confirmed=true`,
`contents_read=false`. The four official MCP listener PIDs remained 1688398, 1688677,
1689034, and 1689331 across the live Kit restart.

## Permission limits and Stage E decisions

The restricted agent sandbox denied inspection of real unrelated-process environments
and stalled asyncio thread wakeups during fixture tests. Approved escalated runs completed
the same tests and inspection. The actual MCP service, UID 1000 outside that sandbox,
proved it can inspect real Kit through `kit_lifecycle_config`, `kit_status`, and
`kit_log_paths`; no alternate identity model or permission bypass was introduced.
Machine configuration outside the writable repository required an approved setup command.
Future OS/container account changes can revoke inspection; failures remain explicit.

The fixed environment label is an operational identity, not authentication against a
malicious same-account process. Manual launches do not participate in the CLI/MCP lock;
concurrent manual launches can produce explicit ambiguity. The approved launcher is
expected to remain a foreground Kit launcher; background/daemonizing replacements need
separate evaluation. Captured output currently has no automatic retention/rotation policy.
Cancellation or readiness timeout requires status inspection rather than assuming exit.

Stage E may add the requested extension-manager inspection/enable/disable/reload and
profiler workflows. Decide protected extensions, especially self-disable/reload of the
8011 bridge, and recovery behavior before exposing those controls. C+D adds none of
those tools, resource samplers, screenshots, test jobs, or log-content readers. There is
no outstanding C+D permission or activation blocker.
