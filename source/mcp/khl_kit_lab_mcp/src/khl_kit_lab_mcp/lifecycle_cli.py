"""Repository lifecycle setup and operator CLI; no package installation."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

from .lifecycle import (
    KIT_ID, KitLifecycle, LifecycleConfig, LifecycleError, LinuxProcesses,
    config_path, load_config, save_config,
)


async def progress(elapsed: float, total: float, message: str):
    print(message, file=sys.stderr, flush=True)


async def run(args) -> dict:
    if args.command == "setup":
        defaults = LifecycleConfig()
        config = LifecycleConfig(
            launcher=args.launcher or defaults.launcher,
            cwd=args.cwd or defaults.cwd,
            kit_executable=args.kit_executable or defaults.kit_executable,
            arguments=tuple(args.argument), log_roots=tuple(args.log_root or defaults.log_roots),
            capture_root=args.capture_root or defaults.capture_root,
            readiness_timeout=defaults.readiness_timeout if args.readiness_timeout is None else args.readiness_timeout,
            shutdown_timeout=defaults.shutdown_timeout if args.shutdown_timeout is None else args.shutdown_timeout,
        )
        config.validate()
        discovery = await asyncio.to_thread(LinuxProcesses(config.kit_executable).discover)
        discovery.require_inspection()
        save_config(config, args.config)
        return {"ok": True, "config_path": str(args.config),
                **await KitLifecycle(config).configuration()}
    lifecycle = KitLifecycle(load_config(args.config))
    if args.command == "config":
        return await lifecycle.configuration()
    if args.command == "status":
        return await lifecycle.status()
    if args.command == "log-paths":
        return await lifecycle.log_paths()
    if args.command == "start":
        return await lifecycle.start(args.timeout, progress)
    if args.command == "stop":
        return await lifecycle.stop(args.force, progress)
    return await lifecycle.restart(args.force, args.timeout, progress)


def main():
    parser = argparse.ArgumentParser(description=f"Manage only Kit identity {KIT_ID}.")
    parser.add_argument("--config", type=Path, default=config_path())
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="Validate and save machine configuration; installs nothing")
    for flag in ("launcher", "cwd", "kit-executable", "capture-root"):
        setup.add_argument(f"--{flag}")
    setup.add_argument("--argument", action="append", default=[], help="One literal argv element; use --argument=--flag")
    setup.add_argument("--log-root", action="append")
    setup.add_argument("--readiness-timeout", type=float)
    setup.add_argument("--shutdown-timeout", type=float)
    for name in ("config", "status", "log-paths", "start", "stop", "restart"):
        command = sub.add_parser(name)
        if name in {"start", "restart"}:
            command.add_argument("--timeout", type=float, help="Readiness timeout override, maximum 3600 seconds")
        if name in {"stop", "restart"}:
            command.add_argument("--force", action="store_true", help="Explicitly allow SIGKILL after graceful shutdown timeout")
    try:
        result = asyncio.run(run(parser.parse_args()))
    except (LifecycleError, OSError) as exc:
        print(json.dumps({"ok": False, "error": {"code": getattr(exc, "code", "OS_ERROR"),
                                                  "message": str(exc)}}))
        raise SystemExit(1) from None
    print(json.dumps(result, indent=2))
    if (result.get("state") in {"AMBIGUOUS", "INSPECTION_DENIED", "PROCESS_CHANGED", "UNRESPONSIVE"}
            or result.get("readiness_timed_out") or result.get("reason") == "LAUNCH_EXITED_WITHOUT_KIT"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
