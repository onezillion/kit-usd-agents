# Kit MCP Server

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that gives AI coding assistants deep knowledge of NVIDIA Omniverse Kit — extensions, app templates, code examples, settings, and developer instructions — powered by semantic search and NVIDIA AI reranking.

Built on the [NVIDIA AIQ Toolkit](https://github.com/NVIDIA/GenerativeAIExamples) (formerly NeMo Agent Toolkit / NAT 1.3+).

---

## 5-Minute Quickstart

Get from zero to working Kit tools in your IDE. Follow every step in order.

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed and running
- An NVIDIA API key (see Step 1 below)
- [Git LFS](https://git-lfs.com/) installed — the FAISS indices and extension metadata under `source/aiq/*/data/` are LFS-tracked. `build-wheels.sh` auto-runs `git lfs install --local && git lfs pull` for you on the first build, so you only need the binary on PATH (`sudo apt-get install git-lfs` or `brew install git-lfs`). Without LFS, the wheel ends up ~13× smaller and the container silently fails at first tool call with `Extension data is not available` — auto-recovery in `build-wheels.sh` catches this.
- The repo cloned: `git clone https://github.com/NVIDIA-Omniverse/kit-usd-agents.git`

### Step 1: Get Your API Key

| Key | What It's For | Where to Get It |
|-----|---------------|-----------------|
| `NVIDIA_API_KEY` | Authenticates calls to NVIDIA's cloud endpoints for embeddings, reranking, and LLM inference | [build.nvidia.com/settings/api-keys](https://build.nvidia.com/settings/api-keys) — sign in, click **Generate API Key**, copy the `nvapi-...` value |

> **Note:** A second key (`NGC_API_KEY` from [org.ngc.nvidia.com/setup/api-key](https://org.ngc.nvidia.com/setup/api-key)) is only required if you plan to run embedder/reranker models locally via NVIDIA NIM containers — see [Deployment Options](#deployment-options).

### Step 2: Configure Your `.env`

```bash
cd kit-usd-agents/source/mcp
cp .env.example .env
```

Open `.env` and set:

```env
NVIDIA_API_KEY=nvapi-YOUR_KEY_HERE
```

### Step 3: Build and Run the Docker Container

```bash
cd kit_mcp

# Build the image
./build-docker.sh        # Linux/macOS
# build-docker.bat       # Windows

# Run the server (note --env-file points to ../.env, one level up)
docker run --rm -p 9902:9902 --env-file ../.env kit-mcp:latest
```

> **`.env` location matters.** `--env-file ../.env` resolves relative to your current directory. Run `docker run` from `source/mcp/kit_mcp/`. If you need to launch from elsewhere, use the absolute path: `--env-file "$(git rev-parse --show-toplevel)/source/mcp/.env"`.

### Step 4: Verify the Server is Running

The server speaks Streamable HTTP at `/mcp` (the canonical endpoint in NAT 1.25). There is **no separate `/health` GET endpoint**; the canonical liveness probe is an MCP `initialize` POST.

> **Trailing slash:** older NAT 1.3 builds required `/mcp/`. NAT 1.25 returns `307 Temporary Redirect` from `/mcp/` to `/mcp`, so both work — but using `/mcp` directly avoids a redirect on every call. If you keep the trailing slash, pass `-L` to curl.

In a new terminal:

```bash
# Easiest: use the included Python health check
python check_mcp_health.py

# Or curl the MCP endpoint directly. The Accept header is required —
# NAT 1.4 streams the response as text/event-stream and returns 406
# without it.
curl -s -X POST http://localhost:9902/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"health","version":"1.0"}}}'
```

A healthy server returns a JSON-RPC `result` payload listing the server's name and capabilities.

### Step 5: Connect Your IDE

All IDE configs point at the same URL: `http://localhost:9902/mcp`.

> **MCP scoping note (Claude Code):** `claude mcp add ... -t http <url>` writes to `.claude.json` in your **current working directory**. The MCP is then only active when you launch Claude CLI from that directory. To register globally, pass `--scope user`. This caveat has bitten developers in practice — running `claude mcp add` from `kit-app-template/`, then opened a new terminal elsewhere and found `kit-dev-mcp` mysteriously absent from `claude mcp list`.

<details>
<summary><strong>Cursor</strong></summary>

Create `.cursor/mcp.json` in your project root (or `~/.cursor/mcp.json` for global access):

```json
{
  "mcpServers": {
    "kit-dev-mcp": {
      "url": "http://localhost:9902/mcp"
    }
  }
}
```

> **Common docs typo:** some internal docs show `"type": "kit-dev-mcp"` — that is **not** a valid MCP transport. Cursor's `mcp.json` accepts a bare `"url"` (no `"type"` field needed); if you must include one, the correct value is `"type": "http"`.

Reload Cursor: `Cmd/Ctrl+Shift+P` → **Developer: Reload Window**

</details>

<details>
<summary><strong>Claude Code</strong></summary>

Add via the CLI (project scope — registers in `.claude.json` in your cwd):

```bash
claude mcp add kit-dev-mcp -t http http://localhost:9902/mcp
```

For user (global) scope:

```bash
claude mcp add kit-dev-mcp --scope user -t http http://localhost:9902/mcp
```

Or add it directly to your `~/.claude.json`:

```json
{
  "mcpServers": {
    "kit-dev-mcp": {
      "type": "http",
      "url": "http://localhost:9902/mcp"
    }
  }
}
```

</details>

<details>
<summary><strong>Windsurf</strong></summary>

Create `~/.windsurf/mcp.json`:

```json
{
  "mcpServers": {
    "kit-dev-mcp": {
      "url": "http://localhost:9902/mcp"
    }
  }
}
```

Restart Windsurf to pick up the new server.

</details>

<details>
<summary><strong>VS Code (Copilot)</strong></summary>

Add to your `.vscode/mcp.json`:

```json
{
  "servers": {
    "kit-dev-mcp": {
      "type": "http",
      "url": "http://localhost:9902/mcp"
    }
  }
}
```

</details>

### Step 6: Verify Tools Appear

In your IDE's AI chat, you should see **12 Kit tools**:

| Tool | Description |
|------|-------------|
| `get_kit_instructions` | Top-level guidance on Kit development concepts |
| `search_kit_app_templates` | Discover app templates (USD Composer, USD Explorer, etc.) |
| `get_kit_app_template_details` | Detailed info on a specific app template |
| `search_kit_extensions` | Semantic search across the indexed Kit extension catalog |
| `get_kit_extension_details` | Full details for one or more extensions (super-flexible input format) |
| `get_kit_extension_dependencies` | Resolve an extension's dependency graph |
| `get_kit_extension_apis` | List the public APIs exposed by an extension |
| `get_kit_api_details` | Full signature + docstring for a Kit API |
| `search_kit_code_examples` | Find Kit code patterns by description |
| `search_kit_test_examples` | Find Kit test patterns by description |
| `search_kit_settings` | Find a Kit setting by name or purpose |
| `search_kit_knowledge` | General Kit-documentation Q&A retrieval |

Try asking: *"Find me the Kit extension that does clash detection"* — if you get the `omni.physxclashdetection.bundle` family back, the index is intact. If you get "no published Kit bundle in the registry", the index is missing that family — see [Index Coverage](#index-coverage) below.

---

## Architecture Overview

```
┌──────────────────────┐
│  Your IDE             │
│  (Cursor / Claude     │
│   Code / Windsurf /   │
│   VS Code Copilot)    │
└────────┬─────────────┘
         │ MCP (Streamable HTTP, POST /mcp/)
         ▼
┌──────────────────────┐
│  Kit MCP Server       │ ← This package
│  (port 9902)          │
└────────┬─────────────┘
         │ AIQ Workflow
         ▼
┌──────────────────────┐
│  RAG Pipeline         │
│  ┌────────────────┐  │
│  │ Embedder       │  │  ← NVIDIA cloud or local NIM
│  │ Reranker       │  │  ← NVIDIA cloud or local NIM
│  └────────────────┘  │
└────────┬─────────────┘
         │
         ▼
┌──────────────────────┐
│  Kit Atlas Database   │  ← Extensions, app templates,
│                       │    code examples, settings,
│                       │    instructions
└──────────────────────┘
```

---

## Deployment Options

### Option A: Cloud Endpoints (Recommended)

```env
NVIDIA_API_KEY=nvapi-YOUR_KEY_HERE
```

### Option B: Local NIM Containers (Advanced, GPU required)

```env
NVIDIA_API_KEY=nvapi-YOUR_KEY_HERE
NGC_API_KEY=your_ngc_key_here
KIT_EMBEDDER_BACKEND=local
KIT_LOCAL_EMBEDDER_URL=http://localhost:8080
KIT_RERANKER_BACKEND=local
KIT_LOCAL_RERANKER_URL=http://localhost:8081
```

> NGC API key: [org.ngc.nvidia.com/setup/api-key](https://org.ngc.nvidia.com/setup/api-key). Full local-NIM setup including `docker login nvcr.io`, wheel building, and the `docker-compose` flow is in [`source/mcp/LOCAL_DEPLOYMENT.md`](../LOCAL_DEPLOYMENT.md).

---

## Project Structure

```
source/mcp/kit_mcp/
├── VERSION.md                 # Version information
├── README.md                  # This file
├── pyproject.toml             # Poetry configuration and dependencies
├── Dockerfile                 # Docker image configuration
├── check_mcp_health.py        # MCP-initialize-based health probe
├── setup-dev.sh / .bat        # Development environment setup
├── run.sh / run.bat           # Local run scripts (non-Docker)
├── build-docker.sh / .bat     # Docker image build scripts
├── workflows/                 # AIQ workflow configs
│   ├── config.yaml            # Cloud-endpoint workflow
│   └── local_config.yaml      # Local-NIM workflow
└── src/
    └── kit_mcp/               # Server source code
```

The data corpus ships pre-built in the wheel at `source/aiq/kit_fns/src/kit_fns/data/<kit_version>/`.

---

## Troubleshooting

| Problem | Likely Cause | Fix |
|---------|-------------|-----|
| `connection refused` on port 9902 | Docker container not running | `docker ps` to check; restart the container if needed |
| `404` on `GET /health` | No `/health` GET endpoint exists | Use `python check_mcp_health.py` or POST an MCP `initialize` to `/mcp` (Step 4) |
| `307 Temporary Redirect` on POST `/mcp/` | NAT 1.25 canonicalises to `/mcp`. `curl -f` (without `-L`) treats 307 as success, so a healthcheck never exercises the endpoint. | Drop the trailing slash, or pass `-L` to curl. The repo's Dockerfile and compose healthchecks both use `curl -fL ... /mcp`. |
| `401 Unauthorized` from cloud calls | Invalid or expired `NVIDIA_API_KEY` | Regenerate at [build.nvidia.com/settings/api-keys](https://build.nvidia.com/settings/api-keys); update `.env` |
| `--env-file: file not found` | Wrong cwd when invoking `docker run` | Run from `source/mcp/kit_mcp/`, or use absolute path: `--env-file "$(git rev-parse --show-toplevel)/source/mcp/.env"` |
| Port 9902 already in use | Another process on that port | `lsof -i :9902` or `netstat -aon \| findstr 9902`; stop or remap to e.g. `-p 9912:9902` |
| Tools not appearing in IDE | MCP config not loaded or wrong URL | Verify with `check_mcp_health.py`; check IDE's MCP config path and URL (use `/mcp` — trailing slash works too via 307 redirect); reload IDE |
| `kit-dev-mcp` missing from `claude mcp list` | `-t http` registered the MCP at project scope (writes `.claude.json` in cwd) | Re-add with `--scope user` for global, or always launch Claude CLI from the project root where you registered |
| Cursor: `"type": "kit-dev-mcp"` in some docs | Docs typo — `kit-dev-mcp` is not a transport | Cursor accepts bare `"url"`; if you must specify a type, use `"type": "http"` |

---

## Headless / SSH developer path

The MCP server itself runs headlessly under `./run.sh` — no GUI required. Some Kit-app workflows that this MCP helps users discover (e.g. installing extensions in a USD Composer / USD Explorer build) are typically demonstrated GUI-first (Window → Extensions → search → install → AUTOLOAD). The supported alternative for headless dev is editing the `.kit` config file directly:

```toml
[dependencies]
"omni.physxclashdetection.bundle" = { version = "110.1.7" }
"omni.kit.viewport.navigation.usd_explorer.bundle" = {}
```

The next `./repo.sh build` of the kit-app-template will resolve and pull these into `extscache`. This is the documented dev-path alternative for SSH / CI environments — call it out when an MCP-assisted developer is working remotely.

---

## Repository user-local service

For durable loopback deployment, use the repository-root
[`setup-mcps-user-local.sh`](../../../setup-mcps-user-local.sh) and
[`manage-mcps-user-local.sh`](../../../manage-mcps-user-local.sh). The local
wrapper in this directory now uses the shared NVIDIA venv, keeps the existing
local-NIM config, binds port 9902 only to `127.0.0.1`, disables usage analytics,
and stores runtime files outside Git. The upstream `run.sh` below remains the
Poetry-oriented development launcher.

## Development

### Running Locally (Without Docker)

```bash
./setup-dev.sh        # Linux/macOS
# setup-dev.bat       # Windows

./run.sh              # Linux/macOS
# run.bat             # Windows
```

### Configuration Files

- `workflows/config.yaml` — Cloud-endpoint workflow
- `workflows/local_config.yaml` — Local-NIM workflow

---

## License

See the [LICENSE](../../../LICENSE) file in the root of this repository.
