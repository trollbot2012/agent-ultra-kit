"""Mock model route — deterministic, keyless, offline.

Every prompt in the kit LEADS with a phase marker (PROBE, CHALLENGE,
STEELMAN, CROSS-EXAM, SYNTHESIS, FIX). The mock dispatches on that marker,
so tests and examples run with zero network and zero API keys.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Callable, Union

from .client import RouteError

Handler = Union[str, Callable[[str], str]]


class MockChatClient:
    def __init__(self, handlers: dict[str, Handler] | None = None,
                 default: str = "OK", fail_routes: set[str] | None = None):
        self.handlers = {k.upper(): v for k, v in (handlers or {}).items()}
        self.default = default
        self.fail_routes = set(fail_routes or ())
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def complete(self, model: str, prompt: str, max_tokens: int) -> str:
        marker = (prompt.split(":", 1)[0] or "").strip().upper()
        with self._lock:
            self.calls.append((model, marker))
        if model in self.fail_routes:
            raise RouteError(f"{model}: mock route configured to fail")
        h = self.handlers.get(marker)
        if h is None:
            return self.default
        return h(prompt) if callable(h) else h


_DEMO_CLAIMS = {
    "correctness": [
        {"claim": "Return value is unvalidated: a None result from the lookup "
                  "propagates to the caller and crashes downstream formatting.",
         "severity": "high", "anchor": "assumption",
         "check": "grep -n 'return' service.py"},
    ],
    "security": [
        {"claim": "Empty or whitespace-only token is accepted as authenticated "
                  "because the check only tests for key presence, not value.",
         "severity": "critical", "anchor": "assumption",
         "check": "grep -n 'token' service.py"},
    ],
    "failure-modes": [
        {"claim": "No timeout on the outbound call; a hung dependency stalls "
                  "the worker pool indefinitely.",
         "severity": "medium", "anchor": "assumption", "check": ""},
    ],
    "abuse-resistance": [
        {"claim": "No rate limiting on retries; a hostile client can force "
                  "unbounded upstream calls.",
         "severity": "medium", "anchor": "assumption", "check": ""},
    ],
}

_LENS_RE = re.compile(r"the '([a-z0-9-]+)' critic")


def _demo_challenge(prompt: str) -> str:
    m = _LENS_RE.search(prompt)
    lens = m.group(1) if m else "correctness"
    return json.dumps(_DEMO_CLAIMS.get(lens, _DEMO_CLAIMS["correctness"]))


def _demo_cross_exam(prompt: str) -> str:
    low = prompt.lower()
    if "token" in low or "authenticated" in low:
        v = {"verdict": "real_now", "severity": "critical",
             "confidence": "high",
             "reasoning": "The steelman offers no code path that rejects an "
                          "empty token; the claim stands on the shown source."}
    elif "unvalidated" in low or "none result" in low:
        v = {"verdict": "real_later", "severity": "high", "confidence": "high",
             "reasoning": "Reachable once the lookup backend can miss; not "
                          "triggered by current callers."}
    else:
        v = {"verdict": "theoretical", "severity": "low", "confidence": "high",
             "reasoning": "Plausible in principle; no concrete path shown in "
                          "the provided context."}
    return json.dumps(v)


def demo_panel_client() -> MockChatClient:
    """A scripted client that produces a plausible, fully offline panel run."""
    return MockChatClient({
        "PROBE": "OK",
        "CHALLENGE": _demo_challenge,
        "STEELMAN": "STEELMAN: the design assumes a trusted gateway strips "
                    "empty tokens upstream, and lookups are backed by a "
                    "cache that cannot miss in the current deployment.",
        "CROSS-EXAM": _demo_cross_exam,
        "SYNTHESIS": json.dumps({
            "synthesis": "The panel accepted an authentication bypass "
                         "(empty token) as real-now critical and an "
                         "unvalidated-return crash as real-later; resilience "
                         "concerns (timeouts, rate limits) remain theoretical "
                         "on the evidence shown.",
            "decision": "Fix the empty-token acceptance before shipping; "
                        "schedule the None-return validation.",
            "disagreements": [],
        }),
        "FIX": "PATCHED",
    })
