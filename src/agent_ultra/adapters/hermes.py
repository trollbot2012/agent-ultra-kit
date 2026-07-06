"""Optional Hermes adapter (example).

Hermes-style agents run a local model router (an OpenAI-compatible proxy) and
keep an on-disk home directory for artifacts. This adapter builds a panel +
broker wired to those conventions, reading EVERYTHING from environment
variables so no operator-specific path is baked in.

    HERMES_ROUTER_URL   OpenAI-compatible proxy (default http://127.0.0.1:4000/v1)
    HERMES_ROUTER_KEY_ENV  env var name holding the router key (default HERMES_ROUTER_KEY)
    HERMES_ROUTES       comma-separated model aliases
    HERMES_HOME         base dir for the broker ledger + panel artifacts

Nothing here imports Hermes itself; it only follows the same conventions, so a
Hermes deployment can adopt the kit by pointing these vars at its own router
and home. Memory write-back is left to the caller (wire a MemoryHooks impl).
"""

from __future__ import annotations

import os
from pathlib import Path

from ..routes.client import OpenAIChatClient
from ..routes.pool import RoutePool
from ..panel.engine import PanelEngine
from ..broker.broker import CommandBroker, TRUSTED_OWNER_TIERS


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME", ".")).expanduser()


def hermes_pool() -> RoutePool:
    base = os.environ.get("HERMES_ROUTER_URL", "http://127.0.0.1:4000/v1")
    key_env = os.environ.get("HERMES_ROUTER_KEY_ENV", "HERMES_ROUTER_KEY")
    routes = [r.strip() for r in os.environ.get("HERMES_ROUTES", "").split(",")
              if r.strip()]
    if not routes:
        raise ValueError("set HERMES_ROUTES to a comma-separated list of model "
                         "aliases served by your router")
    client = OpenAIChatClient(base_url=base, api_key_env=key_env, timeout=300)
    return RoutePool(routes, client=client)


def hermes_panel(memory=None, judge_route: str = "") -> PanelEngine:
    return PanelEngine(hermes_pool(), memory=memory, judge_route=judge_route)


def hermes_broker(critic_mode: bool = False, approver=None):
    """Broker following the Hermes host-execution doctrine: the trusted owner
    auto-runs SAFE+ELEVATED and gates DANGEROUS; critic mode auto-runs SAFE
    only. Dangerous-without-approval DENIES by default (safe)."""
    from ..broker.broker import CRITIC_TIERS
    tiers = CRITIC_TIERS if critic_mode else TRUSTED_OWNER_TIERS
    return CommandBroker(
        ledger_path=_home() / "broker_ledger.jsonl",
        auto_run_tiers=tiers, approver=approver)
