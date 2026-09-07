#!/usr/bin/env python3
"""Verify the repository-owned user-local MCP stack without logging tool inputs."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parent
INVENTORY_PATH = REPO_ROOT / "mcp-services-user-local.json"
DEFAULT_NVIDIA_VENV = Path("/home/ubuntu/kit-ai/venvs/kit-usd-mcp")
LFS_PREFIXES = (
    "source/aiq/omni_ui_fns/",
    "source/aiq/kit_fns/",
    "source/aiq/usd_code_fns/",
    "source/aiq/isaacsim_fns/",
)
FUNCTION_PACKAGES = {
    "omni-ui-fns": "0.6.0",
    "kit-fns": "0.6.0",
    "usd-code-aiq": "0.3.0",
    "isaacsim-fns": "2.0.1",
}
IMPORTS = (
    "nat",
    "omni_ui_mcp",
    "omni_ui_fns",
    "kit_mcp",
    "kit_fns",
    "usd_code_mcp",
    "omni_aiq_usd_code",
    "isaacsim_mcp",
    "isaacsim_fns",
)
CONFIG_PATHS = (
    "source/mcp/omni_ui_mcp/workflow/config.yaml",
    "source/mcp/kit_mcp/workflows/config.yaml",
    "source/mcp/kit_mcp/workflows/local_config.yaml",
    "source/mcp/usd_code_mcp/workflow/config.yaml",
    "source/mcp/usd_code_mcp/workflow/local_config.yaml",
    "source/mcp/isaacsim_mcp/workflows/config.yaml",
)


class VerificationFailure(RuntimeError):
    """A classified verification failure."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def load_inventory() -> dict[str, Any]:
    with INVENTORY_PATH.open(encoding="utf-8") as stream:
        inventory = json.load(stream)
    if inventory.get("schema_version") != 1:
        raise VerificationFailure("inventory", "Unsupported service inventory schema")
    return inventory


def service_map(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {service["id"]: service for service in inventory["services"]}


def model_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return value
    return {}


def tool_schema(tool: Any) -> dict[str, Any]:
    data = model_dict(tool)
    schema = data.get("inputSchema", {})
    return schema if isinstance(schema, dict) else {}


def validate_call_against_live_schema(tool: Any, arguments: dict[str, Any]) -> None:
    schema = tool_schema(tool)
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if schema.get("type") != "object" or not isinstance(properties, dict):
        raise VerificationFailure("schema", f"{tool.name}: input schema is not an object schema")
    missing = sorted(set(required) - set(arguments))
    unexpected = sorted(set(arguments) - set(properties))
    if missing or unexpected:
        raise VerificationFailure(
            "schema",
            f"{tool.name}: deterministic arguments do not match live schema "
            f"(missing={missing}, unexpected={unexpected})",
        )


def result_summary(result: Any) -> dict[str, Any]:
    data = model_dict(result)
    is_error = bool(data.get("isError", False))
    content = data.get("content", [])
    text_chars = 0
    error_text = ""
    for item in content if isinstance(content, list) else []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            text_chars += len(item["text"])
            if not error_text and item["text"].lstrip().upper().startswith("ERROR"):
                error_text = item["text"]
    return {
        "is_error": is_error,
        "content_items": len(content) if isinstance(content, list) else 0,
        "text_characters": text_chars,
        "structured_result": isinstance(data.get("structuredContent"), dict),
        "returned_error_text": bool(error_text),
    }


def classify_dependency_error(result: Any) -> str:
    data = model_dict(result)
    text = json.dumps(data.get("content", []), ensure_ascii=True).lower()
    if "401" in text or "403" in text or "unauthorized" in text or "api key" in text:
        return "cloud_authentication"
    if "rerank" in text:
        return "reranker"
    if "embed" in text:
        return "embedder"
    if "connection" in text or "unreachable" in text or "timed out" in text:
        return "external_dependency"
    return "tool_call"


def load_kit_lab_policy() -> Any:
    path = REPO_ROOT / "source/mcp/khl_kit_lab_mcp/src/khl_kit_lab_mcp/policy.py"
    spec = importlib.util.spec_from_file_location("_khl_kit_lab_policy_for_verify", path)
    if spec is None or spec.loader is None:
        raise VerificationFailure("policy", "Cannot load the Kit Lab policy module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_tool_metadata(
    service: dict[str, Any],
    initialized: Any,
    tools: list[Any],
) -> dict[str, Any]:
    declared_annotations = 0
    declared_meta = 0
    for tool in tools:
        data = model_dict(tool)
        description = data.get("description")
        schema = data.get("inputSchema")
        annotations = data.get("annotations") or {}
        metadata = data.get("_meta") or {}
        if not isinstance(description, str) or not description.strip():
            raise VerificationFailure("metadata", f"{tool.name}: description is missing")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise VerificationFailure("metadata", f"{tool.name}: object input schema is missing")
        if annotations:
            declared_annotations += 1
        if metadata:
            declared_meta += 1

    mode = service["upstream_metadata"]
    if mode == "not-declared":
        if model_dict(initialized).get("instructions"):
            raise VerificationFailure(
                "metadata",
                f"{service['id']}: upstream server instructions changed; update policy and inventory",
            )
        if declared_annotations or declared_meta:
            raise VerificationFailure(
                "metadata",
                f"{service['id']}: upstream metadata changed; update policy and inventory",
            )
        return {
            "descriptions": "present",
            "input_schemas": "object",
            "annotations": "not declared upstream",
            "_meta": "not declared upstream",
        }

    policy = load_kit_lab_policy()
    init_data = model_dict(initialized)
    if init_data.get("instructions") != policy.SERVER_INSTRUCTIONS:
        raise VerificationFailure("policy", "kit-lab: server instructions differ from policy")
    for tool in tools:
        data = model_dict(tool)
        expected = policy.TOOL_POLICIES[tool.name]
        if data.get("description") != expected["description"]:
            raise VerificationFailure("policy", f"{tool.name}: description differs from policy")
        annotations = data.get("annotations") or {}
        expected_annotations = {
            "readOnlyHint": expected["annotations"]["read_only_hint"],
            "destructiveHint": expected["annotations"]["destructive_hint"],
            "idempotentHint": expected["annotations"]["idempotent_hint"],
            "openWorldHint": expected["annotations"]["open_world_hint"],
        }
        for key, expected_value in expected_annotations.items():
            if annotations.get(key) != expected_value:
                raise VerificationFailure("policy", f"{tool.name}: annotation {key} differs from policy")
        policy_meta = (data.get("_meta") or {}).get("khl_policy", {})
        if policy_meta.get("class") != expected["class"]:
            raise VerificationFailure("policy", f"{tool.name}: policy class metadata differs")
        if policy_meta.get("fingerprint") != policy.POLICY_FINGERPRINT:
            raise VerificationFailure("policy", f"{tool.name}: policy fingerprint differs")
    return {
        "descriptions": "exact policy match",
        "input_schemas": "object",
        "annotations": f"declared on {declared_annotations} tools",
        "_meta": f"declared on {declared_meta} tools",
        "server_instructions": "exact policy match",
    }


def verify_listener(service: dict[str, Any]) -> str:
    ss_path = shutil.which("ss")
    if not ss_path:
        raise VerificationFailure("listener", "ss is required to verify listener exposure")
    completed = subprocess.run(
        [ss_path, "-H", "-ltn", f"sport = :{service['port']}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise VerificationFailure("listener", f"{service['id']}: listener inspection failed")
    addresses: list[str] = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            addresses.append(fields[3])
    expected = f"127.0.0.1:{service['port']}"
    if addresses != [expected]:
        raise VerificationFailure(
            "listener",
            f"{service['id']}: expected exactly {expected}, observed {addresses}",
        )
    return expected


async def inspect_service(
    service: dict[str, Any],
    *,
    call_read_only: bool,
    semantic: bool,
    timeout: float,
) -> dict[str, Any]:
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise VerificationFailure("environment", "The selected Python environment cannot import MCP") from exc

    try:
        async with asyncio.timeout(timeout):
            async with streamable_http_client(service["endpoint"]) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    initialized = await session.initialize()
                    listed = await session.list_tools()
                    tools = list(listed.tools)
                    names = [tool.name for tool in tools]
                    expected = service["tools"]
                    if set(names) != set(expected) or len(names) != len(expected):
                        raise VerificationFailure(
                            "tool_inventory",
                            f"{service['id']}: tool mismatch; "
                            f"missing={sorted(set(expected) - set(names))}, "
                            f"unexpected={sorted(set(names) - set(expected))}",
                        )
                    metadata = check_tool_metadata(service, initialized, tools)
                    tool_by_name = {tool.name: tool for tool in tools}
                    report: dict[str, Any] = {
                        "id": service["id"],
                        "endpoint": service["endpoint"],
                        "transport": "healthy",
                        "listener": verify_listener(service),
                        "server": model_dict(initialized).get("serverInfo", {}),
                        "instructions_present": bool(model_dict(initialized).get("instructions")),
                        "tool_count": len(names),
                        "tools": names,
                        "metadata": metadata,
                        "tool_metadata": [model_dict(tool) for tool in tools],
                    }

                    if call_read_only:
                        call = service["read_only_call"]
                        live_tool = tool_by_name[call["tool"]]
                        validate_call_against_live_schema(live_tool, call["arguments"])
                        result = await session.call_tool(call["tool"], call["arguments"])
                        summary = result_summary(result)
                        summary["tool"] = call["tool"]
                        if summary["is_error"] or summary["returned_error_text"]:
                            summary["status"] = "external_dependency_unavailable"
                            summary["failure_category"] = classify_dependency_error(result)
                        else:
                            summary["status"] = "passed"
                        report["read_only_call"] = summary

                    if semantic and service.get("semantic_call"):
                        call = service["semantic_call"]
                        live_tool = tool_by_name[call["tool"]]
                        validate_call_against_live_schema(live_tool, call["arguments"])
                        result = await session.call_tool(call["tool"], call["arguments"])
                        summary = result_summary(result)
                        summary["tool"] = call["tool"]
                        if summary["is_error"] or summary["returned_error_text"]:
                            summary["status"] = "failed"
                            summary["failure_category"] = classify_dependency_error(result)
                        else:
                            summary["status"] = "passed"
                        report["semantic_call"] = summary
                    return report
    except VerificationFailure:
        raise
    except TimeoutError as exc:
        raise VerificationFailure("transport", f"{service['id']}: MCP request timed out") from exc
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise VerificationFailure(
            "transport",
            f"{service['id']}: MCP endpoint unavailable ({type(exc).__name__})",
        ) from exc


def check_tcp_dependency(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "invalid_url"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=2):
            return "reachable"
    except OSError:
        return "unreachable"


def backend_status() -> dict[str, str]:
    embedder = os.environ.get("KIT_EMBEDDER_BACKEND", "nvidia_api")
    reranker = os.environ.get("KIT_RERANKER_BACKEND", "nvidia_api")
    result = {"embedder_backend": embedder, "reranker_backend": reranker}
    if embedder == "local":
        url = os.environ.get("KIT_LOCAL_EMBEDDER_URL", "")
        result["embedder_dependency"] = check_tcp_dependency(url) if url else "not_configured"
    else:
        result["embedder_dependency"] = "credentials_present" if os.environ.get("NVIDIA_API_KEY") else "credentials_missing"
    if reranker == "local":
        url = os.environ.get("KIT_LOCAL_RERANKER_URL", "")
        result["reranker_dependency"] = check_tcp_dependency(url) if url else "not_configured"
    else:
        result["reranker_dependency"] = "credentials_present" if os.environ.get("NVIDIA_API_KEY") else "credentials_missing"
    return result


def run_checked(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise VerificationFailure("environment", f"Command failed: {' '.join(command[:3])}")
    return completed.stdout


def verify_environment(venv: Path, inventory: dict[str, Any]) -> dict[str, Any]:
    if Path(sys.executable).resolve() != (venv / "bin/python").resolve():
        raise VerificationFailure("environment", f"Run environment verification with {venv}/bin/python")
    if not ((3, 11) <= sys.version_info[:2] < (3, 14)):
        raise VerificationFailure("environment", f"Unsupported Python {sys.version.split()[0]}")

    versions: dict[str, str] = {}
    for service in inventory["services"]:
        if service["environment"] != "nvidia-shared":
            continue
        actual = importlib.metadata.version(service["package"])
        if actual != service["expected_version"]:
            raise VerificationFailure(
                "environment",
                f"{service['package']}: expected {service['expected_version']}, got {actual}",
            )
        versions[service["package"]] = actual
    for package, expected in FUNCTION_PACKAGES.items():
        actual = importlib.metadata.version(package)
        if actual != expected:
            raise VerificationFailure("environment", f"{package}: expected {expected}, got {actual}")
        versions[package] = actual
    for package in ("nvidia-nat", "mcp"):
        versions[package] = importlib.metadata.version(package)

    for module in IMPORTS:
        importlib.import_module(module)
    for command in ("nat", "omni-ui-aiq", "kit-mcp", "usd-code-mcp", "isaacsim-mcp"):
        path = venv / "bin" / command
        if not path.is_file() or not os.access(path, os.X_OK):
            raise VerificationFailure("environment", f"Missing entrypoint: {path}")

    import yaml

    for relative in CONFIG_PATHS:
        with (REPO_ROOT / relative).open(encoding="utf-8") as stream:
            if not isinstance(yaml.safe_load(stream), dict):
                raise VerificationFailure("configuration", f"Config is not a mapping: {relative}")

    git_lfs = shutil.which("git-lfs")
    if not git_lfs:
        raise VerificationFailure("lfs", "git-lfs is not installed")
    lfs_version = run_checked([git_lfs, "version"]).strip()
    lfs_paths = run_checked(["git", "lfs", "ls-files", "-n"]).splitlines()
    relevant = [path for path in lfs_paths if path.startswith(LFS_PREFIXES)]
    missing: list[str] = []
    pointers: list[str] = []
    for relative in relevant:
        path = REPO_ROOT / relative
        if not path.is_file():
            missing.append(relative)
            continue
        with path.open("rb") as stream:
            if stream.read(80).startswith(b"version https://git-lfs.github.com/spec/v1"):
                pointers.append(relative)
    if missing or pointers:
        raise VerificationFailure(
            "lfs",
            f"Relevant LFS data is not materialized (missing={len(missing)}, pointers={len(pointers)})",
        )

    tracked_env = run_checked(["git", "ls-files", "source/mcp/.env"]).strip()
    if tracked_env:
        raise VerificationFailure("credentials", "source/mcp/.env must not be tracked")
    return {
        "python": sys.version.split()[0],
        "venv": str(venv),
        "versions": dict(sorted(versions.items())),
        "imports": "passed",
        "entrypoints": "passed",
        "configs": "passed",
        "git_lfs": lfs_version,
        "lfs_files_materialized": len(relevant),
        "credential_file_untracked": True,
    }


async def async_main(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    inventory = load_inventory()
    if args.environment_only:
        report = {"environment": verify_environment(Path(args.venv).resolve(), inventory)}
        return report, 0

    services = service_map(inventory)
    selected = args.service or list(services)
    unknown = sorted(set(selected) - set(services))
    if unknown:
        raise VerificationFailure("arguments", f"Unknown services: {', '.join(unknown)}")

    report: dict[str, Any] = {
        "schema_version": 1,
        "backends": backend_status(),
        "services": [],
    }
    exit_code = 0
    for service_id in selected:
        try:
            result = await inspect_service(
                services[service_id],
                call_read_only=not args.probe,
                semantic=args.semantic,
                timeout=args.timeout,
            )
        except VerificationFailure as exc:
            result = {"id": service_id, "status": "failed", "category": exc.category, "message": str(exc)}
            exit_code = 1
        report["services"].append(result)
    return report, exit_code


def print_human(report: dict[str, Any]) -> None:
    environment = report.get("environment")
    if environment:
        print(f"environment: PASS ({environment['venv']})")
        print(f"python: {environment['python']}")
        for name, version in environment["versions"].items():
            print(f"package: {name}=={version}")
        print(f"LFS materialized: {environment['lfs_files_materialized']} files")
        return
    backends = report.get("backends", {})
    print(
        "backends: "
        f"embedder={backends.get('embedder_backend')}/{backends.get('embedder_dependency')}, "
        f"reranker={backends.get('reranker_backend')}/{backends.get('reranker_dependency')}"
    )
    for service in report.get("services", []):
        if service.get("status") == "failed":
            print(f"{service['id']}: FAIL [{service['category']}] {service['message']}")
            continue
        print(f"{service['id']}: PASS transport, {service['tool_count']} exact tools")
        metadata = service["metadata"]
        print(f"  metadata: annotations={metadata['annotations']}; _meta={metadata['_meta']}")
        if "read_only_call" in service:
            call = service["read_only_call"]
            print(f"  read-only call {call['tool']}: {call['status']} ({call['text_characters']} text chars)")
        if "semantic_call" in service:
            call = service["semantic_call"]
            suffix = f" [{call.get('failure_category')}]" if call["status"] != "passed" else ""
            print(f"  semantic call {call['tool']}: {call['status']}{suffix}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", action="append", help="Service id; repeat to select multiple")
    parser.add_argument("--probe", action="store_true", help="Only initialize and check exact tool metadata")
    parser.add_argument("--semantic", action="store_true", help="Also run one deterministic semantic search")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--json", action="store_true", help="Print JSON rather than a human summary")
    parser.add_argument("--report", type=Path, help="Write the metadata report outside the repository")
    parser.add_argument("--environment-only", action="store_true")
    parser.add_argument("--venv", default=os.environ.get("KIT_USD_MCP_VENV", str(DEFAULT_NVIDIA_VENV)))
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.probe and args.semantic:
        parser.error("--probe and --semantic cannot be combined")
    return args


def main() -> int:
    args = parse_args()
    try:
        report, exit_code = asyncio.run(async_main(args))
    except VerificationFailure as exc:
        report = {"status": "failed", "category": exc.category, "message": str(exc)}
        exit_code = 1
    if args.report:
        report_path = args.report.expanduser().resolve()
        try:
            report_path.relative_to(REPO_ROOT)
        except ValueError:
            pass
        else:
            print("ERROR: verification reports must remain outside the repository", file=sys.stderr)
            return 64
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_human(report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
