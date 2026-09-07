#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
env_file="$(cd -- "$script_dir/.." && pwd)/.env"
venv="${KIT_USD_MCP_VENV:-/home/ubuntu/kit-ai/venvs/kit-usd-mcp}"
state_root="${KIT_USD_MCP_STATE_ROOT:-/home/ubuntu/kit-ai}"

if [ -f "$env_file" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$env_file"
    set +a
fi

command_path="$venv/bin/usd-code-mcp"
if [ ! -x "$command_path" ]; then
    printf 'ERROR: USD Code MCP is not installed in %s; run setup-mcps-user-local.sh.\n' "$venv" >&2
    exit 69
fi

export VIRTUAL_ENV="$venv"
export PATH="$venv/bin:/home/ubuntu/.local/bin:/usr/bin:/bin"
export XDG_CACHE_HOME="$state_root/cache/kit-usd-mcps/usd-code"
export TMPDIR="$state_root/tmp/kit-usd-mcps/usd-code"
export MCP_HOST="127.0.0.1"
export MCP_PORT="9903"
export USD_CODE_MCP_DISABLE_USAGE_LOGGING="true"
export KIT_MCP_DISABLE_USAGE_LOGGING="true"
export PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring"

mkdir -p "$XDG_CACHE_HOME" "$TMPDIR"
cd "$script_dir"
exec "$command_path" workflow/local_config.yaml
