#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
uv_bin="${UV_BIN:-/home/ubuntu/.local/bin/uv}"
venv="${KIT_USD_MCP_VENV:-/home/ubuntu/kit-ai/venvs/kit-usd-mcp}"
assume_yes=false
dry_run=false
lfs_url="${KIT_USD_MCP_LFS_URL:-}"

usage() {
    cat <<'EOF'
Usage: ./setup-mcps-user-local.sh [--yes] [--dry-run] [--venv ABSOLUTE_PATH]

Creates or updates the shared environment for the four official NVIDIA MCPs,
materializes their Git LFS data, and delegates Kit Lab setup to its existing
setup-user-local.sh. It never deletes or replaces another environment.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --yes|-y)
            assume_yes=true
            shift
            ;;
        --dry-run)
            dry_run=true
            shift
            ;;
        --venv)
            if [ "$#" -lt 2 ]; then
                printf 'ERROR: --venv requires a path.\n' >&2
                exit 64
            fi
            venv="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            printf 'ERROR: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 64
            ;;
    esac
done

case "$venv" in
    /*) ;;
    *)
        printf 'ERROR: the shared venv path must be absolute: %s\n' "$venv" >&2
        exit 64
        ;;
esac
if [ "$venv" = "/" ] || [ "$venv" = "/home" ] || [ "$venv" = "/home/ubuntu" ]; then
    printf 'ERROR: refusing unsafe shared venv path: %s\n' "$venv" >&2
    exit 64
fi
if [ ! -x "$uv_bin" ]; then
    printf 'ERROR: uv is not executable at %s. Set UV_BIN to override.\n' "$uv_bin" >&2
    exit 69
fi
if ! command -v git-lfs >/dev/null 2>&1; then
    printf 'ERROR: git-lfs is required. Install it in user space before setup.\n' >&2
    exit 69
fi

printf 'Repository: %s\n' "$repo_root"
printf 'Shared NVIDIA venv: %s\n' "$venv"
printf '%s\n' 'Planned actions:'
printf '%s\n' '  - materialize LFS data for the four official function packages'
printf '%s\n' '  - create or reuse a Python 3.12 venv without touching system Python'
printf '%s\n' '  - install eight local packages editable with fully resolved constraints'
printf '%s\n' '  - record the complete resolved package set inside the venv'
printf '%s\n' '  - run the existing separate Kit Lab setup script'
printf '%s\n' '  - validate imports, versions, entrypoints, configs, and LFS data'

if "$dry_run"; then
    printf 'DRY RUN: no environment, package, or LFS changes were made.\n'
    exit 0
fi

if ! "$assume_yes" && [ -t 0 ]; then
    printf 'Continue? [y/N] '
    read -r answer
    case "$answer" in
        y|Y|yes|YES) ;;
        *)
            printf 'Setup cancelled.\n'
            exit 0
            ;;
    esac
elif ! "$assume_yes"; then
    printf 'ERROR: noninteractive setup requires --yes (or use --dry-run).\n' >&2
    exit 64
fi

cd "$repo_root"
lfs_needs_pull=false
while IFS= read -r lfs_path; do
    case "$lfs_path" in
        source/aiq/omni_ui_fns/*|source/aiq/kit_fns/*|source/aiq/usd_code_fns/*|source/aiq/isaacsim_fns/*)
            if [ ! -f "$lfs_path" ] \
                || head -c 80 "$lfs_path" | rg -q '^version https://git-lfs.github.com/spec/v1'; then
                lfs_needs_pull=true
                break
            fi
            ;;
    esac
done < <(git lfs ls-files -n)
if "$lfs_needs_pull"; then
    lfs_command=(git)
    if [ -n "$lfs_url" ]; then
        lfs_command+=(-c "lfs.url=$lfs_url")
    fi
    "${lfs_command[@]}" lfs pull \
        --include='source/aiq/omni_ui_fns/**,source/aiq/kit_fns/**,source/aiq/usd_code_fns/**,source/aiq/isaacsim_fns/**' \
        --exclude=''
else
    printf 'Relevant Git LFS data is already materialized; skipping remote fetch.\n'
fi

if ! "$uv_bin" python find 3.12 >/dev/null 2>&1; then
    "$uv_bin" python install 3.12
fi

if [ -e "$venv" ] && [ ! -x "$venv/bin/python" ]; then
    printf 'ERROR: %s exists but is not a usable venv; it was left unchanged.\n' "$venv" >&2
    exit 73
fi
if [ ! -x "$venv/bin/python" ]; then
    mkdir -p "$(dirname -- "$venv")"
    "$uv_bin" venv --python 3.12 "$venv"
fi

resolved_constraints="$venv/.kit-usd-mcp-resolved.txt"
base_constraints="$repo_root/requirements-mcps-user-local.txt"
constraints="$base_constraints"
if [ -f "$resolved_constraints" ] \
    && [ ! -L "$resolved_constraints" ] \
    && [ "$(stat -c %u -- "$resolved_constraints" 2>/dev/null)" = "$(id -u)" ]; then
    constraints="$resolved_constraints"
fi

"$uv_bin" pip install \
    --python "$venv/bin/python" \
    --constraints "$constraints" \
    --editable "$repo_root/source/aiq/omni_ui_fns" \
    --editable "$repo_root/source/aiq/kit_fns" \
    --editable "$repo_root/source/aiq/usd_code_fns" \
    --editable "$repo_root/source/aiq/isaacsim_fns" \
    --editable "$repo_root/source/mcp/omni_ui_mcp" \
    --editable "$repo_root/source/mcp/kit_mcp" \
    --editable "$repo_root/source/mcp/usd_code_mcp" \
    --editable "$repo_root/source/mcp/isaacsim_mcp"
"$uv_bin" pip check --python "$venv/bin/python"

resolved_tmp="$venv/.kit-usd-mcp-resolved.txt.tmp.$$"
trap 'rm -f -- "$resolved_tmp"' EXIT
"$uv_bin" pip freeze --exclude-editable --python "$venv/bin/python" >"$resolved_tmp"
chmod 600 "$resolved_tmp"
mv -f -- "$resolved_tmp" "$resolved_constraints"
trap - EXIT

"$repo_root/source/mcp/khl_kit_lab_mcp/setup-user-local.sh"
KIT_USD_MCP_VENV="$venv" \
    "$venv/bin/python" "$repo_root/verify-mcps-user-local.py" \
    --environment-only \
    --venv "$venv"

printf '\nSetup complete. Start the stack with:\n  %s/manage-mcps-user-local.sh start\n' "$repo_root"
printf 'Then verify it with:\n  %s/manage-mcps-user-local.sh verify\n' "$repo_root"
