"""Optional Mneme memory adapter (example of wiring the generic hooks to a
real memory system).

Mneme is one such system; this adapter shows the shape without importing it.
If a `mneme`-like client is available it is used; otherwise events fall back
to a local JSONL file, so importing this module never fails and never
requires the dependency.

Replace `_MnemeClientProtocol` calls with your memory system's real API.
"""

from __future__ import annotations

from pathlib import Path

from ..memory.hooks import MemoryHooks, JsonlHooks


class MnemeHooks(MemoryHooks):
    """Write panel decisions and accepted findings to a memory client.

    client: any object exposing `write_note(kind, title, body, tags)`. Pass
    your Mneme (or other) client. If None, degrades to a JSONL fallback.
    """

    def __init__(self, client=None, fallback_path: Path | str | None = None,
                 namespace: str = "agent-ultra"):
        self.client = client
        self.namespace = namespace
        self._fallback = JsonlHooks(fallback_path) if (client is None and fallback_path) else None

    def _note(self, kind: str, title: str, body: str, tags=()):
        if self.client is not None:
            try:
                self.client.write_note(kind=kind, title=title, body=body,
                                       tags=list(tags) + [self.namespace])
                return
            except Exception:
                pass
        if self._fallback is not None:
            self._fallback.on_lesson_learned(f"{kind}: {title}",
                                             {"body": body, "tags": list(tags)})

    def on_panel_decision(self, record):
        self._note("panel_decision",
                   title=str(record.get("question", ""))[:80],
                   body=str(record.get("decision", "")),
                   tags=["panel", "decision"])

    def on_finding_accepted(self, finding):
        self._note("finding",
                   title=f"[{finding.get('lens')}] {finding.get('verdict')}",
                   body=str(finding.get("claim", "")),
                   tags=["panel", "finding", str(finding.get("severity", ""))])

    def on_lesson_learned(self, lesson, context=None):
        self._note("lesson", title=lesson[:80], body=lesson,
                   tags=["lesson"])
