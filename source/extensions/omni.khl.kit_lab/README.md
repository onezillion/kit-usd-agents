# omni.khl.kit_lab

Development-only persistent laboratory service for an active Omniverse Kit
runtime.

## Canonical endpoints

- `GET /khl/lab/status`
- `GET /khl/lab/runtime/info`
- `GET /khl/lab/stage/summary?include_statistics=false`
- `POST /khl/lab/stage/summary` with `{"include_statistics": false}`
- `POST /khl/lab/extensions/list`
- `GET /khl/lab/viewport/info`
- `POST /khl/lab/session/reset`
- `POST /khl/lab/python/execute`

The runtime context operations are read-only. Their response envelope is:

```json
{
  "ok": true,
  "operation": "stage.summary",
  "result": {}
}
```

Failures use `ok: false` with a stable error object. Large Kit/USD values and
collections are bounded before JSON serialization.

## Compatibility endpoints

The Phase 2A migration retains the original endpoints:

- `GET /khl/ai/status`
- `POST /khl/ai/reset`
- `POST /khl/ai/execute`

The compatibility endpoints use the same lock and persistent namespace as the
canonical endpoints.

## Safety boundary

This extension intentionally permits unrestricted Python execution inside the
Kit interpreter. Keep the Kit HTTP service bound to `127.0.0.1` and use it only
in the development laboratory. Use authorized Python to develop and debug reusable
installed-version Kit/USD source and investigate scenes, attributes, and settings.
Arbitrary Python is elevated execution even for inspection; follow MCP policy and
existing task/session authorization. No deterministic scene-editing API is exposed.

## API 0.3.0 summary contract

The prim inspection and setting getter routes/models/capabilities have been removed.
Python and legacy compatibility routes are retained. The parser/executor still supports
a persistent namespace, top-level await, last-expression results, and captured
stdout/stderr/traceback. Reset clears that namespace; it does not restart Kit, cancel
tasks, unload modules, or guarantee cleanup. Client timeout does not stop execution.

Both summary routes default to `include_statistics=false`: no full prim traversal,
`statistics_computed=false`, `prim_count=null`, and `type_counts=null`. With no stage,
statistics also remain uncomputed. When requested, statistics use the full default
`Usd.Stage.Traverse()` predicate, accumulating counts without retaining all prims.
This work is not bounded and may be expensive. A computed empty stage has zero prims.
Root-child paths are limited to 256 with `root_children_truncated`; other collection
serialization retains the existing bounds. Basic metadata/layer queries still have
USD-dependent costs; the lightweight guarantee is absence of full prim traversal.

MCP uses POST to avoid invoking the former expensive GET on an old loaded bridge.
Source edits require a separately authorized Kit restart or extension activation before
live routes change. Changing disk source or restarting the MCP does not reload Kit.

## API 0.4.0 identity contract

`GET /khl/lab/runtime/identity` returns the usual `ok`/`result` envelope with
`kit_id` (inherited `KHL_KIT_ID`, or null), `pid`, Linux `start_ticks`, `api_version`,
`ready` from `IApp.is_app_ready()`, and `native_log_path` from Carbonite `/log/file`.
It does not acquire the Python execution lock or read log contents. A blocked Kit
main loop can still make HTTP unresponsive; lifecycle OS discovery runs in the MCP.
`/khl/lab/status` additionally reports `kit_id` and `pid` and advertises `runtime.identity`.
All Stage A+B and `/khl/ai/*` compatibility routes remain intact. No lifecycle signal
or launch route is exposed inside Kit. Activate this version by restarting Kit or
explicitly reloading the extension; editing source alone does not reload it.
