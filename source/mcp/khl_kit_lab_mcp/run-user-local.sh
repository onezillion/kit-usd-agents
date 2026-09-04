#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="$HOME/kit-ai/venvs/khl-kit-lab-mcp/bin/python"

test -x "$python_bin"

export KIT_LAB_BASE_URL="${KIT_LAB_BASE_URL:-http://127.0.0.1:8011}"
export KIT_LAB_MCP_HOST="${KIT_LAB_MCP_HOST:-127.0.0.1}"
export KIT_LAB_MCP_PORT="${KIT_LAB_MCP_PORT:-9910}"
export KIT_LAB_TIMEOUT_SECONDS="${KIT_LAB_TIMEOUT_SECONDS:-30}"
export KIT_LAB_EXPERIMENT_ROOT="${KIT_LAB_EXPERIMENT_ROOT:-$HOME/kit-ai/lab/experiments}"
export PYTHONPATH="$script_dir/src${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$KIT_LAB_EXPERIMENT_ROOT"
chmod 700 "$KIT_LAB_EXPERIMENT_ROOT"

exec "$python_bin" -m khl_kit_lab_mcp.server
