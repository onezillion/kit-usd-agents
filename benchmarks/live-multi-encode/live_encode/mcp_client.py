"""Minimal synchronous HTTP client for the Kit Lab in-Kit exec endpoint.

The Kit Lab service (omni.khl.kit_lab) exposes an exec/eval HTTP API on loopback
(default 127.0.0.1:8011) that the MCP server bridges. For orchestration that needs
fine control (and to avoid an MCP round-trip per high-frequency step) the live
runner talks to the Kit Lab HTTP exec endpoint directly.

This client is transport-tolerant: it tries the documented exec routes and accepts
the service's JSON envelope. It is ONLY used by scripts/run_scale.py.
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error


class KitLabClient:
    def __init__(self, base: str = "http://127.0.0.1:8011", timeout: float = 60.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            return {"ok": False, "error": f"HTTP {e.code}: {body}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def execute(self, code: str) -> dict:
        """Execute python in Kit; returns the parsed JSON envelope."""
        for path in ("/khl/lab/python/execute", "/khl/ai/python/execute",
                     "/python/execute", "/execute"):
            res = self._post(path, {"code": code})
            if not (isinstance(res, dict) and res.get("error", "").startswith("HTTP 404")):
                return res
        return res


def extract_stdout(envelope: dict, tag_begin: str, tag_end: str) -> str | None:
    """Pull a delimited block out of the exec stdout payload."""
    out = envelope.get("stdout", "") if isinstance(envelope, dict) else ""
    i = out.find(tag_begin)
    j = out.find(tag_end)
    if i < 0 or j < 0 or j <= i:
        return None
    return out[i + len(tag_begin):j].strip()
