#!/usr/bin/env python3
"""Write bounded operational logs while discarding likely tool-input lines."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


SENSITIVE_MARKERS = (
    "usage -",
    "query value",
    "called with",
    "query:",
    "request:",
    "context for '",
    "tool argument",
    "tool input",
    '"parameters"',
)


def rotate(path: Path, backups: int) -> None:
    oldest = path.with_name(f"{path.name}.{backups}")
    if oldest.exists() and not oldest.is_symlink():
        oldest.unlink()
    for index in range(backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        target = path.with_name(f"{path.name}.{index + 1}")
        if source.exists() and not source.is_symlink():
            os.replace(source, target)
    if path.exists() and not path.is_symlink():
        os.replace(path, path.with_name(f"{path.name}.1"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--max-bytes", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--backups", type=int, default=2)
    args = parser.parse_args()
    if args.max_bytes < 1024 or args.backups < 1:
        parser.error("unsafe log rotation limits")

    path = args.path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        print(f"Refusing symlink log path: {path}", file=sys.stderr)
        return 73
    os.umask(0o077)
    size = path.stat().st_size if path.exists() else 0
    stream = path.open("ab", buffering=0)
    try:
        for line in sys.stdin.buffer:
            lowered = line.lower()
            if any(marker.encode() in lowered for marker in SENSITIVE_MARKERS):
                continue
            if size + len(line) > args.max_bytes:
                stream.close()
                rotate(path, args.backups)
                stream = path.open("ab", buffering=0)
                size = 0
            stream.write(line)
            size += len(line)
    finally:
        stream.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
