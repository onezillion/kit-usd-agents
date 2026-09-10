#!/usr/bin/env bash
# KHL Kit Lab MCP — experiment-record operator recovery (operator-only).
#
# This wrapper exists ONLY for explicit operator recovery when an authorized
# manager/operator cannot perform a normal tool call through the agent surface.
# It is NOT an allowlist bypass; read-only reviewers must NEVER use shell
# execution to gain experiment mutation. Use it only when the task explicitly
# authorizes experiment recovery AND the shell/process authority is available.
#
# It calls ONLY normal MCP tools against http://127.0.0.1:9910/mcp:
#   kit_experiment_current, kit_experiment_get, kit_experiment_start,
#   kit_experiment_finish.
# It never manipulates manifest.json, events.jsonl, summary.md, or current.json
# directly.
#
# Usage:
#   experiment-user-local.sh current
#   experiment-user-local.sh get <experiment-id>
#   experiment-user-local.sh start <title> <objective> <tag[,tag,...]>
#   experiment-user-local.sh finish --expect-id <experiment-id> <outcome> "<summary>"
#
# Exactly one bounded operation per invocation. No free-form note tool is exposed;
# experiment records already gather substantive MCP operations automatically.
#
# Exit codes:
#   0 — success
#   nonzero — wrong/inactive state, MCP failure, malformed result, usage error, mismatch
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${HOME}/kit-ai/venvs/khl-kit-lab-mcp/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "experiment-user-local: missing Kit Lab MCP interpreter: $python_bin" >&2
  exit 127
fi

if [[ $# -lt 1 ]]; then
  echo "usage: $0 {current|get <experiment-id>|start <title> <objective> <tag[,tag,...]>|finish --expect-id <experiment-id> <outcome> \"<summary>\"}" >&2
  exit 2
fi
cmd="$1"
shift

case "$cmd" in
  current)
    if [[ $# -ne 0 ]]; then
      echo "usage: $0 current" >&2
      exit 2
    fi
    mode="current"
    ;;
  get)
    if [[ $# -ne 1 ]]; then
      echo "usage: $0 get <experiment-id>" >&2
      exit 2
    fi
    mode="get"
    expect_id="$1"
    ;;
  start)
    if [[ $# -ne 3 ]]; then
      echo "usage: $0 start <title> <objective> <tag[,tag,...]>" >&2
      exit 2
    fi
    mode="start"
    title="$1"
    objective="$2"
    tags="$3"
    ;;
  finish)
    if [[ $# -ne 4 ]] || [[ "$1" != "--expect-id" ]]; then
      echo "usage: $0 finish --expect-id <experiment-id> <outcome> \"<summary>\"" >&2
      exit 2
    fi
    mode="finish"
    expect_id="$2"
    outcome="$3"
    summary="$4"
    ;;
  *)
    echo "experiment-user-local: unknown subcommand: $cmd" >&2
    exit 2
    ;;
esac

export KHL_EXP_MODE="$mode"
export KHL_EXP_EXPECT_ID="${expect_id:-}"
export KHL_EXP_OUTCOME="${outcome:-}"
export KHL_EXP_SUMMARY="${summary:-}"
export KHL_EXP_TITLE="${title:-}"
export KHL_EXP_OBJECTIVE="${objective:-}"
export KHL_EXP_TAGS="${tags:-}"

exec "$python_bin" - <<'PY'
import asyncio
import json
import os
import sys

from mcp import Client

ENDPOINT = "http://127.0.0.1:9910/mcp"
OUTCOMES = {"success", "failed", "inconclusive", "cancelled"}


def _fail(kind, detail):
    print(f"experiment-user-local: {kind}: {detail}", file=sys.stderr)
    sys.exit(1)


def _payload(result):
    data = getattr(result, "structured_content", None) or {}
    if not isinstance(data, dict):
        _fail("malformed_result", f"structured_content has wrong type: {type(data)}")
    return data


MAX_TITLE_CHARS = 200
MAX_OBJECTIVE_CHARS = 4_000
MAX_TAGS = 20
MAX_TAG_CHARS = 64
MAX_SUMMARY_CHARS = 20_000
MAX_EXPERIMENT_ID_CHARS = 96


async def main():
    mode = os.environ["KHL_EXP_MODE"]
    expect_id = os.environ.get("KHL_EXP_EXPECT_ID", "")
    outcome = os.environ.get("KHL_EXP_OUTCOME", "")
    summary = os.environ.get("KHL_EXP_SUMMARY", "")
    title = os.environ.get("KHL_EXP_TITLE", "")
    objective = os.environ.get("KHL_EXP_OBJECTIVE", "")
    raw_tags = os.environ.get("KHL_EXP_TAGS", "")

    if mode in {"get", "finish"}:
        if not expect_id:
            _fail("usage", f"{mode} requires <experiment-id>")
        if len(expect_id) > MAX_EXPERIMENT_ID_CHARS:
            _fail("usage", f"experiment ID exceeds {MAX_EXPERIMENT_ID_CHARS} characters")
    if mode == "start":
        if not title.strip() or not objective.strip():
            _fail("usage", "start requires non-empty <title> and <objective>")
        if len(title) > MAX_TITLE_CHARS:
            _fail("usage", f"title exceeds {MAX_TITLE_CHARS} characters")
        if len(objective) > MAX_OBJECTIVE_CHARS:
            _fail("usage", f"objective exceeds {MAX_OBJECTIVE_CHARS} characters")
        tags = [tag.strip() for tag in raw_tags.split(",") if tag.strip()]
        tags = list(dict.fromkeys(tags))
        if len(tags) > MAX_TAGS:
            _fail("usage", f"tags exceed {MAX_TAGS} items")
        if any(len(tag) > MAX_TAG_CHARS for tag in tags):
            _fail("usage", f"a tag exceeds {MAX_TAG_CHARS} characters")
    else:
        tags = []
    if mode == "finish":
        if outcome not in OUTCOMES:
            _fail("usage", f"outcome must be one of: {sorted(OUTCOMES)}")
        if not summary.strip():
            _fail("usage", "summary must be non-empty")
        if len(summary) > MAX_SUMMARY_CHARS:
            _fail("usage", f"summary exceeds {MAX_SUMMARY_CHARS} characters")

    try:
        async with Client(ENDPOINT) as client:
            if mode == "current":
                data = _payload(await client.call_tool("kit_experiment_current", {}))
                print(json.dumps(data, indent=2, ensure_ascii=False))
                return

            if mode == "get":
                data = _payload(
                    await client.call_tool(
                        "kit_experiment_get", {"experiment_id": expect_id, "event_limit": 20}
                    )
                )
                print(json.dumps(data, indent=2, ensure_ascii=False))
                return

            if mode == "start":
                data = _payload(
                    await client.call_tool(
                        "kit_experiment_start",
                        {"title": title, "objective": objective, "tags": tags},
                    )
                )
                if not data.get("active"):
                    _fail("mcp_or_state_failure", f"start did not activate an experiment: {data!r}")
                print(json.dumps(data, indent=2, ensure_ascii=False))
                return

            # finish
            data = _payload(
                await client.call_tool(
                    "kit_experiment_finish",
                    {
                        "summary": summary,
                        "outcome": outcome,
                        "experiment_id": expect_id,
                    },
                )
            )
            exp = data.get("experiment") or {}
            if exp.get("status") != "finished":
                _fail("mismatch_or_inactive", f"experiment not finished: {data!r}")
            if exp.get("experiment_id") != expect_id:
                _fail("mismatch_or_inactive", f"refused without mutation on expected-id mismatch: {data!r}")
            print(json.dumps(data, indent=2, ensure_ascii=False))
    except SystemExit:
        raise
    except Exception as exc:
        _fail("mcp_or_state_failure", str(exc))


asyncio.run(main())
PY
