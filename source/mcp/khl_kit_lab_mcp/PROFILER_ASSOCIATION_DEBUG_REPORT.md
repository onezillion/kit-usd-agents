# Profiler Association Debug — Carbonite CPU `IProfileMonitor` Marker Association

Job: PROFILER-ASSOCIATION-DEBUG. Verified 2026-09-10 on Kit `110.1.3+production.349534.56c4c3a1.gl`.
This report is separate from, and does not modify, the historical `STAGE_E_REPORT.md`.

## Summary

**PASS.** The bounded Carbonite CPU profiler capture could not associate its unique
operation marker because **when Python instrumentation is enabled, `begin/end` zones
do not appear in the completed-frame snapshot on `carb.profiler.cpu.in_memory` —
while `instant()` events still do.** The smallest production correction (add a unique
`instant()` ownership token before `mark_frame_end()`, keeping the payload zones)
restores association, **verified live twice with `python_profile=true`** (the exact
configuration that always failed in Stage E), with exact restoration and independent
post-operation verification.

This conclusion was produced by a two-phase campaign of bounded, discriminating live
probes — not by re-running the Stage E captures.

## Baseline

- Branch `main`, HEAD `92b883e8ca67a413177dd71a668e0787f2f87c07`
  (`Add omni-ui, usd-code, and isaacsim tools to KHL agents`), clean working tree, no drift.
- Kit: `nchc-kit-dev-main`, Kit `110.1.3`, renderer `RealTimePathTracing`, single GPU.
- Pre-change runtime: READY, PID `1581924`, ticks `225386974`, bridge API `0.5.0`,
  bridge generation `372ebf477f8644a9900346f49502a627`.
- Profiler baseline (job-authorized read-only): `carb.profiler.cpu.in_memory` loaded,
  `capture_active=false`, `capture_mask=0`, `python_profiling=false`, `capture_owner=null`,
  `bridge_capture_active=false`; profiler extensions `omni.activity.profiler-1.0.4` /
  `omni.kit.profiler.window-2.5.0` enabled, `omni.kit.profiler.tracy-1.2.1` disabled.
- Stage E (unchanged, historical): E1 PASS; self-disable FORBIDDEN; self-reload RESTRICTED;
  E2 INCONCLUSIVE (scoped). E2 was not reopened; this campaign investigated the mechanism.

## Phase A — diagnostic probes (all live, bounded, exact restoration each time)

Installed API identities were established first from the authoritative files
(`kit-sdk/kernel/py/carb/profiler/_profiler.pyi`, `dev/include/carb/profiler/IProfiler.h`,
`IProfileMonitor.h`). Notable documented facts used:
- `mark_frame_end()` defines the snapshot as "the previous frame (up to the previous
  `markFrameEnd()` call)"; `get_last_profile_events()` returns only that frame.
- `kNoZoneId` indicates a zone discarded, "typically because it doesn't match the
  current capture mask" (mask gating, not visibility).
- `InstantType` C++ doc **comments are swapped relative to names**
  (`Thread` says "entire process", `Process` says "thread profile zone"); naming must not
  be assumed from docs.
- `InstantType.THREAD` is the intended thread-timeline instant and is used.

GLM Review 1 (checkpoint 1, GO) tightened the probe design: removed a baseline
`mark_frame_end` with no discriminative value, removed `ensure_thread()` during the
capture window, recorded `get_main_thread_id()`, collected `snapshot1` even on a
`snapshot0` hit, and **dropped Probe B** (module-level and explicit-CPU `begin/end` share
the same emitter/monitor because only the mask keys capture — B carried no discriminative
information).

### Discriminator 1 — frame publication semantics (sync, python OFF, one zone, no cadence)
- marker `KHL_PROBE_FRAMESYNC_DO_NOT_REUSE`; mask `0→1→0`; snapshot0 **FOUND**
  (`tid 159960514`, 72 events); snapshot1 **absent** (59 events); restoration complete.
- **Conclusion:** `get_last_profile_events()` returns only the just-ended frame; no
  re-publication, no delayed publication. Explicit `carb.profiler-cpu.plugin` begin/end
  **does** associate under Python-OFF. Start of the timing/routing branch: closed.

### Discriminator A/B — Python-instrumentation interaction (same emission+read, ON vs OFF)
- marker `KHL_PROBE_PYINTERACT_DO_NOT_REUSE`; mask `0→1→0`, python `off→on→off`; restoration complete.
- python **ON**: snapshot0 marker **NOT FOUND** (76 events); snapshot1 absent (287).
- python **OFF**: snapshot0 marker **FOUND** (`tid 160070310`, 611 events); snapshot1 absent (175).
- **Conclusion:** the single toggled variable (Python instrumentation) determines whether
  the `begin/end` zone appears in the completed-frame snapshot. Event volume `76↔611`
  corroborates that capture routing changes under Python mode. This **is the Stage E
  failure mode**: production always ran with `python_profile=true`. Async/update-cadence
  was rendered unnecessary (the sync shape reproduced both pass and fail).

### Fix-direction probe — candidate association tokens
- marker `KHL_PROBE_FIXDIR_DO_NOT_REUSE`; restoration complete.
- python ON `instant(THREAD)` → snapshot0 **FOUND** (60 events).
- python ON `begin + value_int + end` → **NOT FOUND** (snapshot0 1114 / snapshot1 2813).
- python OFF `instant(THREAD)` / `instant(PROCESS)` / `begin+value_int+end` → all FOUND.
- **Conclusion:** `instant()` is a Python-safe ownership/association token;
  `value_int` in-zone is not. `InstantType.THREAD` names correctly in practice.

### Replication — instant under Python ON
- new marker, second run: snapshot0 **FOUND** (`tid 160232591`, 68 events); snapshot1 absent.
- Repeatable; not a fluke.

## Production implementation

One-line change in `source/extensions/omni.khl.kit_lab/omni/khl/kit_lab/stage_e.py`
(at the `STOPPING` boundary): the payload `begin/end` zone is preserved, and a unique

```python
profiler.instant(PROFILE_MASK, carb.profiler.InstantType.THREAD, marker)
```

is emitted immediately before `monitor.mark_frame_end()`. Association for the capture
ID contract is satisfied by the instant token (unique per capture, emitted exactly once
on the operation thread). The change preserves all existing Stage E safety/ownership
behavior (bounded inputs, fixed max duration, bounded events, control serialization,
pre-existing-refusal, exact mask/Python/settings restoration, truthful failure codes, no
native-export overclaim).

## Frame-aware tests

`source/mcp/khl_kit_lab_mcp/tests/test_stage_e.py` was upgraded so the profiler fake models
the **measured** runtime semantics rather than a cumulative event list:
- `FakeProfiler.begin/end` append zone events only when `self.python` is False (zones
  invisible under Python instrumentation);
- `FakeProfiler.instant(...)` appends under both instrumentation states;
- `mark_frame_end()` publishes only the current frame's events and clears the window
  (no re-publication); `get_last_profile_events()` returns the snapshot.
- New regression test `test_instant_token_associates_when_python_profiling_drops_zones`
  runs the full capture with Python ON and asserts association via the instant token.
- The zone-after-boundary regression is inherent in the harness: any zone emitted after
  `mark_frame_end()` cannot retroactively appear in the prior snapshot.
- Removed the previous `profiler_module.begin/end` bypasses that returned a cumulative list.

Results: **84/84** package tests PASS (was 83; +1 new regression test), **10/10** root tests PASS.

## Live result (production `kit_profiler_capture`)

A graceful (no-force) Kit lifecycle restart activated the changed bridge
(`372ebf…` → `4bf777b76b0a46f7b1c3f4893e733bd1`, PID `4043435`). The transient
duplicate-candidate startup window converged naturally to READY with one candidate;
no force/SIGKILL/manual signaling was used.

Two independent bounded captures, both `python_profile=true`, `duration=1.0s`:

| capture_id | marker_thread_id | frames | events_visited | events_returned | phase | restoration |
|---|---|---:|---:|---:|---|---|
| `8403ade32abc4964be08cf629af8a823` | 101724 | 121 | 1121 | 512 (bounded) | COMPLETE | complete, conflicts=[] |
| `db58d41d3ca4489f978a82288d86d5ad` | 435935 | 125 | 1268 | 512 (bounded) | COMPLETE | complete, conflicts=[] |

For both: `marker_found=true`, the generated marker encoded the capture ID exactly, found event
`duration=0.0, indent=1` (the **instant** token), `profile_thread_id` ==
`association_scan.marker_thread_id`, `final_capture_mask=0`, `final_python_profiling=false`.
Acceptance met on all points (marker found; useful zone payload present; marker belongs
to capture ID; bounded parsing; state/mask/Python restored; no leftover task; responsive).

**Repeatability:** first capture succeeded cleanly; one confirmation capture succeeded
identically. Association is demonstrated strongly enough for this job.

## State restoration (independent, post-operation)

- READY, single candidate PID `4043435`, ticks `230367580`, bridge API `0.5.0`.
- `busy=false`, `namespace_keys=[]`, `capture_active=false`, `capture_owner=null`,
  `bridge_capture_active=false`, `capture_mask=0`, `python_profiling=false`, `mutated=false`.
- Profiler extensions unchanged (`omni.activity.profiler-1.0.4`, `omni.kit.profiler.window-2.5.0`
  enabled; `omni.kit.profiler.tracy-1.2.1` disabled). Baseline settings (`saveProfile=false`,
  `compressProfile=true`, `filePath` absent) were snapshotted and conflict-checked.
- No operation-owned capture remains. No background task remains. No temp stage prim added.

## Failed attempts ledger

| attempt (marker) | hypothesis | result | strengthened / weakened | closed? |
|---|---|---|---|---|
| framesync (KHL_PROBE_FRAMESYNC) | delayed publication / emission routing | snapshot0 FOUND; snapshot1 absent | weakens delay & emission-failure | YES (both disproven) |
| python A/B (KHL_PROBE_PYINTERACT) | python instrumentation interaction | ON absent / OFF FOUND | **strongly supports python interaction** | YES (cause identified) |
| fix-direction (KHL_PROBE_FIXDIR) | instant is a python-safe token | instant FOUND (ON & OFF); value_int not | supports instant token; rejects value_int | YES |
| instant replication (KHL_PROBE_INSTANTPYON) | reproducibility of instant under python | FOUND again | confirms repeatability | YES |
| production capture x2 | production correctness/persistence | COMPLETE, marker_found, restored | proves fix in real path | YES |

## GLM reviews

- **Review 1 (checkpoint 1, GO):** probe-design corrections (removed no-value baseline
  boundary and in-window `ensure_thread`, added `main_thread_id`, collected snapshot1
  even on hit; dropped redundant Probe B). Affected the probe matrix; no production change yet.
- **Review 2 (checkpoint 2, GO):** corroborated root cause (scoped to observation),
  association, restoration, and conservative capability wording; flagged one non-blocking
  residual (see Open questions) and one honesty note (cadence isolate not separately probed).

## Rejected / not-pursued hypotheses

- **Delayed/re-published frame publication** — disproven by Discriminator 1.
- **Explicit-provider emission does not reach the monitor** — disproven by OFF-step successes.
- **Global/mux vs explicit emitter** (Probe B) — dropped as non-discriminating by design.
- **`value_int` sample as the ownership token** — rejected; does not associate under Python ON.
- **Re-running the same failed Stage E capture** — not done; the failure was isolated to the
  Python-instrumentation interaction via A/B, not cadence.

## Open questions

- **Mechanism (unproven, intentionally not claimed):** *why* zones do not appear under
  Python instrumentation is not established. The `76↔611` volume shift is consistent with
  either per-frame budget overflow or genuine non-routing of zones into the CPU in-memory
  store under Python mode. The instant-token fix is **mechanism-agnostic** and measurably
  restores association in both modes; no specific C++ cause is claimed.
- **Per-iteration same-named-zone cadence** was not separately isolated as a budget/flood
  factor; it is subsumed by the mechanism-agnostic token but is one of the explanations
  the A/B alone does not distinguish.

## Capability wording

At the end of the discovery campaign capabilities were `available_requires_capture_association`.
A follow-up **architecture revision** (`profiler-collection-revision`, see below) then revised
the collector to dedicated exact association tokens with deterministic fair cross-thread
collection, truthful truncation flags, and atomic duplicate-ID reservation, and **promoted
the capability to `available_verified_bounded_capture`** after live python-ON plus python-OFF
captures each associated, fairly collected, truthfully flagged truncation, and restored state
independently. The name stays explicit to the bounded CPU in-memory contract; native file
export, GPU, and Tracy remain `unsupported`. No bare `supported`/`available` claim is used.

## Files changed

Only the two intended production/test files (left uncommitted for operator inspection):
- `source/extensions/omni.khl.kit_lab/omni/khl/kit_lab/stage_e.py` (instant ownership token)
- `source/mcp/khl_kit_lab_mcp/tests/test_stage_e.py` (frame-aware harness + regression test)

## Verification results (closure)

- `test-user-local.sh`: 90/90 PASS (discovery campaign 84/84; revision added 6 regression tests)
- `python3 -m unittest discover -s tests -v`: 10/10 PASS
- `manage-mcps-user-local.sh verify`: 5-service PASS (10/12/7/5/27)
- `verify-user-local.sh`: PASS / 27 tools
- `verify-user-local.sh --lifecycle`: PASS / READY single candidate
- `git diff --check`: PASS
- Discovery captures `8403ade...`/`db58d41d...` (python ON, old bridge): COMPLETE, marker associated, restored
- Revision captures (revised bridge `cf391aea...`): `adb7691...` (python ON) and `d318a8b...` (python OFF)
  COMPLETE, association token found exactly, fair cross-thread collection, truthful flags, restored

## Experiment / artifacts

- Discovery experiment: `20260910T041014225647Z-profiler-association-debug-f3f66b68`
  (finished 2026-09-10 during housekeeping of this revision job with outcome `success`).
- Revision experiment: `20260910T073814635800Z-profiler-collection-revision-b95ef743`
  (verify-only for the collection architecture; finished 2026-09-10 with outcome `success`).
- Discovery captures: `/home/ubuntu/kit-ai/lab/profiles/{8403ade32abc4964be08cf629af8a823,db58d41d3ca4489f978a82288d86d5ad}/result.json`.
- Revision captures (revised bridge): `/home/ubuntu/kit-ai/lab/profiles/{adb76910574940349f8e4f798998c134,d318a8b7227e467084dce333db6a21fa}/result.json`.
- Instant-token probe evidence was bounded in-memory (summarized above).
