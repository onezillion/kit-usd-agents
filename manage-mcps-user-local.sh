#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
nvidia_venv="${KIT_USD_MCP_VENV:-/home/ubuntu/kit-ai/venvs/kit-usd-mcp}"
kit_lab_venv="${KHL_KIT_LAB_MCP_VENV:-/home/ubuntu/kit-ai/venvs/khl-kit-lab-mcp}"
state_root="${KIT_USD_MCP_STATE_ROOT:-/home/ubuntu/kit-ai}"
run_root="${KIT_USD_MCP_RUN_DIR:-$state_root/run/kit-usd-mcps}"
log_root="${KIT_USD_MCP_LOG_DIR:-$state_root/logs/kit-usd-mcps}"
startup_timeout="${KIT_USD_MCP_STARTUP_TIMEOUT:-90}"
shutdown_timeout="${KIT_USD_MCP_SHUTDOWN_TIMEOUT:-15}"
probe_timeout="${KIT_USD_MCP_PROBE_TIMEOUT:-8}"
semantic=false
full_kit_lab=false
setup_yes=false
setup_dry_run=false
command_name=""
requested_services=()
newly_started=()
starting=false
inflight_service=""
inflight_pid=""
inflight_token=""

services=(omni-ui kit usd-code isaacsim kit-lab)
declare -A display_names=(
    [omni-ui]="OmniUI MCP"
    [kit]="NVIDIA Kit MCP"
    [usd-code]="USD Code MCP"
    [isaacsim]="Isaac Sim MCP"
    [kit-lab]="KHL Kit Lab MCP"
)
declare -A ports=(
    [omni-ui]=9901
    [kit]=9902
    [usd-code]=9903
    [isaacsim]=9904
    [kit-lab]=9910
)
declare -A wrappers=(
    [omni-ui]="$repo_root/source/mcp/omni_ui_mcp/run-user-local.sh"
    [kit]="$repo_root/source/mcp/kit_mcp/run-user-local.sh"
    [usd-code]="$repo_root/source/mcp/usd_code_mcp/run-user-local.sh"
    [isaacsim]="$repo_root/source/mcp/isaacsim_mcp/run-user-local.sh"
    [kit-lab]="$repo_root/source/mcp/khl_kit_lab_mcp/run-user-local.sh"
)
declare -A commands=(
    [omni-ui]="omni-ui-aiq"
    [kit]="kit-mcp"
    [usd-code]="usd-code-mcp"
    [isaacsim]="isaacsim-mcp"
)

usage() {
    cat <<'EOF'
Usage: ./manage-mcps-user-local.sh [OPTIONS] COMMAND [SERVICE ...]

Commands:
  setup     Delegate to setup-mcps-user-local.sh
  start     Start all or selected services
  stop      Stop all or selected manager-owned services
  restart   Restart all or selected manager-owned services
  status    Classify all or selected services
  verify    Verify MCP metadata and deterministic read-only calls

Services: omni-ui kit usd-code isaacsim kit-lab (default: all)

Options:
  --venv PATH          Override the shared NVIDIA venv
  --semantic           Include semantic-search calls during verify
  --full-kit-lab       Also run Kit Lab's mutating experiment verification
  --yes                Noninteractive setup confirmation
  --dry-run            Show setup actions without changing anything
  -h, --help           Show this help
EOF
}

is_positive_integer() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
        0) return 1 ;;
        *) return 0 ;;
    esac
}

for value in "$startup_timeout" "$shutdown_timeout" "$probe_timeout"; do
    if ! is_positive_integer "$value"; then
        printf 'ERROR: timeout values must be positive integers.\n' >&2
        exit 64
    fi
done

while [ "$#" -gt 0 ]; do
    case "$1" in
        setup|start|stop|restart|status|verify)
            if [ -n "$command_name" ]; then
                printf 'ERROR: multiple commands were supplied.\n' >&2
                exit 64
            fi
            command_name="$1"
            shift
            ;;
        --venv)
            if [ "$#" -lt 2 ]; then
                printf 'ERROR: --venv requires a path.\n' >&2
                exit 64
            fi
            nvidia_venv="$2"
            shift 2
            ;;
        --semantic)
            semantic=true
            shift
            ;;
        --full-kit-lab)
            full_kit_lab=true
            shift
            ;;
        --yes|-y)
            setup_yes=true
            shift
            ;;
        --dry-run)
            setup_dry_run=true
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --*)
            printf 'ERROR: unknown option: %s\n' "$1" >&2
            exit 64
            ;;
        *)
            requested_services+=("$1")
            shift
            ;;
    esac
done

if [ -z "$command_name" ]; then
    usage >&2
    exit 64
fi
if [ "${#requested_services[@]}" -eq 0 ]; then
    requested_services=("${services[@]}")
fi
for service in "${requested_services[@]}"; do
    if [ -z "${ports[$service]+set}" ]; then
        printf 'ERROR: unknown service: %s\n' "$service" >&2
        exit 64
    fi
done

case "$nvidia_venv" in
    /*) ;;
    *)
        printf 'ERROR: --venv must be an absolute path.\n' >&2
        exit 64
        ;;
esac

resolved_repo="$(realpath -m -- "$repo_root")"
resolved_run="$(realpath -m -- "$run_root")"
resolved_log="$(realpath -m -- "$log_root")"
case "$resolved_run/" in
    "$resolved_repo/"*)
        printf 'ERROR: runtime state must remain outside the repository: %s\n' "$run_root" >&2
        exit 64
        ;;
esac
case "$resolved_log/" in
    "$resolved_repo/"*)
        printf 'ERROR: logs must remain outside the repository: %s\n' "$log_root" >&2
        exit 64
        ;;
esac
run_root="$resolved_run"
log_root="$resolved_log"

probe_python=""
choose_probe_python() {
    local candidate
    for candidate in "$nvidia_venv/bin/python" "$kit_lab_venv/bin/python" /usr/bin/python3; do
        if [ -x "$candidate" ] && "$candidate" -c 'import mcp' >/dev/null 2>&1; then
            probe_python="$candidate"
            return 0
        fi
    done
    return 1
}

load_credentials() {
    local env_file="$repo_root/source/mcp/.env"
    if [ -f "$env_file" ]; then
        set -a
        # shellcheck disable=SC1090
        . "$env_file"
        set +a
    fi
}

ensure_runtime_dirs() {
    umask 077
    mkdir -p "$run_root" "$log_root"
    chmod 700 "$run_root" "$log_root"
}

state_file_for() {
    printf '%s/%s.state' "$run_root" "$1"
}

log_file_for() {
    printf '%s/%s.log' "$log_root" "$1"
}

state_pid=""
state_pgid=""
state_start_ticks=""
state_token=""
state_service=""
state_port=""

load_state() {
    local service="$1"
    local path
    local key
    local value
    path="$(state_file_for "$service")"
    state_pid=""
    state_pgid=""
    state_start_ticks=""
    state_token=""
    state_service=""
    state_port=""
    if [ ! -e "$path" ]; then
        return 1
    fi
    if [ ! -f "$path" ] || [ -L "$path" ] || [ "$(stat -c %u -- "$path" 2>/dev/null)" != "$(id -u)" ]; then
        return 2
    fi
    while IFS='=' read -r key value; do
        case "$key" in
            pid) state_pid="$value" ;;
            pgid) state_pgid="$value" ;;
            start_ticks) state_start_ticks="$value" ;;
            token) state_token="$value" ;;
            service) state_service="$value" ;;
            port) state_port="$value" ;;
            *) return 2 ;;
        esac
    done <"$path"
    if ! is_positive_integer "$state_pid" \
        || ! is_positive_integer "$state_pgid" \
        || ! is_positive_integer "$state_start_ticks" \
        || ! is_positive_integer "$state_port"; then
        return 2
    fi
    case "$state_token" in
        ''|*[!A-Za-z0-9_-]*) return 2 ;;
    esac
    if [ "$state_service" != "$service" ] || [ "$state_port" != "${ports[$service]}" ]; then
        return 2
    fi
    return 0
}

process_start_ticks() {
    local pid="$1"
    local stat_line
    local after_comm
    if ! IFS= read -r stat_line <"/proc/$pid/stat"; then
        return 1
    fi
    after_comm="${stat_line##*) }"
    # Field 22 is field 20 after removing pid and comm.
    awk '{print $20}' <<<"$after_comm"
}

pid_has_token() {
    local pid="$1"
    local token="$2"
    [ -r "/proc/$pid/environ" ] || return 1
    tr '\0' '\n' <"/proc/$pid/environ" | rg -F -x -q "KIT_USD_MCP_OWNER_TOKEN=$token"
}

group_ownership() {
    local pgid="$1"
    local token="$2"
    local pid
    local found=false
    while read -r pid; do
        [ -n "$pid" ] || continue
        found=true
        if ! pid_has_token "$pid" "$token"; then
            return 2
        fi
    done < <(ps -eo pid=,pgid= | awk -v wanted="$pgid" '$2 == wanted {print $1}')
    "$found" || return 1
    return 0
}

state_group_owned() {
    local current_ticks
    if [ -r "/proc/$state_pid/stat" ]; then
        current_ticks="$(process_start_ticks "$state_pid")" || return 2
        [ "$current_ticks" = "$state_start_ticks" ] || return 2
    fi
    group_ownership "$state_pgid" "$state_token"
}

remove_validated_state() {
    local service="$1"
    local path
    path="$(state_file_for "$service")"
    if [ -f "$path" ] && [ ! -L "$path" ] && [ "$(stat -c %u -- "$path" 2>/dev/null)" = "$(id -u)" ]; then
        rm -f -- "$path"
    else
        printf 'ERROR: refusing to remove unvalidated state file: %s\n' "$path" >&2
        return 1
    fi
}

write_state() {
    local service="$1"
    local path
    local temporary
    path="$(state_file_for "$service")"
    temporary="$path.tmp.$$"
    umask 077
    {
        printf 'service=%s\n' "$service"
        printf 'port=%s\n' "${ports[$service]}"
        printf 'pid=%s\n' "$state_pid"
        printf 'pgid=%s\n' "$state_pgid"
        printf 'start_ticks=%s\n' "$state_start_ticks"
        printf 'token=%s\n' "$state_token"
    } >"$temporary"
    chmod 600 "$temporary"
    mv -f -- "$temporary" "$path"
}

port_available() {
    /usr/bin/python3 - "${ports[$1]}" <<'PY'
import socket
import sys

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    # Match uvicorn's normal restart behavior: allow sockets in TIME_WAIT while
    # still refusing an address with an active listener.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
}

probe_service() {
    local service="$1"
    if [ -z "$probe_python" ] && ! choose_probe_python; then
        return 69
    fi
    "$probe_python" "$repo_root/verify-mcps-user-local.py" \
        --service "$service" \
        --probe \
        --timeout "$probe_timeout" >/dev/null 2>&1
}

kit_lab_bridge_status() {
    local output
    if ! output="$($probe_python "$repo_root/verify-mcps-user-local.py" --service kit-lab --timeout "$probe_timeout" --json 2>/dev/null)"; then
        return 1
    fi
    VERIFY_JSON="$output" "$probe_python" - <<'PY'
import json
import os

report = json.loads(os.environ["VERIFY_JSON"])
call = report["services"][0].get("read_only_call", {})
raise SystemExit(0 if call.get("status") == "passed" else 2)
PY
}

service_prerequisites() {
    local service="$1"
    if [ ! -x "${wrappers[$service]}" ]; then
        printf 'ERROR: wrapper is not executable: %s\n' "${wrappers[$service]}" >&2
        return 69
    fi
    if [ "$service" = "kit-lab" ]; then
        if [ ! -x "$kit_lab_venv/bin/python" ]; then
            printf 'ERROR: Kit Lab environment is missing; run setup.\n' >&2
            return 69
        fi
    elif [ ! -x "$nvidia_venv/bin/${commands[$service]}" ]; then
        printf 'ERROR: %s is missing from %s; run setup.\n' "${commands[$service]}" "$nvidia_venv" >&2
        return 69
    fi
}

stop_loaded_group() {
    local service="$1"
    local waited=0
    if ! state_group_owned; then
        printf '%s: refusing to signal a process group that is not manager-owned.\n' "$service" >&2
        return 1
    fi
    kill -TERM -- "-$state_pgid" 2>/dev/null || true
    while group_ownership "$state_pgid" "$state_token" >/dev/null 2>&1; do
        if [ "$waited" -ge "$shutdown_timeout" ]; then
            if ! group_ownership "$state_pgid" "$state_token"; then
                printf '%s: ownership changed during shutdown; refusing KILL.\n' "$service" >&2
                return 1
            fi
            printf '%s: TERM timeout after %ss; escalating manager-owned group to KILL.\n' "$service" "$shutdown_timeout" >&2
            kill -KILL -- "-$state_pgid" 2>/dev/null || true
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done
    waited=0
    while group_ownership "$state_pgid" "$state_token" >/dev/null 2>&1 && [ "$waited" -lt 5 ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if group_ownership "$state_pgid" "$state_token" >/dev/null 2>&1; then
        printf '%s: manager-owned process group did not exit.\n' "$service" >&2
        return 1
    fi
    remove_validated_state "$service"
}

start_one() {
    local service="$1"
    local load_rc
    local ownership_rc
    local waited
    local log_file
    LAST_START_NEW=false
    load_state "$service"
    load_rc=$?
    if [ "$load_rc" -eq 0 ]; then
        state_group_owned
        ownership_rc=$?
        if [ "$ownership_rc" -eq 0 ]; then
            if probe_service "$service"; then
                printf '%s: already healthy (manager-owned).\n' "$service"
                return 0
            fi
            printf '%s: process group is alive but its MCP endpoint is unhealthy; use restart.\n' "$service" >&2
            return 1
        fi
        if [ "$ownership_rc" -eq 1 ]; then
            printf '%s: removing validated stale manager state.\n' "$service"
            remove_validated_state "$service" || return 1
        else
            printf '%s: stale state points at a non-owned process; refusing automatic removal.\n' "$service" >&2
            return 1
        fi
    elif [ "$load_rc" -eq 2 ]; then
        printf '%s: invalid or unsafe manager state; refusing automatic removal.\n' "$service" >&2
        return 1
    fi

    if ! port_available "$service"; then
        printf '%s: port %s is occupied by an unowned process; refusing to start.\n' "$service" "${ports[$service]}" >&2
        return 1
    fi
    service_prerequisites "$service" || return $?
    ensure_runtime_dirs
    state_token="$(tr -d '-' </proc/sys/kernel/random/uuid)"
    inflight_service="$service"
    inflight_token="$state_token"
    log_file="$(log_file_for "$service")"
    KIT_USD_MCP_OWNER_TOKEN="$state_token" \
    KIT_USD_MCP_VENV="$nvidia_venv" \
    KIT_USD_MCP_STATE_ROOT="$state_root" \
        /usr/bin/setsid /usr/bin/bash -c \
        'set -o pipefail; "$1" 2>&1 | "$2" "$3"' \
        _ "${wrappers[$service]}" "$repo_root/mcp-operational-log.py" "$log_file" &
    state_pid=$!
    state_pgid="$state_pid"
    inflight_pid="$state_pid"
    waited=0
    while [ ! -r "/proc/$state_pid/stat" ] && [ "$waited" -lt 5 ]; do
        sleep 1
        waited=$((waited + 1))
    done
    state_start_ticks="$(process_start_ticks "$state_pid")" || {
        printf '%s: launcher exited before state could be recorded; see %s.\n' "$service" "$log_file" >&2
        inflight_service=""
        inflight_pid=""
        inflight_token=""
        return 1
    }
    state_service="$service"
    state_port="${ports[$service]}"
    write_state "$service"

    waited=0
    while [ "$waited" -lt "$startup_timeout" ]; do
        if ! group_ownership "$state_pgid" "$state_token" >/dev/null 2>&1; then
            printf '%s: process exited during startup; see %s.\n' "$service" "$log_file" >&2
            remove_validated_state "$service" || true
            inflight_service=""
            inflight_pid=""
            inflight_token=""
            return 1
        fi
        if probe_service "$service"; then
            printf '%s: started and healthy on 127.0.0.1:%s.\n' "$service" "${ports[$service]}"
            LAST_START_NEW=true
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    printf '%s: startup timed out after %ss; rolling back this service.\n' "$service" "$startup_timeout" >&2
    stop_loaded_group "$service" || true
    inflight_service=""
    inflight_pid=""
    inflight_token=""
    return 1
}

stop_one() {
    local service="$1"
    local load_rc
    local ownership_rc
    load_state "$service"
    load_rc=$?
    if [ "$load_rc" -eq 1 ]; then
        if port_available "$service"; then
            printf '%s: already stopped.\n' "$service"
            return 0
        fi
        printf '%s: port %s is occupied but no manager state exists; no signal sent.\n' "$service" "${ports[$service]}" >&2
        return 1
    fi
    if [ "$load_rc" -eq 2 ]; then
        printf '%s: invalid or unsafe manager state; no signal sent.\n' "$service" >&2
        return 1
    fi
    state_group_owned
    ownership_rc=$?
    if [ "$ownership_rc" -eq 1 ]; then
        printf '%s: removing validated stale manager state; process is already stopped.\n' "$service"
        remove_validated_state "$service"
        return $?
    fi
    if [ "$ownership_rc" -ne 0 ]; then
        printf '%s: state does not identify a wholly manager-owned group; no signal sent.\n' "$service" >&2
        return 1
    fi
    if stop_loaded_group "$service"; then
        printf '%s: stopped.\n' "$service"
        return 0
    fi
    return 1
}

status_one() {
    local service="$1"
    local load_rc
    local ownership_rc
    load_state "$service"
    load_rc=$?
    if [ "$load_rc" -eq 2 ]; then
        printf '%-10s STALE_MANAGER_STATE (invalid or unsafe state file)\n' "$service"
        return 1
    fi
    if [ "$load_rc" -eq 1 ]; then
        if port_available "$service"; then
            printf '%-10s MISSING_STOPPED\n' "$service"
        else
            printf '%-10s PORT_OCCUPIED_UNOWNED (127.0.0.1:%s)\n' "$service" "${ports[$service]}"
            return 1
        fi
        return 0
    fi
    state_group_owned
    ownership_rc=$?
    if [ "$ownership_rc" -eq 1 ]; then
        printf '%-10s STALE_MANAGER_STATE (recorded process group is gone)\n' "$service"
        return 1
    fi
    if [ "$ownership_rc" -ne 0 ]; then
        printf '%-10s STALE_MANAGER_STATE (ownership validation failed)\n' "$service"
        return 1
    fi
    if ! probe_service "$service"; then
        printf '%-10s PROCESS_ALIVE_ENDPOINT_UNHEALTHY\n' "$service"
        return 1
    fi
    if [ "$service" = "kit-lab" ]; then
        kit_lab_bridge_status
        case "$?" in
            0)
                printf '%-10s HEALTHY (MCP endpoint and Kit runtime bridge)\n' "$service"
                ;;
            2)
                printf '%-10s EXTERNAL_DEPENDENCY_UNAVAILABLE (MCP healthy; Kit runtime bridge unavailable)\n' "$service"
                return 1
                ;;
            *)
                printf '%-10s HEALTHY (MCP endpoint; bridge probe inconclusive)\n' "$service"
                return 1
                ;;
        esac
    else
        printf '%-10s HEALTHY\n' "$service"
    fi
}

rollback_new_services() {
    local index
    starting=false
    if [ "${#newly_started[@]}" -eq 0 ]; then
        return
    fi
    printf 'Rolling back services started by this invocation...\n' >&2
    for ((index=${#newly_started[@]} - 1; index >= 0; index--)); do
        stop_one "${newly_started[$index]}" || true
    done
    newly_started=()
}

startup_interrupted() {
    printf 'Startup interrupted.\n' >&2
    if [ -n "$inflight_service" ]; then
        if load_state "$inflight_service" && state_group_owned; then
            stop_loaded_group "$inflight_service" || true
        elif [ -n "$inflight_pid" ] \
            && [ -n "$inflight_token" ] \
            && group_ownership "$inflight_pid" "$inflight_token"; then
            kill -TERM -- "-$inflight_pid" 2>/dev/null || true
            for cleanup_wait in 1 2 3 4 5; do
                group_ownership "$inflight_pid" "$inflight_token" >/dev/null 2>&1 || break
                sleep 1
            done
            if group_ownership "$inflight_pid" "$inflight_token"; then
                kill -KILL -- "-$inflight_pid" 2>/dev/null || true
            fi
        fi
        inflight_service=""
        inflight_pid=""
        inflight_token=""
    fi
    rollback_new_services
    exit 130
}

start_selected() {
    local service
    starting=true
    trap startup_interrupted INT TERM
    for service in "${requested_services[@]}"; do
        if ! start_one "$service"; then
            rollback_new_services
            trap - INT TERM
            return 1
        fi
        if "$LAST_START_NEW"; then
            newly_started+=("$service")
        fi
        inflight_service=""
        inflight_pid=""
        inflight_token=""
    done
    starting=false
    trap - INT TERM
    return 0
}

stop_selected() {
    local index
    local result=0
    for ((index=${#requested_services[@]} - 1; index >= 0; index--)); do
        stop_one "${requested_services[$index]}" || result=1
    done
    return "$result"
}

verify_selected() {
    local args=()
    local service
    local report_path
    if ! choose_probe_python; then
        printf 'ERROR: no Python environment with the MCP SDK is available; run setup.\n' >&2
        return 69
    fi
    ensure_runtime_dirs
    load_credentials
    for service in "${requested_services[@]}"; do
        args+=(--service "$service")
    done
    if "$semantic"; then
        args+=(--semantic)
    fi
    report_path="$log_root/verification-last.json"
    if ! "$probe_python" "$repo_root/verify-mcps-user-local.py" \
        "${args[@]}" \
        --report "$report_path"; then
        return 1
    fi
    printf 'Metadata report: %s\n' "$report_path"
    if "$full_kit_lab"; then
        printf '%s\n' 'NOTICE: Kit Lab full verification creates an experiment and executes Kit Python (2 + 2).'
        "$repo_root/source/mcp/khl_kit_lab_mcp/verify-user-local.sh" --full || return 1
    fi
}

case "$command_name" in
    setup)
        setup_args=(--venv "$nvidia_venv")
        "$setup_yes" && setup_args+=(--yes)
        "$setup_dry_run" && setup_args+=(--dry-run)
        exec "$repo_root/setup-mcps-user-local.sh" "${setup_args[@]}"
        ;;
    start)
        export KIT_USD_MCP_VENV="$nvidia_venv"
        start_selected
        exit $?
        ;;
    stop)
        stop_selected
        exit $?
        ;;
    restart)
        stop_selected || exit $?
        newly_started=()
        export KIT_USD_MCP_VENV="$nvidia_venv"
        start_selected
        exit $?
        ;;
    status)
        choose_probe_python || true
        result=0
        for service in "${requested_services[@]}"; do
            status_one "$service" || result=1
        done
        exit "$result"
        ;;
    verify)
        verify_selected
        exit $?
        ;;
esac
