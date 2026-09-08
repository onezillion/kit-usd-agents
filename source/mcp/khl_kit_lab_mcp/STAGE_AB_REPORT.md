# Kit Lab Stage A+B report — 2026-09-08

Stage B is implemented and both MCP 0.4.0 and bridge API 0.3.0 are now active.
The initial A+B pass left bridge activation pending; the user-authorized follow-up
verified the new live summary and route removal. A user-confirmed runtime change
occurred during recovery, so this was not verified as an in-place reload of the
original Kit process. See the activation follow-up below.

## Scope and starting state

The checkout began clean at `4346747c0eb75b90bd06577a83663a87a32de308`.
No applicable AGENTS.md was found in the repository or its ancestor directories.
One Codex agent performed the work. The user confirmed that Kit Lab scope includes
its bridge extension and Kit Lab-specific shared registry/docs/allowlist changes.
No model topology or official NVIDIA MCP implementation/inventory was changed.

Inspected the registry, setup and manager, central verifier, policy, bridge,
Python executor, experiment store, tests, VS Code aliases and agent allowlists.
The only nonhistorical consumers of the removed tools found in this checkout were
their server/policy, live verifier, docs, registry, reviewer allowlist, and tests.
Creation/removal were implemented by server-generated Python, not dedicated
bridge routes. Prim inspection and setting reads had dedicated bridge routes.
No compatibility consumer requiring preservation of these shortcuts was found.

## Implemented behavior

MCP package/source version is `0.4.0`, bridge API/extension version on disk is
`0.3.0`, and policy version is `2.0`, fingerprint `07cefacaba54a3ca`.
The breaking removals advance the pre-1.0 minor versions. No installation or
dependency refresh was needed: the existing launcher imports this checkout.

Retained exactly:

1. `kit_lab_policy`
2. `kit_lab_status`
3. `kit_runtime_info`
4. `kit_stage_summary`
5. `kit_extensions_list`
6. `kit_viewport_info`
7. `kit_execute_python`
8. `kit_reset_python_session`
9. `kit_experiment_start`
10. `kit_experiment_current`
11. `kit_experiment_list`
12. `kit_experiment_get`
13. `kit_experiment_note`
14. `kit_experiment_finish`

Removed `kit_prim_create`, `kit_prim_remove`, `kit_prim_inspect`, and
`kit_setting_get`; deleted unused `live_stage.py` and its server dispatch helper.
Removed bridge `/prim/inspect` and `/settings/get` routes, models, implementations,
and capability entries. Generic Python and `/khl/ai/*` compatibility remain.

The old stage summary called `list(stage.Traverse())` unconditionally. The new
default skips traversal and returns `statistics_computed=false`, `prim_count=null`,
and `type_counts=null`. `include_statistics=true` explicitly requests a complete
default-predicate `Usd.Stage.Traverse()` count; it may be expensive. It accumulates
counts without retaining every prim. Computed empty and unavailable stages are
distinct. Root-child output is limited to 256 paths with explicit truncation.
Basic layer/metadata queries retain their USD-dependent costs.

MCP sends a typed POST summary request. Old bridges reject it rather than silently
running their expensive GET implementation. GET remains in bridge source with the
same optional boolean. Existing calls with no MCP arguments remain valid.

Policy now directs development and debugging toward reusable installed-version
Kit/USD Python, including scene/attribute/settings investigation. Python remains
elevated execution even for inspection. Existing task/session authorization is
honored without repeated prompts, within its scope. Persistence, package injection,
event-loop and policy-bypass restrictions remain. The reviewer remains read-only
and cannot execute Python. Knowledge and live-runtime aliases remain distinct.

The executor and experiment store implementations were preserved. Namespace,
top-level await, last-expression results, stdout/stderr/traceback, source recording,
and historical record formats remain. Namespace reset does not restart Kit,
cancel tasks, unload modules, or guarantee full cleanup. Client timeout is not
evidence that in-Kit execution stopped.

The standalone verifier now defaults to read-only calls. `--full` explicitly opts
into experiment writes and `2 + 2`; central `--full-kit-lab` passes that flag.
No full/mutating live verifier was run in this task. Historical evidence validation
accepts retired event names and still checks result/source references.

## Files changed

- `source/mcp/khl_kit_lab_mcp/`: server, client version string, package version,
  policy, policy manual, README, verifier, policy tests; new bridge/verifier tests
  and this report; deleted `src/khl_kit_lab_mcp/live_stage.py`.
- `source/extensions/omni.khl.kit_lab/`: `service.py`, extension version, README.
- `mcp-services-user-local.json`, `MCP_USER_LOCAL.md`, `tests/test_mcp_user_local.py`.
- `manage-mcps-user-local.sh`: only the Kit Lab full-verifier notice/flag.
- `.github/agents/glm-reviewer.agent.md` and `kimi-manager.agent.md`: Kit Lab
  allowlist/policy wording only. Models, delegation topology, and other permissions
  remain unchanged. `.vscode/mcp.json` remains unchanged.

## Tests and live evidence

- `source/mcp/khl_kit_lab_mcp/test-user-local.sh`: 36 tests passed.
- `python3 -m unittest discover -s tests -v`: 10 tests passed.
- `git diff --check` and shell syntax checks passed.
- Isolated tests execute actual bridge Python with stub Kit/USD/router objects.
  They prove no default traversal, opt-in statistics, empty/unavailable distinction,
  root-child limits, exact routes/capabilities, persistent Python/await/output/error
  compatibility, legacy namespace sharing, and that reset does not cancel tasks.
  They do not replace installed-Kit activation checks.
- In-process MCP tests verify the exact 14 tools, metadata/policy/schema, typed
  summary requests, Python exception/source recording, and retrieval of historical
  events naming all four removed tools after store reconstruction.
- `./manage-mcps-user-local.sh restart kit-lab`: restarted only the owned MCP
  service; its new initialized server version is `0.4.0`.
- `./manage-mcps-user-local.sh verify`: all five endpoints passed exact inventories,
  loopback listeners, metadata requirements, and default read-only calls. Official
  inventories remain 10/12/7/5; Kit Lab is 14. Official registry entries are identical
  to HEAD. Existing upstream NAT metadata limitations remain unchanged.
- Metadata report: `/home/ubuntu/kit-ai/logs/kit-usd-mcps/verification-last.json`.
- Standalone default verifier passed the 14-tool metadata checks, then failed on
  summary with HTTP 405, confirming the old bridge has no POST summary route.
- Live read-only Python inspected loaded APIs/routes and native log association;
  it produced captured results successfully without setting namespace variables.
- After MCP restart, existing finished experiment
  `20260907T115342368320Z-phase-3-mcp-policy-verification-3f7628ed` remained retrievable
  with 12 events, including historical create/remove/inspect events.
- Final bridge status remained responsive, API `0.2.0`, `busy=false`, and an empty
  lab namespace. No active experiment existed at initial inspection.

Sandbox restrictions blocked `/proc` ownership checks and loopback connections.
The affected manager/verifier commands succeeded after normal explicit escalation;
no guard modification or bypass was used.

## Loaded versus disk state

| Surface | On disk | Loaded/live |
|---|---|---|
| Kit Lab MCP | 0.4.0, 14 tools | 0.4.0, 14 tools, verified |
| Kit Lab bridge | API 0.3.0, removed routes, lightweight summary | API 0.3.0; live summary and HTTP routes verified in follow-up |
| Python executor | preserved | read-only inspection worked |
| Experiment store | preserved | historical retrieval worked after MCP restart |

The initial A+B pass did not reload extensions or restart Kit. The user subsequently
authorized reloading only `omni.khl.kit_lab`; the follow-up is detailed below. Codex
did not issue a Kit stop/restart, scene mutation, profiling capture, launcher edit,
pod/node change, package install, commit, or push.
The old `/home/ubuntu/kit-ai/venvs/nvidia-kit-mcp` environment is present and untouched.

## Launcher and log findings for C+D

Inspected `/home/ubuntu/kit-sdk-110.1.3/nchc.khl.editor.full.sh` read-only. It resolves
SCRIPT_DIR using `dirname "${BASH_SOURCE[0]}"`, does not change cwd, and references
Kit and the app relative to SCRIPT_DIR. Invoking its absolute path supports the
repository as launch cwd by script inspection; this was not launch-tested.
The running Kit PID observed was `4017332`, cwd `/home/ubuntu/kit-sdk-110.1.3`.

Default `${EXEC:-exec}` replaces the shell and runs Kit in the foreground. An EXEC
override can change this behavior and needs accounting in later ownership design.
The script inherits stdout/stderr without redirection. Existing `$EXTRA_FLAGS` is
unquoted, so shell word splitting and pathname expansion apply; `"$@"` preserves
individual argument boundaries. New options should use an argument array through
`"$@"`, not eval or an arbitrary shell command. Do not rewrite the launcher.

Installed Carbonite logging source uses `/log/file`. Reading this setting in the
actual Kit process identified:
`/home/ubuntu/.nvidia-omniverse/logs/Kit/omni.app.editor.full/0.0/kit_20260908_023527.log`.
This associates the path with the responding process, not with newest mtime.
During the initial A+B pass, no native log contents were read. During the reload
follow-up, the user expressly authorized reading this one named log for session
reload diagnosis; relevant extension-resolution errors were inspected. This did not
grant general access to the log root. Future discovery should return paths plus process/
launch association; ordinary terminal reads remain subject to session consent and
harness permissions. Capture startup stdout/stderr and cross-check native log path
when ready. Before readiness, association remains uncertain unless the native
startup output or approved launch configuration identifies the file.

Concrete next C+D task: implement repository-local Kit lifecycle setup/supervision
and lightweight log-path discovery. Record the user-approved absolute launcher,
repository cwd where supported, structured arguments, log roots and timeouts outside
Git. Preserve the official MCP environment arrangement; add no new environment file.
Implement owned process identity, status, start/stop/restart and readiness polling
with a 180-second default, task override, and progress updates. On readiness expiry,
report alive-but-not-ready and preserve the process. Choose a separate shutdown
timeout during C+D. Setup validates permissions; it cannot grant OS/harness access.
No generic log reader, resource sampler, profiler, screenshots or new test-job API
belongs in C+D.

## Profiler findings for E

Live extension metadata and installed source confirm:

- `omni.activity.profiler` 1.0.4 is enabled. Its installed usage docs describe
  `acquire_activity_profiler`, `enable_capture_mask`, `disable_capture_mask`, and
  `release_activity_profiler`; this converts profiler events to activity events.
- `omni.kit.profiler.window` 2.5.0 is enabled. Its installed implementation uses
  Carbonite capture masks and `/plugins/carb.profiler-cpu.plugin/filePath` and
  `/plugins/carb.profiler-cpu.plugin/saveProfile`. It includes cProfile support.
- `omni.kit.profiler.tracy` 1.2.1 is installed but disabled; it was not enabled.
- Loaded `carb.profiler.IProfiler` has `get_capture_mask`, `set_capture_mask`,
  `is_python_profiling_enabled`, and `set_python_profiling_enabled`.
  `IProfileMonitor` has `get_last_profile_events` and `mark_frame_end`.
  Presence was inspected; control/capture methods were not invoked.

These IProfiler methods are also documented in the
[Kit 110.1.3 IProfiler API](https://docs.omniverse.nvidia.com/kit/docs/kit-manual/110.1.3/carb.profiler/carb.profiler.IProfiler.html).
The [NVIDIA profiler window guide](https://docs.omniverse.nvidia.com/extensions/latest/ext_profiler.html)
describes CPU/Python capture and trace workflows. Local 2.5.0 source was authoritative
for implementation details; its versioned extension documentation page could not
be retrieved. `_toggle_capture` and `_stop_capturing` exist but are private UI
methods, not a confirmed stable public capture API. Stopping through that UI can
also trigger export/viewer behavior. A supported, bounded, headless capture lifecycle,
completion/file-flush guarantees, GPU metric availability, and restoration semantics
remain unverified. No profiler was started, software installed, or custom profiler built.

Other diagnostic limits observed: extension listing enumerates/sorts the installed
catalog; its shared serializer caps collections at 256 even when a larger limit is
requested, so pre-existing returned/truncated metadata can disagree beyond that size.
The serializer can materialize iterables before truncation. These pre-existing
diagnostic issues were left outside Stage B's focused summary revision.

## Skill follow-up and Stage F

Installed `/home/ubuntu/.agents/skills/omniverse-kit-dev-agent/SKILL.md` is present
and byte-identical to the canonical file under
`/home/ubuntu/Documents/khl-ai-skills/skills/omniverse-kit-dev-agent/`. It contains no
relative Markdown links to validate. Discovery is current, but wording is stale:
remove deterministic scene-tool preference, Python escape-hatch framing, the 0.3.0
create/remove inventory, and advice to expand scene wrappers. Replace these with
real Kit/USD development/debugging, retained context tools, lightweight summary
semantics, and accurate namespace-reset limitations. Keep authorization, persistence,
event-loop, knowledge/live-runtime, and reviewer restrictions. No skill file was edited
or synchronized in this task.

After C+D, then E, separately authorize Stage F to validate a reusable installed-
version Kit/USD source deliverable in live Kit, inspect its scene effects through
Python, and independently verify cleanup, responsiveness, source/results recording,
and retrieval after MCP restart. Do not infer standalone-source correctness from
MCP shortcut success or from the harmless expression check.


## Activation follow-up and extension-manager scope

The user authorized the Kit Lab extension reload and requested extension-manager
controls alongside profiler development in Stage E. No new MCP tools were added in
this follow-up; the active inventory remains exactly 14.

Installed `ExtensionManager` bindings provide `set_extension_enabled` (deferred)
and `set_extension_enabled_immediate` (boolean result), but no `reload_extension`.
The installed Python loader reloads extension modules after a disable/enable cycle.
Before attempting the cycle, Kit Lab had no enabled direct dependents, an empty
lab namespace, and no active experiment.

The first attempt disabled the current exact ID `omni.khl.kit_lab-0.2.0`, then
incorrectly reused that ID to enable it. On disable, Kit discovered disk version
0.3.0 and unregistered 0.2.0; exact-version re-enable and its retry therefore failed.
This was an operator error in the reload script, not an executor or summary failure.
The named native log confirmed the dependency-resolution error. Kit Lab routes
returned HTTP 404 while the adapter remained available.

Recovery used Kit's existing loopback debugpy listener on port 3000, which had no
attached client. A task-owned script in `/tmp/kit_lab_reload_recover.py` attached,
queued `set_extension_enabled('omni.khl.kit_lab', True)` by unversioned name, and
explicitly disconnected without terminating or requesting suspension of Kit. The
DAP initialize/configuration/attach/evaluate/disconnect requests all succeeded.
Subsequent inspection confirmed `debugpy.is_client_connected()` was false.

During this interval the responding Kit PID changed from 4017332 to 766258 and the
stage identity changed from 9223001 / World0 to 9223003 / World1. The user confirmed
restarting Kit or changing the stage. Codex issued no Kit restart command. Because
of this external runtime change, the recovered process cannot establish preservation
of the original stage or prove that the first process recovered in place. The
reload task's outcome marker was absent in the new process.

Verified in the responding new process:

- Enabled ID `omni.khl.kit_lab-0.3.0`, bridge API 0.3.0; 416 enabled extensions.
- Default summary succeeds with `statistics_computed=false`, null counts, and
  bounded root-child output; no full statistics request was made.
- Live HTTP OpenAPI inventory has GET and POST summary, no `/prim/inspect` or
  `/settings/get`, and all three legacy `/khl/ai/*` compatibility routes.
- `verify-user-local.sh`: 14-tool metadata and `KIT_LAB_READ_ONLY_OK` passed.
- `./manage-mcps-user-local.sh verify kit-lab`: 14 exact tools, metadata and live
  bridge status passed. The manager's latest report now covers this selected service;
  the initial all-five PASS results above remain the earlier evidence.
- Python inspection succeeds, the debugger is detached, and no experiment is active.

Stage E now includes extension-management tools as well as profiler tools. Plan
narrow enable, disable, and reload controls, with installed-version/API grounding.
Candidate read-only debugging functions are resolved ID/version/path and enable
state, declared dependencies/dependents, reloadability, and supported startup/error
status. Decide final tool names and contracts during that stage.

Required design checks from this attempt: resolve the currently available ID after
version changes; preflight dependency impact and do not silently disable unrelated
extensions; report queued versus completed state and verify readiness afterward;
use a recovery path independent of the extension being disabled, especially for
Kit Lab and its HTTP dependencies; preserve authorized session scope and the
read-only reviewer boundary. Do not imply that reload unloads every module, cancels
every task, or cleans stage state. Keep installs, registry downloads, persistent
configuration changes and broader process operations outside an enable/disable
request unless separately authorized. Keep implementation in Kit Lab and synchronize
all tool/policy/bridge/inventory/tests surfaces when these tools are added.
