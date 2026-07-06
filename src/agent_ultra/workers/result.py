"""Normalized worker result — the single shape every worker returns.

Ultra reads this shape regardless of which runtime produced the work, so the
router worker and the Deep Agents worker are interchangeable at the loop's
builder/fixer seams.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class WorkerResult:
    worker: str                       # "router" | "deepagents" | ...
    status: str = "ok"                # "ok" | "failed"
    summary: str = ""
    files_changed: list = field(default_factory=list)
    commands_run: list = field(default_factory=list)
    proof: list = field(default_factory=list)   # evidence strings
    error: str | None = None
    # advisory edit for the fixer seam: the loop applies + rolls back so the
    # worker's write still passes through tests + gates. None for pure builds.
    edit: dict | None = None          # {"path", "content"} | None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.error is None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def failed(worker: str, error: str, summary: str = "") -> WorkerResult:
    return WorkerResult(worker=worker, status="failed", error=error,
                        summary=summary or error)
