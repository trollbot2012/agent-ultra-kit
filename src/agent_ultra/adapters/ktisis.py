"""Optional Ktisis adapter (example).

Ktisis-style build agents care most about the ULTRA loop: a coding agent
builds, tests prove the contract, the panel finds the unknown failure modes,
and only proof-gated work ships. This adapter wires the loop to a caller-
supplied builder and fixer (the two runtime-specific seams) while providing
the panel + broker from environment conventions.

    KTISIS_ROUTER_URL / KTISIS_ROUTER_KEY_ENV / KTISIS_ROUTES / KTISIS_HOME

The builder and fixer are the ONLY runtime-specific pieces: pass callables
that drive your coding agent. Everything else is the portable core.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..routes.client import OpenAIChatClient
from ..routes.pool import RoutePool
from ..panel.engine import PanelEngine
from ..broker.broker import CommandBroker, TRUSTED_OWNER_TIERS
from ..ultra_loop.loop import UltraLoop


def _home() -> Path:
    return Path(os.environ.get("KTISIS_HOME", ".")).expanduser()


def ktisis_pool() -> RoutePool:
    base = os.environ.get("KTISIS_ROUTER_URL", "http://127.0.0.1:4000/v1")
    key_env = os.environ.get("KTISIS_ROUTER_KEY_ENV", "KTISIS_ROUTER_KEY")
    routes = [r.strip() for r in os.environ.get("KTISIS_ROUTES", "").split(",")
              if r.strip()]
    if not routes:
        raise ValueError("set KTISIS_ROUTES to a comma-separated list of model "
                         "aliases served by your router")
    client = OpenAIChatClient(base_url=base, api_key_env=key_env, timeout=300)
    return RoutePool(routes, client=client)


def ktisis_ultra_loop(workspace, builder=None, fixer=None, memory=None) -> UltraLoop:
    """Build an ULTRA loop for a Ktisis workspace.

    builder(workspace, task) -> str        drive the coding agent to implement
    fixer(fix_task, workspace) -> bool      drive the agent to fix one finding
    Both optional; without a fixer the loop reports findings but does not
    auto-fix (still fully useful as a gate).
    """
    engine = PanelEngine(ktisis_pool(), memory=memory)
    broker = CommandBroker(ledger_path=_home() / "broker_ledger.jsonl",
                           auto_run_tiers=TRUSTED_OWNER_TIERS)
    return UltraLoop(workspace, panel=engine, broker=broker,
                     builder=builder, fixer=fixer, memory=memory)
