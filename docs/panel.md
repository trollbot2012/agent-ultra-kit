# Adversarial panel

The panel fans a hard question out across bounded critic agents, then judges
the debate. It proposes; the judge disposes; proof gates decide what ships.

## Quick start

```python
from agent_ultra.routes.pool import RoutePool
from agent_ultra.routes.client import OpenAIChatClient
from agent_ultra.panel.engine import PanelEngine

pool = RoutePool(["gpt-4o-mini"],
                 client=OpenAIChatClient("http://127.0.0.1:4000/v1",
                                         api_key_env="OPENAI_API_KEY"))
engine = PanelEngine(pool)
report = engine.run(
    "Is this change safe to ship?",
    size="medium",
    evidence_dirs=["./src"],       # critics read real source, not fiction
)
print(report.decision)
for f in report.accepted:
    print(f.verdict, f.severity, f.lens, f.claim)
```

Offline, use `from agent_ultra.routes.mock import demo_panel_client`.

## Sizes and lenses

| size   | lenses | use for |
|--------|--------|---------|
| small  | 4      | quick sanity check |
| medium | 10     | normal review |
| large  | 16     | needs `allow_large=True` |

The default lens library covers correctness, security, failure-modes,
simplicity, performance, operator-experience, data-integrity,
maintainability, cost, concurrency, testability, migration-risk,
observability, compatibility, recovery, abuse-resistance. Pass your own with
`lenses=[...]`.

## Verdicts

- `real_now` — a real problem triggerable now.
- `real_later` — real but needs a future condition.
- `theoretical` — plausible, but no concrete path shown.
- `wrong` — the steelman refutes it.

`report.accepted` is the `real_now`/`real_later` set. A finding whose judge
call failed is marked `unjudged` (never silently `theoretical`).

## Phase protocol

Every prompt leads with a marker: `PROBE`, `CHALLENGE`, `STEELMAN`,
`CROSS-EXAM`, `SYNTHESIS`. The mock route dispatches on that marker, which is
why examples and tests run fully offline.

## Routing modes

- `single` (default) — one healthy route runs everything.
- `mixed` — critics spread across healthy routes; a steelman prefers a
  different backend than its accuser. Collapses to `single` when only one
  route is healthy.

## Degradation

If the preferred route is dead the pool falls through to the next healthy one;
a route that dies mid-run is marked dead and calls fall through; a dead route
can be re-probed and revived. If **all** routes are dead the panel raises
`PanelError` with a health report — it never fabricates findings.

## Low-context guard

A run with under ~500 characters of context is flagged `low_context=True`:
the critics are told to attack only what is stated and not to invent code.
Feed real source through `evidence_dirs=` / `evidence_paths=` / `context=`.
