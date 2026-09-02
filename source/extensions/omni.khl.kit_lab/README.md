# omni.khl.kit_lab

Development-only persistent laboratory service for an active Omniverse Kit
runtime.

## Canonical endpoints

- `GET /khl/lab/status`
- `POST /khl/lab/session/reset`
- `POST /khl/lab/python/execute`

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
in the disposable development laboratory. Structured Kit operations,
experiment persistence, and permission levels are planned for the next phase.
