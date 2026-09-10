import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit


DEFAULT_BASE_URL = "http://127.0.0.1:8011"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class KitLabClientError(RuntimeError):
    """A transport or response error from the Kit Lab HTTP service."""


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class KitLabClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("KIT_LAB_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self.timeout_seconds = timeout_seconds or float(
            os.environ.get("KIT_LAB_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
        )

        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("KIT_LAB_BASE_URL must be an absolute HTTP(S) URL")

        if (
            parsed.hostname not in LOOPBACK_HOSTS
            and not _env_truthy("KIT_LAB_ALLOW_REMOTE")
        ):
            raise ValueError(
                "KIT_LAB_BASE_URL must use loopback unless "
                "KIT_LAB_ALLOW_REMOTE=true is explicitly set"
            )

        if self.timeout_seconds <= 0:
            raise ValueError("KIT_LAB_TIMEOUT_SECONDS must be positive")

    async def get(self, path: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._request_sync, "GET", path, None)

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._request_sync,
            "POST",
            path,
            payload,
        )

    def _request_sync(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not path.startswith("/"):
            raise ValueError("Kit Lab request path must start with '/'")

        body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "khl-kit-lab-mcp/0.6.0",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            detail = raw.decode("utf-8", errors="replace")
            raise KitLabClientError(
                f"Kit Lab returned HTTP {exc.code}: {detail[:2000]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise KitLabClientError(
                f"Cannot reach Kit Lab at {self.base_url}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise KitLabClientError(
                f"Kit Lab request timed out after {self.timeout_seconds:g}s"
            ) from exc

        if len(raw) > MAX_RESPONSE_BYTES:
            raise KitLabClientError(
                f"Kit Lab response exceeded {MAX_RESPONSE_BYTES} bytes"
            )

        if status < 200 or status >= 300:
            raise KitLabClientError(f"Kit Lab returned unexpected HTTP {status}")

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KitLabClientError("Kit Lab returned invalid JSON") from exc

        if not isinstance(result, dict):
            raise KitLabClientError("Kit Lab returned a non-object JSON response")

        return result
