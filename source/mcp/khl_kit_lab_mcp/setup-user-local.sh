#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
uv_bin="$HOME/.local/bin/uv"
venv="$HOME/kit-ai/venvs/khl-kit-lab-mcp"

test -x "$uv_bin"
mkdir -p "$HOME/kit-ai/venvs"

if ! "$uv_bin" python find 3.12 >/dev/null 2>&1; then
  "$uv_bin" python install 3.12
fi

if [ ! -x "$venv/bin/python" ]; then
  "$uv_bin" venv --python 3.12 "$venv"
fi

"$uv_bin" pip install \
  --python "$venv/bin/python" \
  "mcp==2.1.1"

PYTHONPATH="$script_dir/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$venv/bin/python" - <<'PY'
import mcp
from mcp.server import MCPServer
import khl_kit_lab_mcp

print("MCP_IMPORT_OK")
print("khl_kit_lab_mcp:", khl_kit_lab_mcp.__version__)
print("python SDK import:", MCPServer.__name__)
PY

printf 'Environment ready: %s\n' "$venv"
