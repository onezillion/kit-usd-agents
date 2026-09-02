#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="$HOME/kit-ai/venvs/khl-kit-lab-mcp/bin/python"

test -x "$python_bin"
export PYTHONPATH="$script_dir/src${PYTHONPATH:+:$PYTHONPATH}"

exec "$python_bin" "$script_dir/verify.py"
