"""Ultracode — deterministic multi-agent workflow engine for agent-ultra-kit.

Public surface:

    from agent_ultra.ultracode import UltracodeEngine, demo_pool
    eng = UltracodeEngine(demo_pool())          # offline, keyless
    report = eng.run_script(eng.resolve("smoke"))
    print(report.final_state)                   # COMPLETE

Workflow scripts are Python modules with a ``META`` dict and an
``async def run(wf)`` coroutine. See engine.py for the ``wf`` surface
(agent / parallel / pipeline / budget / run_check / phase / log / evidence)
and the bundled examples/ for runnable workflows.
"""

from .engine import (
    UltracodeEngine,
    Workflow,
    WorkflowError,
    WorkflowReport,
    BudgetExceeded,
    summarize_events,
    render_card,
    print_card,
    supports_rich,
    build_receipt,
    verify_receipt,
    ultracode_home,
    DEFAULTS,
)
from .mock import demo_pool, MockUltracodeClient

__all__ = [
    "UltracodeEngine", "Workflow", "WorkflowError", "WorkflowReport",
    "BudgetExceeded", "summarize_events", "render_card", "print_card",
    "supports_rich", "build_receipt", "verify_receipt", "ultracode_home",
    "DEFAULTS", "demo_pool", "MockUltracodeClient",
]
