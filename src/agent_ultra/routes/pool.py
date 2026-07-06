"""Health-aware route pool.

A ROUTE is a model name/alias served by a ChatClient. Panel agents are roles,
not models: the pool only decides which backend a role call lands on. One
healthy route is enough to run everything (same-model multi-lens mode).

Degradation ladder:
  - a route that fails a live probe is never assigned
  - a route that dies mid-run is marked dead; calls fall through to the next
    alive route
  - dead routes can be re-probed and revived
  - zero healthy routes -> the CALLER decides (the panel raises with a
    health report; it never silently pretends).
"""

from __future__ import annotations

import threading

from .client import ChatClient, RouteError

PROBE_PROMPT = "PROBE: reply with exactly: OK"


class RoutePool:
    def __init__(self, routes: list[str], client: ChatClient | None = None,
                 client_map: dict[str, ChatClient] | None = None,
                 probe_max_tokens: int = 200):
        if not routes:
            raise ValueError("RoutePool needs at least one route")
        if client is None and not client_map:
            raise ValueError("RoutePool needs a client or a client_map")
        self._routes = list(dict.fromkeys(routes))
        self._client = client
        self._client_map = dict(client_map or {})
        self._probe_max_tokens = probe_max_tokens
        self._dead: set[str] = set()
        self._health: dict[str, bool] = {}
        self._lock = threading.Lock()

    def client_for(self, route: str) -> ChatClient:
        c = self._client_map.get(route, self._client)
        if c is None:
            raise RouteError(f"{route}: no client configured")
        return c

    def chat(self, route: str, prompt: str, max_tokens: int) -> str:
        return self.client_for(route).complete(route, prompt, max_tokens)

    # -- health ------------------------------------------------------------

    def probe(self, route: str) -> bool:
        """Tiny live completion; cached per pool instance."""
        with self._lock:
            if route in self._health:
                return self._health[route]
        try:
            self.chat(route, PROBE_PROMPT, self._probe_max_tokens)
            ok = True
        except RouteError:
            ok = False
        with self._lock:
            self._health[route] = ok
            if not ok:
                self._dead.add(route)
        return ok

    def probe_all(self) -> list[str]:
        """Probe every route; return the healthy ones in preference order."""
        return [r for r in self._routes if self.probe(r)]

    def health_report(self) -> dict:
        with self._lock:
            return {"routes": list(self._routes), "health": dict(self._health),
                    "dead": sorted(self._dead)}

    # -- liveness ----------------------------------------------------------

    def alive(self) -> list[str]:
        with self._lock:
            return [r for r in self._routes if r not in self._dead]

    def dead(self) -> list[str]:
        with self._lock:
            return [r for r in self._routes if r in self._dead]

    def is_dead(self, route: str) -> bool:
        with self._lock:
            return route in self._dead

    def mark_dead(self, route: str) -> None:
        with self._lock:
            self._dead.add(route)

    def revive(self, route: str) -> bool:
        """Re-probe a dead route; revive it if it answers."""
        with self._lock:
            self._health.pop(route, None)
        if self.probe(route):
            with self._lock:
                self._dead.discard(route)
            return True
        return False

    def assign(self, index: int, avoid: str = "") -> list[str]:
        """Ordered candidate routes for agent slot *index*: round-robin pick
        first, then the rest; routes matching *avoid* sort last (a steelman
        prefers a different backend than its accuser when one exists)."""
        alive = self.alive()
        if not alive:
            return []
        primary = alive[index % len(alive)]
        ordered = [primary] + [r for r in alive if r != primary]
        if avoid and len(ordered) > 1:
            ordered.sort(key=lambda r: r == avoid)
        return ordered
