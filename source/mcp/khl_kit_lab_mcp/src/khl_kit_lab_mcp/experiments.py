"""Durable, local experiment records for the Kit Lab runtime MCP.

The store is intentionally single-process.  A threading lock protects all
sequence allocation and writes within that process; callers must not point two
runtime MCP processes at the same root.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar


DEFAULT_EXPERIMENT_ROOT = "/home/ubuntu/kit-ai/lab/experiments"
EXPERIMENT_ID_RE = re.compile(
    r"^[0-9]{8}T[0-9]{12}Z-[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?-[0-9a-f]{8}$"
)
OUTCOMES = {"success", "failed", "inconclusive", "cancelled"}

MAX_TITLE_CHARS = 200
MAX_OBJECTIVE_CHARS = 4_000
MAX_TAGS = 20
MAX_TAG_CHARS = 64
MAX_NOTE_CHARS = 4_000
MAX_SUMMARY_CHARS = 20_000
MAX_EVENT_LIMIT = 500
MAX_LIST_LIMIT = 100
MAX_LIST_OFFSET = 100_000
MAX_LIST_SCAN = 10_000
MAX_EVENT_LINE_BYTES = 256 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_ARGUMENT_BYTES = 32 * 1024
MAX_JSON_DEPTH = 10
MAX_COLLECTION_ITEMS = 1_000
MAX_STRING_CHARS = 128 * 1024
MAX_PYTHON_SOURCE_CHARS = 128 * 1024

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:api[_-]?key|authorization|bearer|credential|passwd|password|secret|token)"
)
_SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{12,}|"
    r"(?:api[_-]?key|authorization|credential|passwd|password|secret|token)"
    r"\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{12,})"
)


class ExperimentError(RuntimeError):
    """Base error for experiment persistence."""


class ExperimentValidationError(ExperimentError):
    """Input or path validation failed."""


class ExperimentConflictError(ExperimentError):
    """An operation conflicts with the single-active-experiment rule."""


class ExperimentNotFoundError(ExperimentError):
    """The requested experiment does not exist."""


class ExperimentCorruptError(ExperimentError):
    """Persisted experiment state is malformed or inconsistent."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def validate_experiment_id(experiment_id: str) -> str:
    if not isinstance(experiment_id, str) or not EXPERIMENT_ID_RE.fullmatch(experiment_id):
        raise ExperimentValidationError("Malformed experiment ID")
    return experiment_id


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    value = value[:32].rstrip("-")
    return value or "experiment"


def generate_experiment_id(title: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{_slug(title)}-{secrets.token_hex(4)}"


def _require_text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ExperimentValidationError(f"{field} must be text")
    value = value.strip()
    if not value:
        raise ExperimentValidationError(f"{field} must not be empty")
    if len(value) > maximum:
        raise ExperimentValidationError(f"{field} exceeds {maximum} characters")
    if _SENSITIVE_TEXT_RE.search(value):
        raise ExperimentValidationError(f"{field} appears to contain credential material")
    return value


def _optional_filter(value: str | None, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _require_text(value, field, maximum)


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= MAX_JSON_DEPTH:
        return "[TRUNCATED: maximum nesting depth]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = _SENSITIVE_TEXT_RE.sub("[REDACTED]", value)
        if len(text) > MAX_STRING_CHARS:
            return text[:MAX_STRING_CHARS] + "\n[TRUNCATED]"
        return text
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        items = list(value.items())
        for key, item in items[:MAX_COLLECTION_ITEMS]:
            safe_key = str(key)[:256]
            if _SENSITIVE_KEY_RE.search(safe_key):
                output[safe_key] = "[REDACTED_BY_KEY]"
            else:
                output[safe_key] = _bounded_value(item, depth=depth + 1)
        if len(items) > MAX_COLLECTION_ITEMS:
            output["__truncated_items__"] = len(items) - MAX_COLLECTION_ITEMS
        return output
    if isinstance(value, (list, tuple)):
        items = list(value)
        output = [_bounded_value(item, depth=depth + 1) for item in items[:MAX_COLLECTION_ITEMS]]
        if len(items) > MAX_COLLECTION_ITEMS:
            output.append({"__truncated_items__": len(items) - MAX_COLLECTION_ITEMS})
        return output
    return _bounded_value(repr(value), depth=depth + 1)


def _bounded_document(value: Any, maximum_bytes: int) -> Any:
    bounded = _bounded_value(value)
    encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return bounded
    preview = encoded[: max(0, maximum_bytes - 256)].decode("utf-8", errors="ignore")
    return {
        "truncated": True,
        "original_bounded_size_bytes": len(encoded),
        "json_preview": preview,
    }


def _safe_error(exc: BaseException) -> dict[str, str]:
    return {
        "type": type(exc).__name__[:200],
        "message": str(_bounded_value(str(exc)))[:4_000],
    }


T = TypeVar("T")


async def invoke_and_record(
    store: "ExperimentStore",
    experiment_id: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    operation: Callable[[], Awaitable[T]],
    *,
    python_source: str | None = None,
) -> T:
    """Invoke one existing runtime tool and durably record its outcome."""
    started = time.monotonic()
    try:
        result = await operation()
    except BaseException as exc:
        elapsed_ms = round((time.monotonic() - started) * 1_000, 3)
        if experiment_id is not None:
            store.record_operation(
                experiment_id,
                tool_name,
                arguments,
                success=False,
                elapsed_ms=elapsed_ms,
                error=_safe_error(exc),
                python_source=python_source,
                result_class="transport_or_server_failure",
            )
        raise

    elapsed_ms = round((time.monotonic() - started) * 1_000, 3)
    if experiment_id is not None:
        result_class = "result"
        if (
            tool_name == "kit_execute_python"
            and isinstance(result, dict)
            and not result.get("ok", False)
        ):
            result_class = "python_exception_result"
        store.record_operation(
            experiment_id,
            tool_name,
            arguments,
            success=True,
            elapsed_ms=elapsed_ms,
            result=result,
            python_source=python_source,
            result_class=result_class,
        )
    return result


class ExperimentStore:
    """Single-process durable experiment store rooted at a configured path."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        configured = root if root is not None else os.environ.get(
            "KIT_LAB_EXPERIMENT_ROOT", DEFAULT_EXPERIMENT_ROOT
        )
        self.root = Path(configured).expanduser().resolve(strict=False)
        self._lock = threading.RLock()
        self._absence_checked = False
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    @property
    def current_pointer(self) -> Path:
        return self.root / "current.json"

    def _ensure_contained(self, path: Path) -> Path:
        resolved = path.resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise ExperimentValidationError("Resolved path escapes experiment root")
        return resolved

    def _experiment_dir(self, experiment_id: str, *, must_exist: bool = True) -> Path:
        validate_experiment_id(experiment_id)
        raw_candidate = self.root / experiment_id
        if raw_candidate.is_symlink():
            raise ExperimentValidationError("Experiment directory must not be a symlink")
        candidate = self._ensure_contained(raw_candidate)
        if must_exist and not candidate.is_dir():
            raise ExperimentNotFoundError(f"Experiment not found: {experiment_id}")
        return candidate

    def _atomic_write_text(self, path: Path, text: str) -> None:
        parent = self._ensure_contained(path.parent)
        if not parent.is_dir():
            raise ExperimentCorruptError(f"Missing persistence directory: {parent.name}")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                fd = -1
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            self._fsync_directory(parent)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _atomic_write_json(self, path: Path, payload: Any) -> None:
        text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        self._atomic_write_text(path, text)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _read_json(self, path: Path, *, maximum_bytes: int = MAX_RESULT_BYTES) -> dict[str, Any]:
        if path.is_symlink():
            raise ExperimentCorruptError(f"Unsafe JSON symlink: {path.name}")
        resolved = self._ensure_contained(path)
        if not resolved.is_file():
            raise ExperimentCorruptError(f"Missing or unsafe JSON file: {path.name}")
        try:
            with resolved.open("rb") as handle:
                raw = handle.read(maximum_bytes + 1)
            if len(raw) > maximum_bytes:
                raise ExperimentCorruptError(f"JSON file is too large: {path.name}")
            payload = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExperimentCorruptError(f"Invalid JSON file: {path.name}") from exc
        if not isinstance(payload, dict):
            raise ExperimentCorruptError(f"JSON file is not an object: {path.name}")
        return payload

    def _manifest(self, experiment_id: str) -> dict[str, Any]:
        directory = self._experiment_dir(experiment_id)
        manifest = self._read_json(directory / "manifest.json")
        if manifest.get("experiment_id") != experiment_id:
            raise ExperimentCorruptError("Manifest experiment ID mismatch")
        if manifest.get("schema_version") != 1:
            raise ExperimentCorruptError("Unsupported experiment manifest schema")
        if not isinstance(manifest.get("next_sequence"), int) or manifest["next_sequence"] < 1:
            raise ExperimentCorruptError("Manifest has an invalid next_sequence")
        if manifest.get("status") not in {"active", "finished"}:
            raise ExperimentCorruptError("Manifest has an invalid status")
        return manifest

    def _current_id_locked(self) -> str | None:
        pointer = self.current_pointer
        if not pointer.exists():
            if self._absence_checked:
                return None
            self._absence_checked = True
            active = self._discover_active_locked()
            if active is not None:
                self._atomic_write_json(pointer, {"experiment_id": active})
                self._absence_checked = False
            return active
        self._absence_checked = False
        payload = self._read_json(pointer, maximum_bytes=4_096)
        experiment_id = payload.get("experiment_id")
        try:
            validate_experiment_id(experiment_id)
        except ExperimentValidationError as exc:
            raise ExperimentCorruptError("Current experiment pointer is malformed") from exc
        manifest = self._manifest(experiment_id)
        if manifest["status"] != "active":
            self._clear_current_locked()
            return None
        return experiment_id

    def _discover_active_locked(self) -> str | None:
        active: list[str] = []
        identifiers = sorted(
            (
                child.name
                for child in self.root.iterdir()
                if child.is_dir()
                and not child.is_symlink()
                and EXPERIMENT_ID_RE.fullmatch(child.name)
            ),
            reverse=True,
        )[:MAX_LIST_SCAN]
        for experiment_id in identifiers:
            try:
                manifest = self._manifest(experiment_id)
            except ExperimentError:
                continue
            if manifest["status"] == "active":
                active.append(experiment_id)
                if len(active) > 1:
                    raise ExperimentCorruptError(
                        "Multiple active experiment manifests exist under this root"
                    )
        return active[0] if active else None

    def current_id(self) -> str | None:
        with self._lock:
            return self._current_id_locked()

    def ensure_can_start(self) -> None:
        with self._lock:
            active = self._current_id_locked()
            if active is not None:
                raise ExperimentConflictError(
                    f"Experiment {active} is already active; finish it before starting another"
                )

    def start(
        self,
        title: str,
        objective: str,
        tags: list[str] | None,
        runtime_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        title = _require_text(title, "title", MAX_TITLE_CHARS)
        objective = _require_text(objective, "objective", MAX_OBJECTIVE_CHARS)
        if tags is None:
            tags = []
        if not isinstance(tags, list) or len(tags) > MAX_TAGS:
            raise ExperimentValidationError(f"tags must contain at most {MAX_TAGS} items")
        clean_tags = [_require_text(tag, "tag", MAX_TAG_CHARS) for tag in tags]
        clean_tags = list(dict.fromkeys(clean_tags))
        snapshot = _bounded_document(runtime_snapshot, MAX_RESULT_BYTES)

        with self._lock:
            self.ensure_can_start()
            experiment_id = generate_experiment_id(title)
            while (self.root / experiment_id).exists():
                experiment_id = generate_experiment_id(title)
            final_directory = self.root / experiment_id
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{experiment_id}.", suffix=".tmp", dir=self.root)
            )
            os.chmod(temporary, 0o700)
            try:
                for name in ("scripts", "results", "artifacts"):
                    (temporary / name).mkdir(mode=0o700)
                now = utc_now()
                manifest = {
                    "schema_version": 1,
                    "experiment_id": experiment_id,
                    "title": title,
                    "objective": objective,
                    "tags": clean_tags,
                    "status": "active",
                    "outcome": None,
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                    "next_sequence": 2,
                    "runtime_snapshot": snapshot,
                    "summary": None,
                }
                self._atomic_write_json(temporary / "manifest.json", manifest)
                initial_event = {
                    "sequence": 1,
                    "timestamp": now,
                    "event": "experiment_started",
                    "title": title,
                }
                self._atomic_write_text(
                    temporary / "events.jsonl",
                    json.dumps(initial_event, ensure_ascii=False, separators=(",", ":")) + "\n",
                )
                self._atomic_write_text(temporary / "summary.md", "")
                os.replace(temporary, final_directory)
                self._fsync_directory(self.root)
                self._atomic_write_json(self.current_pointer, {"experiment_id": experiment_id})
                self._absence_checked = False
            except BaseException:
                self._absence_checked = False
                self._remove_temporary_tree(temporary)
                raise
            return manifest

    def _remove_temporary_tree(self, directory: Path) -> None:
        resolved = self._ensure_contained(directory)
        if not resolved.name.startswith(".") or not resolved.name.endswith(".tmp"):
            raise ExperimentValidationError("Refusing to remove a non-temporary path")
        if not resolved.exists():
            return
        for child in sorted(resolved.rglob("*"), reverse=True):
            if child.is_dir() and not child.is_symlink():
                child.rmdir()
            else:
                child.unlink()
        resolved.rmdir()

    def current(self) -> dict[str, Any]:
        with self._lock:
            experiment_id = self._current_id_locked()
            if experiment_id is None:
                return {"active": False, "experiment": None}
            return {"active": True, "experiment": self._manifest(experiment_id)}

    def _reserve_sequence_locked(self, experiment_id: str) -> tuple[int, dict[str, Any]]:
        manifest = self._manifest(experiment_id)
        if manifest["status"] != "active":
            raise ExperimentConflictError(f"Experiment {experiment_id} is not active")
        sequence = manifest["next_sequence"]
        manifest["next_sequence"] = sequence + 1
        manifest["updated_at"] = utc_now()
        self._atomic_write_json(self._experiment_dir(experiment_id) / "manifest.json", manifest)
        return sequence, manifest

    def _append_event_locked(self, experiment_id: str, event: dict[str, Any]) -> None:
        path = self._experiment_dir(experiment_id) / "events.jsonl"
        if path.is_symlink():
            raise ExperimentCorruptError("Unsafe events symlink")
        resolved = self._ensure_contained(path)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(resolved, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            encoded = line.encode("utf-8")
            if len(encoded) > MAX_EVENT_LINE_BYTES:
                raise ExperimentValidationError("Event record exceeds its storage bound")
            with os.fdopen(descriptor, "ab") as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _tool_file_name(tool_name: str) -> str:
        name = re.sub(r"[^a-z0-9]+", "-", tool_name.lower()).strip("-")
        return name[:80] or "tool"

    def record_operation(
        self,
        experiment_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        success: bool,
        elapsed_ms: float,
        result: Any = None,
        error: dict[str, Any] | None = None,
        python_source: str | None = None,
        result_class: str = "result",
    ) -> dict[str, Any]:
        with self._lock:
            current = self._current_id_locked()
            if current != experiment_id:
                raise ExperimentConflictError(
                    "The experiment active when this operation began is no longer active"
                )
            sequence, _ = self._reserve_sequence_locked(experiment_id)
            directory = self._experiment_dir(experiment_id)
            result_reference = f"results/{sequence:06d}-{self._tool_file_name(tool_name)}.json"
            source_reference = None
            if python_source is not None:
                if (
                    not isinstance(python_source, str)
                    or len(python_source) > MAX_PYTHON_SOURCE_CHARS
                ):
                    raise ExperimentValidationError(
                        "Python source exceeds the "
                        f"{MAX_PYTHON_SOURCE_CHARS}-character recording limit"
                    )
                source_reference = f"scripts/{sequence:06d}.py"
                self._atomic_write_text(directory / source_reference, python_source)

            result_payload = {
                "schema_version": 1,
                "experiment_id": experiment_id,
                "sequence": sequence,
                "tool": tool_name,
                "success": bool(success),
                "result_class": result_class,
                "elapsed_ms": max(0.0, float(elapsed_ms)),
                "result": _bounded_document(result, MAX_RESULT_BYTES) if success else None,
                "error": _bounded_document(error, MAX_RESULT_BYTES) if not success else None,
            }
            self._atomic_write_json(directory / result_reference, result_payload)

            event_arguments = dict(arguments)
            if python_source is not None:
                event_arguments.pop("code", None)
                event_arguments["python_source"] = {
                    "file": source_reference,
                    "characters": len(python_source),
                }
            event = {
                "sequence": sequence,
                "timestamp": utc_now(),
                "event": "tool_operation",
                "tool": tool_name,
                "arguments": _bounded_document(event_arguments, MAX_ARGUMENT_BYTES),
                "success": bool(success),
                "result_class": result_class,
                "elapsed_ms": max(0.0, float(elapsed_ms)),
                "result_file": result_reference,
                "python_source_file": source_reference,
            }
            self._append_event_locked(experiment_id, event)
            return event

    def note(self, note: str) -> dict[str, Any]:
        note = _require_text(note, "note", MAX_NOTE_CHARS)
        with self._lock:
            experiment_id = self._current_id_locked()
            if experiment_id is None:
                raise ExperimentConflictError("No experiment is active")
            sequence, _ = self._reserve_sequence_locked(experiment_id)
            event = {
                "sequence": sequence,
                "timestamp": utc_now(),
                "event": "note",
                "note": note,
            }
            self._append_event_locked(experiment_id, event)
            return {"experiment_id": experiment_id, "sequence": sequence, "event": event}

    def _read_recent_events(self, experiment_id: str, limit: int) -> list[dict[str, Any]]:
        if limit < 0 or limit > MAX_EVENT_LIMIT:
            raise ExperimentValidationError(f"event_limit must be between 0 and {MAX_EVENT_LIMIT}")
        if limit == 0:
            return []
        path = self._experiment_dir(experiment_id) / "events.jsonl"
        if path.is_symlink():
            raise ExperimentCorruptError("Unsafe events symlink")
        resolved = self._ensure_contained(path)
        if not resolved.is_file():
            raise ExperimentCorruptError("Missing or unsafe events file")
        recent: deque[dict[str, Any]] = deque(maxlen=limit)
        try:
            with resolved.open("rb") as handle:
                for raw_line in handle:
                    if len(raw_line) > MAX_EVENT_LINE_BYTES:
                        raise ExperimentCorruptError("An event record exceeds its size bound")
                    payload = json.loads(raw_line)
                    if not isinstance(payload, dict):
                        raise ExperimentCorruptError("An event record is not an object")
                    recent.append(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExperimentCorruptError("events.jsonl is malformed") from exc
        return list(recent)

    def get(self, experiment_id: str, event_limit: int = 100) -> dict[str, Any]:
        with self._lock:
            manifest = self._manifest(experiment_id)
            events = self._read_recent_events(experiment_id, event_limit)
            return {"experiment": manifest, "events": events, "event_limit": event_limit}

    def list(
        self,
        *,
        status: str | None = None,
        tag: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        if status not in {None, "active", "finished"}:
            raise ExperimentValidationError("status must be active or finished")
        tag = _optional_filter(tag, "tag", MAX_TAG_CHARS)
        if limit < 1 or limit > MAX_LIST_LIMIT:
            raise ExperimentValidationError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        if offset < 0 or offset > MAX_LIST_OFFSET:
            raise ExperimentValidationError(f"offset must be between 0 and {MAX_LIST_OFFSET}")

        with self._lock:
            identifiers = sorted(
                (
                    child.name
                    for child in self.root.iterdir()
                    if child.is_dir()
                    and not child.is_symlink()
                    and EXPERIMENT_ID_RE.fullmatch(child.name)
                ),
                reverse=True,
            )
            scan_truncated = len(identifiers) > MAX_LIST_SCAN
            identifiers = identifiers[:MAX_LIST_SCAN]
            matches: list[dict[str, Any]] = []
            errors: list[dict[str, str]] = []
            for experiment_id in identifiers:
                try:
                    manifest = self._manifest(experiment_id)
                except ExperimentError as exc:
                    if len(errors) < 20:
                        errors.append({"experiment_id": experiment_id, "error": str(exc)[:500]})
                    continue
                if status is not None and manifest["status"] != status:
                    continue
                if tag is not None and tag not in manifest.get("tags", []):
                    continue
                matches.append(
                    {
                        "experiment_id": manifest.get("experiment_id"),
                        "title": manifest.get("title"),
                        "objective": self._preview(manifest.get("objective"), 500),
                        "tags": manifest.get("tags"),
                        "status": manifest.get("status"),
                        "outcome": manifest.get("outcome"),
                        "created_at": manifest.get("created_at"),
                        "updated_at": manifest.get("updated_at"),
                        "completed_at": manifest.get("completed_at"),
                        "summary": self._preview(manifest.get("summary"), 500),
                    }
                )
            page = matches[offset : offset + limit]
            next_offset = offset + len(page) if offset + len(page) < len(matches) else None
            return {
                "experiments": page,
                "offset": offset,
                "limit": limit,
                "next_offset": next_offset,
                "matched": len(matches),
                "scan_truncated": scan_truncated,
                "errors": errors,
            }

    @staticmethod
    def _preview(value: Any, maximum: int) -> Any:
        if not isinstance(value, str) or len(value) <= maximum:
            return value
        return value[:maximum] + "…"

    def finish(self, summary: str, outcome: str) -> dict[str, Any]:
        summary = _require_text(summary, "summary", MAX_SUMMARY_CHARS)
        if outcome not in OUTCOMES:
            raise ExperimentValidationError(
                "outcome must be one of: cancelled, failed, inconclusive, success"
            )
        with self._lock:
            experiment_id = self._current_id_locked()
            if experiment_id is None:
                raise ExperimentConflictError("No experiment is active")
            sequence, manifest = self._reserve_sequence_locked(experiment_id)
            directory = self._experiment_dir(experiment_id)
            completed_at = utc_now()
            summary_document = (
                f"# {manifest['title']}\n\n"
                f"- Experiment ID: `{experiment_id}`\n"
                f"- Outcome: `{outcome}`\n"
                f"- Completed: `{completed_at}`\n\n"
                "## Summary\n\n"
                f"{summary}\n"
            )
            self._atomic_write_text(directory / "summary.md", summary_document)
            event = {
                "sequence": sequence,
                "timestamp": completed_at,
                "event": "experiment_finished",
                "outcome": outcome,
                "summary_file": "summary.md",
            }
            self._append_event_locked(experiment_id, event)
            manifest.update(
                {
                    "status": "finished",
                    "outcome": outcome,
                    "summary": summary,
                    "updated_at": completed_at,
                    "completed_at": completed_at,
                }
            )
            self._atomic_write_json(directory / "manifest.json", manifest)
            self._clear_current_locked()
            return {
                "active": False,
                "experiment": manifest,
                "final_event": event,
                "summary_file": "summary.md",
            }

    def _clear_current_locked(self) -> None:
        pointer = self.current_pointer
        try:
            pointer.unlink()
        except FileNotFoundError:
            return
        self._fsync_directory(self.root)
        self._absence_checked = True
