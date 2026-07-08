"""Escalation budget — bound how often the verifier may escalate.

Verification (especially the LLM channel) costs something. The budget caps
escalations per session and per rolling window, and dedups on identical
``claim_sha256`` so re-asking the same claim is free after the first answer.
All limits are configurable with neutral defaults.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """An escalation was requested past the configured cap."""


@dataclass
class EscalationBudget:
    per_session: int = 20         # max escalations for one session
    per_window: int = 5           # max escalations inside window_seconds
    window_seconds: int = 60

    _session_counts: dict = field(default_factory=dict, init=False)
    _window: deque = field(default_factory=deque, init=False)
    _dedup: dict = field(default_factory=dict, init=False)  # claim_sha -> result

    def cached(self, claim_sha: str):
        """Return a previously computed result for this exact claim, or None."""
        return self._dedup.get(claim_sha)

    def remember(self, claim_sha: str, result) -> None:
        self._dedup[claim_sha] = result

    def check(self, session_id: str, claim_sha: str) -> None:
        """Raise BudgetExceeded if a fresh escalation would breach a cap. A
        deduped (already-seen) claim never counts against the budget."""
        if claim_sha in self._dedup:
            return
        now = time.time()
        while self._window and now - self._window[0] > self.window_seconds:
            self._window.popleft()
        if len(self._window) >= self.per_window:
            raise BudgetExceeded(
                f"per-window cap {self.per_window}/{self.window_seconds}s reached")
        if self._session_counts.get(session_id, 0) >= self.per_session:
            raise BudgetExceeded(
                f"per-session cap {self.per_session} reached for {session_id!r}")

    def charge(self, session_id: str, claim_sha: str) -> None:
        """Record a fresh escalation against the caps (skip if deduped)."""
        if claim_sha in self._dedup:
            return
        self._window.append(time.time())
        self._session_counts[session_id] = \
            self._session_counts.get(session_id, 0) + 1
