# omni.khl.kit_lab

Development-only persistent laboratory service for an active Omniverse Kit
runtime.

## Canonical endpoints

- `GET /khl/lab/status`
- `GET /khl/lab/runtime/info`
- `GET /khl/lab/stage/summary`
- `POST /khl/lab/prim/inspect`
- `POST /khl/lab/extensions/list`
- `POST /khl/lab/settings/get`
- `GET /khl/lab/viewport/info`
- `POST /khl/lab/session/reset`
- `POST /khl/lab/python/execute`

Phase 2B.1 operations are read-only. Their response envelope is:

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
in the development laboratory. Agents should prefer the deterministic read
operations and use `python.execute` only when the narrow API is insufficient.

Phase 2B.1 does not expose deterministic mutations. Scoped temporary-stage
mutations and artifact persistence belong to a later phase.
