"""Private infrastructure evidence for bounded Kit profiler captures."""

from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_PROFILE_ROOT = "/home/ubuntu/kit-ai/lab/profiles"
MAX_PROFILE_RESULT_BYTES = 4 * 1024 * 1024


class ProfileStoreError(RuntimeError):
    pass


def validate_capture_id(capture_id: str) -> str:
    if not isinstance(capture_id, str) or len(capture_id) != 32:
        raise ProfileStoreError("Invalid capture ID")
    try:
        parsed = uuid.UUID(hex=capture_id)
    except ValueError as exc:
        raise ProfileStoreError("Invalid capture ID") from exc
    if parsed.hex != capture_id:
        raise ProfileStoreError("Capture ID must be canonical lowercase hex")
    return capture_id


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class ProfileStore:
    def __init__(self, root: str | Path | None = None) -> None:
        configured = root or os.environ.get("KIT_LAB_PROFILE_ROOT", DEFAULT_PROFILE_ROOT)
        self.root = Path(configured).expanduser()
        if not self.root.is_absolute():
            raise ProfileStoreError("Profile root must be absolute")

    def _validate_existing_ancestors(self, path: Path) -> None:
        for candidate in reversed((path, *path.parents)):
            if not candidate.exists():
                continue
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ProfileStoreError(f"Symlink is forbidden in profile path: {candidate}")
            if candidate == self.root or self.root in candidate.parents:
                if metadata.st_uid != os.getuid():
                    raise ProfileStoreError("Profile paths must be user-owned")

    def _ensure_root(self) -> Path:
        self._validate_existing_ancestors(self.root.parent)
        self.root.mkdir(mode=0o700, parents=False, exist_ok=True)
        self._validate_existing_ancestors(self.root)
        metadata = self.root.stat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ProfileStoreError("Profile root must be a user-owned directory")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ProfileStoreError("Profile root must be private")
        return self.root.resolve(strict=True)

    def directory(self, capture_id: str) -> Path:
        validate_capture_id(capture_id)
        root = self._ensure_root()
        raw = root / capture_id
        if raw.is_symlink():
            raise ProfileStoreError("Capture directory must not be a symlink")
        resolved = raw.resolve(strict=False)
        if root not in resolved.parents:
            raise ProfileStoreError("Capture directory escaped the profile root")
        if raw.exists():
            metadata = raw.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise ProfileStoreError("Capture path must be a user-owned directory")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ProfileStoreError("Capture directory must be private")
        return resolved

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        capture_id = uuid.uuid4().hex
        directory = self.directory(capture_id)
        directory.mkdir(mode=0o700, exist_ok=False)
        manifest = {
            "schema_version": 1,
            "capture_id": capture_id,
            "created_at": _utc_now(),
            "state": "starting",
            "request": request,
        }
        path, size = self._write_new_json(directory, "request.json", manifest)
        return {
            "capture_id": capture_id,
            "directory": str(directory),
            "request_path": str(path),
            "request_bytes": size,
        }

    def finish(self, capture_id: str, result: dict[str, Any]) -> dict[str, Any]:
        directory = self.directory(capture_id)
        if not directory.is_dir():
            raise ProfileStoreError("Capture directory is missing")
        payload = {
            "schema_version": 1,
            "capture_id": capture_id,
            "finished_at": _utc_now(),
            "state": "complete" if result.get("ok") else "failed",
            "result": result,
        }
        path, size = self._write_new_json(directory, "result.json", payload)
        return {
            "directory": str(directory),
            "result_path": str(path),
            "format": "application/json",
            "actual_bytes": size,
        }

    def get(self, capture_id: str) -> dict[str, Any]:
        directory = self.directory(capture_id)
        request = self._read_json(directory, "request.json")
        result_path = directory / "result.json"
        try:
            result = self._read_json(directory, "result.json")
        except FileNotFoundError:
            result = None
        infrastructure_output = None
        if result is not None:
            actual_bytes = self._file_size(directory, "result.json")
            infrastructure_output = {
                "directory": str(directory),
                "result_path": str(result_path),
                "format": "application/json",
                "actual_bytes": actual_bytes,
            }
        return {
            "capture_id": capture_id,
            "request": request,
            "result": result,
            "complete": result is not None,
            "directory": str(directory),
            "infrastructure_output": infrastructure_output,
        }

    def _open_directory_fd(self, directory: Path) -> int:
        expected = directory.stat(follow_symlinks=False)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        actual = os.fstat(fd)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            os.close(fd)
            raise ProfileStoreError("Capture directory changed during validation")
        if actual.st_uid != os.getuid() or stat.S_IMODE(actual.st_mode) & 0o077:
            os.close(fd)
            raise ProfileStoreError("Capture directory became unsafe")
        return fd

    def _write_new_json(self, directory: Path, name: str, payload: dict[str, Any]) -> tuple[Path, int]:
        final = directory / name
        raw = (json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")
        if len(raw) > MAX_PROFILE_RESULT_BYTES:
            raise ProfileStoreError("Profile evidence exceeded the fixed size bound")
        directory_fd = self._open_directory_fd(directory)
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        fd = None
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(
                    temporary, name,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise ProfileStoreError(f"Refusing to overwrite {name}") from exc
            os.unlink(temporary, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)
        return final, len(raw)

    def _read_json(self, directory: Path, name: str) -> dict[str, Any]:
        directory_fd = self._open_directory_fd(directory)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                        or stat.S_IMODE(metadata.st_mode) & 0o077
                        or metadata.st_size > MAX_PROFILE_RESULT_BYTES):
                    raise ProfileStoreError("Profile evidence has unsafe type, ownership, mode, or size")
                raw = stream.read(MAX_PROFILE_RESULT_BYTES + 1)
        finally:
            os.close(directory_fd)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ProfileStoreError("Profile evidence is not a JSON object")
        return value

    def _file_size(self, directory: Path, name: str) -> int:
        directory_fd = self._open_directory_fd(directory)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ProfileStoreError("Profile evidence is not a regular file")
                return metadata.st_size
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
