#!/bin/bash
# Launch the NVIDIA Kit MCP from its dedicated user-space environment.

set -e

KIT_MCP_ROOT="/home/ubuntu/Documents/kit-usd-agents/source/mcp/kit_mcp"
KIT_MCP_ENV_FILE="/home/ubuntu/Documents/kit-usd-agents/source/mcp/.env"
KIT_MCP_VENV="/home/ubuntu/kit-ai/venvs/nvidia-kit-mcp"
KIT_MCP_STATE_ROOT="/home/ubuntu/kit-ai"

if [ -f "$KIT_MCP_ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$KIT_MCP_ENV_FILE"
    set +a
fi

if [ -z "${NVIDIA_API_KEY:-}" ]; then
    echo "WARNING: NVIDIA_API_KEY is not set; cloud-backed Kit searches may fail." >&2
fi

export VIRTUAL_ENV="$KIT_MCP_VENV"
export PATH="$KIT_MCP_VENV/bin:/home/ubuntu/.local/bin:/usr/bin:/bin"
export XDG_CACHE_HOME="$KIT_MCP_STATE_ROOT/cache"
export TMPDIR="$KIT_MCP_STATE_ROOT/tmp"
export MCP_HOST="127.0.0.1"
export MCP_PORT="9902"
export KIT_MCP_DISABLE_USAGE_LOGGING="true"
export PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring"

mkdir -p "$XDG_CACHE_HOME" "$TMPDIR"
cd "$KIT_MCP_ROOT"
exec "$KIT_MCP_VENV/bin/kit-mcp" workflows/local_config.yaml
