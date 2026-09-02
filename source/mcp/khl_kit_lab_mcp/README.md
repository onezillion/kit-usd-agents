# KHL Kit Lab Runtime MCP

Local MCP adapter for the persistent `omni.khl.kit_lab` extension.

This package contains no model, embedding, retrieval, reranking, NVIDIA API or
Kubernetes logic. It translates typed MCP tools into deterministic calls to the
Kit Lab HTTP service.

## Boundaries

- MCP endpoint: `http://127.0.0.1:9910/mcp`
- Kit Lab endpoint: `http://127.0.0.1:8011`
- Both listeners remain loopback-only.
- Remote Kit HTTP targets are rejected unless `KIT_LAB_ALLOW_REMOTE=true` is
  explicitly set. The MCP listener itself cannot be exposed remotely.
- No credentials are required.

## Install

```bash
./setup-user-local.sh
```

The Python environment is stored under:

```text
/home/ubuntu/kit-ai/venvs/khl-kit-lab-mcp
```

## Run

Keep Kit running, then start the MCP in another terminal:

```bash
./run-user-local.sh
```

Verify from a third terminal:

```bash
./verify-user-local.sh
```

## VS Code

Preserve the existing `kit-dev-mcp` entry and add:

```json
"kit-lab-runtime": {
  "type": "http",
  "url": "http://127.0.0.1:9910/mcp"
}
```

## Environment variables

| Name | Default | Purpose |
|---|---|---|
| `KIT_LAB_BASE_URL` | `http://127.0.0.1:8011` | Kit HTTP service |
| `KIT_LAB_TIMEOUT_SECONDS` | `30` | Individual Kit request timeout |
| `KIT_LAB_MCP_HOST` | `127.0.0.1` | MCP bind host; loopback enforced |
| `KIT_LAB_MCP_PORT` | `9910` | MCP port |
| `KIT_LAB_ALLOW_REMOTE` | unset | Explicit remote Kit-target opt-in |

`kit_execute_python` is an unrestricted development escape hatch. It is marked
as potentially destructive in its MCP annotations. Prefer deterministic tools.
