# Stage E — Installed Extensions and Built-in Profiler

Verified 2026-09-09 through 2026-09-10.

## Summary

Stage E is **PARTIAL**.

- E1 safe local extension control: **PASS**.
- Standalone Kit Lab self-disable: **FORBIDDEN**.
- Standalone Kit Lab self-reload: **RESTRICTED** by enforced preflight because no
  independent survivor owns re-enable.
- E2 bounded Carbonite CPU in-memory capture: **INCONCLUSIVE (scoped)**.

The current bounded bridge protocol did not demonstrate successful association of
the operation-specific marker with captured native profiler events. This does not
show that Carbonite profiling is broken, that Kit 110.1.3 lacks profiling, that
profiler capture is generally unsupported, or that infrastructure blocked the test.
The bridge can start and stop its bounded operation and restore profiler state, but
useful event association has not been demonstrated.

## Baseline

- Branch: `main`.
- Pushed/local/source checkpoint:
  `f05a3d0da5ee17b581929b45e5ffbf12c1de7273` (`origin/main` and local HEAD
  matched before implementation).
- Initial working tree: clean. All changes listed below were left uncommitted for
  operator inspection.
- Kit: `110.1.3+production.349534.56c4c3a1.gl`.
- Initial bridge/API: 0.4.0; final bridge/API: 0.5.0.
- Initial MCP package: 0.5.0; final MCP package: 0.6.0 using MCP SDK 2.1.1.
- Initial policy: 2.1 / `09469759cd1b6289`; final policy: 2.2 /
  `3868204d36842ed9`.
- Initial exact Kit Lab inventory: 20 tools; final exact inventory: 27 tools.
- Stable Kit identity: `KHL_KIT_ID=nchc-kit-dev-main`.
- Initial observed process: PID 1770712, start ticks 216411708.
- Final observed process: PID 1581924, start ticks 225386974, one identified
  candidate, READY, bridge generation `372ebf477f8644a9900346f49502a627`.
- The four NVIDIA knowledge services retained exact tool counts 10/12/7/5. All five
  `.vscode/mcp.json` aliases were preserved.

The initial anonymous default stage contained only the normal generated roots. Its
root and session layers were already dirty as part of that generated default scene;
no user-authored Stage E content was identified.

## Architecture and API evidence

### E1 installed-extension control

The following MCP tools and bridge operations were added:

- `kit_extension_inspect`
- `kit_extension_enable`
- `kit_extension_disable`
- `kit_extension_reload`

Inputs accept canonical unversioned extension identities with an optional tag.
Version-qualified IDs are rejected. Inspection uses Kit's complete internal local
catalog, `solve_extensions(..., add_enabled=True)`, resolved dependency state, active
reverse dependents, reloadability/toggleability metadata, exact IDs, and fresh enabled
state. Empty native dependency dictionaries are treated as complete leaf state; an
unreadable config or referenced missing catalog node makes the graph incomplete.

Only already-installed local solutions are eligible. Registry downloads, install
hooks, extension search-path changes, cascaded dependent disable, Kit launch, restart,
and kill are outside this surface. Mutations revalidate under the bridge control lock
and the MCP lifecycle mutation lock, record phases and exact before/after state, and
verify startup failure/state after Kit updates.

Rollback restores only operation-owned immediate deltas. It rebuilds a fresh graph,
uses dependent-first safe ordering, preserves concurrent external changes, and reports
conflicts instead of overwriting them. Reload disables the exact old version, verifies
the disabled state, rediscovers the unversioned installed solution, and can re-enable
a different compatible version. Changed-version behavior was verified with isolated
fixtures rather than by editing installed SDK extensions.

The active Kit Lab extension and its dependency closure are protected. Standalone
self-disable returns `SELF_DISABLE_FORBIDDEN`. Standalone self-reload returns
`SELF_RELOAD_UNSUPPORTED` before side effects because the control channel has no
independent survivor. Live refusal preserved exact ID `omni.khl.kit_lab-0.5.0`, its
enabled state, and the complete enabled-state snapshot.

### E2 bounded profiler surface

The following MCP tools and bridge operations were added:

- `kit_profiler_status`
- `kit_profiler_capture`
- `kit_profiler_capture_status`

Installed API discovery established:

- `omni.activity.profiler-1.0.4`: installed and baseline/final enabled.
- `omni.kit.profiler.window-2.5.0`: installed and baseline/final enabled.
- `omni.kit.profiler.tracy-1.2.1`: installed and baseline/final disabled.
- Public `carb.profiler.IProfiler`: mask, Python-instrumentation flag, `begin`/`end`,
  and thread registration.
- Public `carb.profiler.IProfileMonitor`: completed event snapshots and
  `mark_frame_end()`.
- No verified public native file export/flush contract suitable for this bounded
  surface.

No profiler extension was installed, enabled, or disabled for Stage E. The bridge
explicitly binds both control and monitoring to `carb.profiler-cpu.plugin` because
unnamed interface acquisition selected different profiler and monitor providers on
this runtime.

Capture inputs are typed, finite, and limited to 10 seconds. The implementation
refuses a pre-existing nonzero CPU mask, active Python instrumentation, or native
`saveProfile`. It owns the asynchronous operation after client disconnect, serializes
against extension/lifecycle mutation, snapshots mask/Python and relevant setting
presence/value, emits a unique capture marker, explicitly closes the native frame,
and restores exact prior state without overwriting concurrent changes.

Native event parsing is limited to 128 advertised threads, 20,000 visited events, and
512 returned events. The global visit budget is distributed across threads, samples
both ends of oversized root lists, and retains a found marker even when output is full.
Success requires the generated marker; an empty acknowledgment, setting change, or
nonzero output file is insufficient.

The external MCP persists bounded JSON evidence beneath the fixed private root
`/home/ubuntu/kit-ai/lab/profiles`. Callers cannot choose paths or filenames. Capture
IDs are UUIDs; directory-fd operations, `O_NOFOLLOW`, containment, owner/mode/size
checks, 0700 directories, 0600 files, and no-overwrite creation protect evidence.
Native profiler file export, GPU capture, Tracy, external viewers, and standalone
cProfile output are unsupported by this surface.

Because no live capture produced an associated marker, status reports CPU events,
bounded capture, Python instrumentation, and headless operation as exactly
`available_requires_capture_association`, never as bare `supported` or `available`.

## Live verification

### E1 result

The verified safe target was the already-installed local extension
`omni.kit.viewport.menubar.framerate-1.0.10`. Its initial state was disabled, its
dependency graph and protection analysis were complete, it was reloadable/toggleable,
it had no active reverse dependents, and all required dependencies were already
enabled.

`verify-user-local.sh --stage-e-extension
omni.kit.viewport.menubar.framerate` performed enable -> reload -> disable and returned
`KIT_LAB_STAGE_E_EXTENSION_OK`. Fresh inspection proved the target returned to disabled
and the complete enabled-extension snapshot exactly matched the baseline.

### E2 result

Four bounded one-second CPU+Python operations ran under distinct Kit processes and
bridge generations. Each returned `CAPTURE_ASSOCIATION_FAILED`, persisted truthful
evidence, and restored exact state.

| Capture ID | PID | Result file bytes | Result |
|---|---:|---:|---|
| `82d8e6f6d16746d7862c6c5d0e51e2fc` | 1503790 | 2586 | marker absent; restoration complete |
| `25cf732adcaa4014bf7960f069cbbb77` | 1524012 | 2585 | marker absent; restoration complete |
| `6d03c49032fe46aba0ae846f2b4d43d3` | 1548674 | 2586 | marker absent; restoration complete |
| `3ee0a3f565da4299836f7a32cf1623bb` | 1568275 | 9966 | marker absent after cross-thread diagnostic; restoration complete |

The final diagnostic scanned 61 profiler threads and visited 1,241 events with no
thread-list truncation. No operation marker was found. Final and independently
observed state was:

- CPU capture mask 0;
- Python profiling false;
- native `saveProfile` false;
- `compressProfile` true;
- `filePath` absent;
- capture inactive with no owner;
- no active bridge capture task;
- profiler extension states unchanged.

This evidence makes E2 inconclusive for the bounded protocol. A possible interaction
between Kit's update cadence, `mark_frame_end()`, and completed-event snapshot timing is
an open hypothesis only. It was not investigated after closure was requested and must
not be treated as the established cause.

### Runtime and cleanup

Bridge activation required graceful Kit lifecycle restarts because filesystem watching
was disabled. The process transitions were expected source-activation boundaries:

- 1770712 -> 1480518;
- 1480518 -> 1503790;
- 1503790 -> 1524012;
- 1524012 -> 1548674;
- 1548674 -> 1568275;
- 1568275 -> 1581924.

Several starts briefly exposed two identity-tagged candidates before the launcher
naturally converged. The lifecycle controller reported ambiguity and did not select,
signal, or force either candidate. No SIGKILL, force mode, implicit failure fallback,
or unrelated process control was used.

Final read-only observations showed Kit READY and responsive with one matching process,
Kit Lab not busy, an empty persistent Python namespace, no capture owner/task, the E1
target disabled, and the anonymous default stage containing seven standard bounded root
children with no `/World/AgentSceneLab` root. The lightweight summary intentionally left
full prim counts null; it is not represented as a complete traversal.

## Review

### Review 1

An independent read-only reviewer audited the design before live mutation and was
recalled for material profiler corrections. It identified and caused fixes for:

- solver planning without the enabled app closure;
- incomplete graph handling for missing nodes;
- mutation interference ownership and rollback;
- incorrect profiler marker API use;
- pre-existing mask/Python capture ownership;
- output-path replacement/symlink races;
- missing persisted output metadata;
- insufficient exact-restoration verification;
- reload ownership if version rediscovery fails;
- native empty dictionary leaf semantics;
- mismatched unnamed profiler/monitor providers;
- main-thread-only event association.

Focused tests were added for each accepted finding. No reviewer performed source edits or
live mutation. No material recommendation was rejected; unsafe self-reload and unverified
native export/GPU/Tracy were restricted or deferred.

### Review 2

KHL Kimi Reviewer (Kimi Max) ran the delegated final evidence campaign without source,
experiment, extension, profiler, Python, or lifecycle mutation. It ran 83 package tests,
10 root tests, the five-service verifier, both read-only Kit Lab verifiers, inspected the
four retained capture artifacts, and checked final runtime state.

Kimi invoked KHL GLM Reviewer (GLM 5.2) once with read-only repository and the exact 15
Kit Lab READ_ONLY tools. GLM returned GO WITH CONDITIONS. Its material contribution was
to settle the taxonomy as scoped **INCONCLUSIVE**, reject stronger unsupported/broken or
external-infrastructure-blocked wording, and identify flush timing only as an unproven
future hypothesis. That finding was accepted. Review found no closure blocker.

Collaboration was useful with moderate overhead: Review 1 produced material source and
safety corrections; Review 2 improved claim precision without reopening implementation.

## Automated verification

Final delegated closure results before this report was added:

- `source/mcp/khl_kit_lab_mcp/test-user-local.sh`: 83/83 PASS.
- `python3 -m unittest discover -s tests -v`: 10/10 PASS.
- `./manage-mcps-user-local.sh verify`: all five services PASS; exact counts
  10/12/7/5/27.
- `source/mcp/khl_kit_lab_mcp/verify-user-local.sh`: PASS / 27 exact tools.
- `source/mcp/khl_kit_lab_mcp/verify-user-local.sh --lifecycle`: PASS / READY identity
  and log association.
- `git diff --check`: PASS.

Dangerous lifecycle ambiguity, timeout, force, cancellation, dependency interference,
capture cancellation, concurrent changes, output traversal/symlink/overwrite, and
changed-version cases were exercised with isolated fixtures rather than by destabilizing
the live runtime. Existing baseline Python, experiment, lightweight summary, viewport,
extensions list, lifecycle, identity, log association, and policy contracts remain under
regression coverage.

## Acceptance matrix

| # | Verdict | Evidence |
|---:|---|---|
| 1 | PASS | Pushed/local/source/runtime baseline and exact old 20-tool inventory were established with no initial drift. |
| 2 | PASS | All retained contracts passed package/root regression and deterministic read-only live verification; destructive paths are accurately labeled isolated. |
| 3 | PASS | Real local identity/version/state resolution uses the complete catalog/solver/graph and refuses uncertainty. |
| 4 | PASS | Ordinary enable/disable/reload passed live with exact restoration; isolated changed-version rediscovery passed. |
| 5 | PASS | Active dependents, protected control plane, incomplete graphs, non-reloadable targets, and general install/registry paths are refused; no E2 install exception was needed. |
| 6 | RESTRICTED | Standalone self-disable is forbidden and self-reload is deterministically refused before side effects because no survivor exists. |
| 7 | PASS | Extension/profiler failures never launched, restarted, or killed Kit. All lifecycle transitions were explicit source activation. |
| 8 | INCONCLUSIVE | Installed APIs were established and bounded operations ran, but no associated marker/useful event result was verified. |
| 9 | PASS | Fixed 10-second input limit, server-owned/shielded work, bounded cleanup, cancellation/timeout tests, and retained evidence prevent silent indefinite work. |
| 10 | INCONCLUSIVE | Output containment/fresh private JSON persistence passed, but useful native event association did not; native file export is unsupported. |
| 11 | PASS | Profiler/settings/extension states were restored and independently checked; no job capture/task/temp prim remains; no profiler extension was installed. |
| 12 | PASS | Native export, GPU, Tracy, external viewers, and standalone cProfile output are explicitly unsupported; Python instrumentation requires association. |
| 13 | PASS | Kit is responsive under stable logical identity; activation PID/generation transitions, empty namespace, default stage, and experiment preservation are documented. |
| 14 | PASS | Versions, policy/fingerprint, schemas/annotations, registry, docs, verifier, reviewer exposure, 27-tool inventory, and all five MCP entries agree. |
| 15 | PASS | Relevant tests/verifiers and scoped diff checks pass; no SDK/package/Nucleus/unrelated runtime change, commit, or push occurred. |

Full Stage E PASS is not claimed because criteria 8 and 10 lack useful associated native
event evidence.

## Files changed

Stage E source and metadata intended for operator review:

- `.github/agents/glm-reviewer.agent.md`
- `.github/agents/kimi-manager.agent.md`
- `.github/agents/kimi-reviewer.agent.md`
- `MCP_USER_LOCAL.md`
- `mcp-services-user-local.json`
- `source/extensions/omni.khl.kit_lab/README.md`
- `source/extensions/omni.khl.kit_lab/config/extension.toml`
- `source/extensions/omni.khl.kit_lab/omni/khl/kit_lab/extension.py`
- `source/extensions/omni.khl.kit_lab/omni/khl/kit_lab/service.py`
- `source/extensions/omni.khl.kit_lab/omni/khl/kit_lab/stage_e.py`
- `source/mcp/khl_kit_lab_mcp/MCP_POLICY.md`
- `source/mcp/khl_kit_lab_mcp/README.md`
- `source/mcp/khl_kit_lab_mcp/STAGE_E_REPORT.md`
- `source/mcp/khl_kit_lab_mcp/pyproject.toml`
- `source/mcp/khl_kit_lab_mcp/src/khl_kit_lab_mcp/__init__.py`
- `source/mcp/khl_kit_lab_mcp/src/khl_kit_lab_mcp/client.py`
- `source/mcp/khl_kit_lab_mcp/src/khl_kit_lab_mcp/policy.py`
- `source/mcp/khl_kit_lab_mcp/src/khl_kit_lab_mcp/profiles.py`
- `source/mcp/khl_kit_lab_mcp/src/khl_kit_lab_mcp/server.py`
- `source/mcp/khl_kit_lab_mcp/tests/test_bridge.py`
- `source/mcp/khl_kit_lab_mcp/tests/test_policy.py`
- `source/mcp/khl_kit_lab_mcp/tests/test_profiles.py`
- `source/mcp/khl_kit_lab_mcp/tests/test_stage_e.py`
- `source/mcp/khl_kit_lab_mcp/tests/test_verifier.py`
- `source/mcp/khl_kit_lab_mcp/verify.py`
- `tests/test_mcp_user_local.py`

`.vscode/mcp.json`, the four NVIDIA knowledge service implementations/configurations,
the SDK, launcher, and historical Stage reports were not changed.

Generated evidence that must not be committed is outside the repository under
`/home/ubuntu/kit-ai/lab/profiles`, `/home/ubuntu/kit-ai/lab/experiments`, and
`/home/ubuntu/kit-ai/lab/launches`. Native Kit log contents were not read.

## Failed attempts

- One lifecycle command used an unsupported `--readiness-timeout` option and performed
  no action; it was replaced by the documented `--timeout` option.
- Several graceful restart commands returned a transient `AMBIGUOUS` startup result.
  Read-only rediscovery showed natural convergence to one READY process; no retry signal
  or force action was used.
- An immediate verifier during one final startup correctly failed while the bridge was
  still unavailable; a later read-only status showed READY.
- Four profiler operations failed association as listed above. Each subsequent attempt
  had a concrete reviewed hypothesis (native completion boundary, explicit CPU provider,
  then bounded cross-thread collection). No further capture was made after the exhaustive
  result.
- Kimi's read-only guard rejected configuration-path `git diff`/search attempts. It used
  direct read-only file access and synchronization tests instead; no guard was changed.

## Tool discipline and remaining work

- Codex was the sole repository/runtime writer. Reviewers were read-only.
- Kimi's generic tester role exposes `execute` and `kit-lab-runtime/*`, but the bounded
  Review 2 job prohibited all mutation; the trace shows only read-only operations.
- GLM's allowlist exactly matches the 15 current Kit Lab READ_ONLY policy entries and
  contains no wildcard, Python, reset, mutation, lifecycle control, or experiment write.
- No Python/system package installation, extension registry download, SDK/launcher edit,
  Nucleus operation, native log read, external profiler process, commit, push, tag, or
  release occurred.

The only recommended next step is a separate, newly authorized profiler-association
debugging job from this settled baseline. It may test the open frame-completion/flush
timing hypothesis, but that work is explicitly outside Stage E closure.

Experiment finalization: experiment
`20260909T141105596572Z-stage-e-extension-and-built-in-p-5b3c9b78` finished at
`2026-09-10T02:47:29.653Z` with outcome `inconclusive`; a separate read-only
`kit_experiment_current` observation returned `active: false`.
