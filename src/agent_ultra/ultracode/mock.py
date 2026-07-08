"""Offline, deterministic, keyless mock route for ultracode.

No network, no API key. The client answers by inspecting the prompt:

  * "Reply with exactly: X"        -> X
  * a schema request                -> a synthesized schema-valid JSON value
  * "uppercase <word>"              -> the word uppercased (as JSON if asked)
  * otherwise                       -> a short deterministic echo

This lets every bundled example and the demo run with zero setup, and makes
tests fully offline. It is intentionally simple — a stand-in that exercises
the fan-out/journal/resume/receipt machinery, not a real model.
"""

from __future__ import annotations

import json
import re
import threading

from ..routes.pool import RoutePool
from ..routes.client import RouteError

_SCHEMA_RE = re.compile(r"matching this schema[^\n]*\n(\{.*\})\s*$", re.DOTALL)
_EXACT_RE = re.compile(r"[Rr]eply with exactly:?\s*(.+)")
# match "uppercase X", "'X' uppercased", or "X uppercased"
_UPPER_RE = re.compile(
    r"uppercas\w*\s+'?([A-Za-z0-9_-]+)'?|'?([A-Za-z0-9_-]+)'?\s+uppercas")


def _synthesize(schema):
    """Build a minimal value that satisfies a (subset) JSON schema."""
    if not isinstance(schema, dict):
        return "mock"
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    typ = schema.get("type")
    if isinstance(typ, list):
        typ = typ[0] if typ else "string"
    if typ == "object":
        out = {}
        props = schema.get("properties") or {}
        for key in schema.get("required", list(props.keys())):
            out[key] = _synthesize(props.get(key, {"type": "string"}))
        return out
    if typ == "array":
        return [_synthesize(schema.get("items", {"type": "string"}))]
    if typ == "integer":
        return 1
    if typ == "number":
        return 1.0
    if typ == "boolean":
        return False
    if typ == "null":
        return None
    return "mock"


class MockUltracodeClient:
    def __init__(self, default: str = "ok", fail_routes=None):
        self.default = default
        self.fail_routes = set(fail_routes or ())
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def usage_snapshot(self) -> dict:
        with self._lock:
            return dict(self._usage)

    def complete(self, model: str, prompt: str, max_tokens: int) -> str:
        with self._lock:
            self.calls.append((model, prompt[:60]))
            self._usage["prompt_tokens"] += max(1, len(prompt) // 4)
            self._usage["completion_tokens"] += 8
        if model in self.fail_routes:
            raise RouteError(f"{model}: mock route configured to fail")
        m = _SCHEMA_RE.search(prompt)
        if m:
            try:
                schema = json.loads(m.group(1))
            except ValueError:
                schema = {}
            up = _UPPER_RE.search(prompt)
            val = _synthesize(schema)
            # if the schema wants a single string and the prompt asked to
            # uppercase a word, honor it so examples produce meaningful output
            if up and isinstance(val, dict):
                word = up.group(1) or up.group(2)
                for k, v in val.items():
                    if isinstance(v, str):
                        val[k] = word.upper()
                        break
            return json.dumps(val)
        m = _EXACT_RE.search(prompt)
        if m:
            return m.group(1).strip().splitlines()[0].strip()
        m = _UPPER_RE.search(prompt)
        if m:
            return (m.group(1) or m.group(2)).upper()
        return f"{self.default}: " + " ".join(prompt.split())[:40]


def demo_pool(routes=None, fail_routes=None) -> RoutePool:
    """A RoutePool wired to the deterministic mock client."""
    client = MockUltracodeClient(fail_routes=fail_routes)
    return RoutePool(list(routes or ["mock-a", "mock-b"]), client=client)
