"""Memory write-back hooks — generic, optional, never required.

Subclass MemoryHooks (all methods are no-ops) and pass an instance to the
panel engine / ULTRA loop / broker wrapper of your choice. The kit never
imports a specific memory system; adapters (e.g. the optional external-memory
adapter) live in agent_ultra.adapters.
"""

from __future__ import annotations

import time
from pathlib import Path


class MemoryHooks:
    """Override any subset. Exceptions raised by hooks are swallowed by
    callers — memory write-back must never break a run."""

    def on_panel_decision(self, record: dict) -> None: ...

    def on_finding_accepted(self, finding: dict) -> None: ...

    def on_command_run(self, result: dict) -> None: ...

    def on_task_complete(self, report: dict) -> None: ...

    def on_lesson_learned(self, lesson: str, context: dict | None = None) -> None: ...


def safe_call(hooks: MemoryHooks | None, method: str, *args, **kwargs) -> None:
    if hooks is None:
        return
    try:
        getattr(hooks, method)(*args, **kwargs)
    except Exception:
        pass  # memory is best-effort by doctrine


class CompositeHooks(MemoryHooks):
    def __init__(self, *hooks: MemoryHooks):
        self.hooks = [h for h in hooks if h is not None]

    def _fan(self, method: str, *args, **kwargs) -> None:
        for h in self.hooks:
            safe_call(h, method, *args, **kwargs)

    def on_panel_decision(self, record):
        self._fan("on_panel_decision", record)

    def on_finding_accepted(self, finding):
        self._fan("on_finding_accepted", finding)

    def on_command_run(self, result):
        self._fan("on_command_run", result)

    def on_task_complete(self, report):
        self._fan("on_task_complete", report)

    def on_lesson_learned(self, lesson, context=None):
        self._fan("on_lesson_learned", lesson, context)


class JsonlHooks(MemoryHooks):
    """Default reference implementation: every event appended to one JSONL
    file. Good enough to prove the wiring; replace with your agent's memory."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _write(self, event: str, payload) -> None:
        from ..artifacts.records import append_jsonl
        append_jsonl(self.path, {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event, "payload": payload})

    def on_panel_decision(self, record):
        self._write("panel_decision", record)

    def on_finding_accepted(self, finding):
        self._write("finding_accepted", finding)

    def on_command_run(self, result):
        self._write("command_run", result)

    def on_task_complete(self, report):
        self._write("task_complete", report)

    def on_lesson_learned(self, lesson, context=None):
        self._write("lesson_learned", {"lesson": lesson, "context": context or {}})
