# Architecture

agent-ultra-kit is a **portable core** plus **thin adapters**. Nothing in the
core imports an adapter; adapters may make runtime-specific assumptions.

```
                         ┌─────────────────────────────────────────┐
                         │                core                      │
   routes/  ── health-aware model routing (OpenAI-compatible)      │
   panel/   ── adversarial panel engine (roles, not models)        │
   ultra_loop/ build→test→panel→fix→re-panel→ship gate             │
   broker/  ── SAFE/ELEVATED/DANGEROUS classification + ledger      │
   proof/   ── proof gates ("done" requires evidence)              │
   evidence/── bounded, redacted source gathering                  │
   artifacts/  uniform run record + JSONL ledger schema            │
   memory/  ── generic write-back hooks (no memory system required)│
                         └─────────────────────────────────────────┘
                                        ▲
                         ┌──────────────┴───────────────┐
                         │           adapters/           │
   cli ── generic shell entrypoint (zero third-party deps)
   litellm_routes ── build a pool over a LiteLLM proxy
   docker_sandbox ── run non-approved commands in a container
   external_memory ── wire hooks to a memory system
   hermes / ktisis ── env-driven wiring for those runtimes
```

## The clean rule: agents are roles, not models

A *panel agent* is a bounded reasoning lens — one prompt-scoped critic call.
A *route* is the model backend that call happens to run on. One healthy model
runs a 4-, 10-, or 16-lens panel by being called once per lens with a
different role. Value comes from diverse **perspectives**, not diverse
providers. Extra healthy routes enable `mixed` mode (critics spread across
backends; a claim's steelman prefers a different backend than its accuser),
which collapses to `single` automatically when only one route is healthy.

## Data flow of one panel run

```
question + context/evidence
   │
   ├─ probe routes ──► healthy set (else PanelError with a health report)
   │
   ├─ CHALLENGE  (parallel critics, one per lens) ──► raw claims
   ├─ dedupe     (deterministic Jaccard merge)
   ├─ STEELMAN   (best good-faith defense of each claim)
   ├─ CROSS-EXAM (judge rules: real_now | real_later | theoretical | wrong)
   ├─ SYNTHESIS  (judge summary + decision)
   │
   └─ proof gates  ◄── ONLY accepted claims' read-only checks
      destructive gates ◄── accepted claims' dangerous checks (split out)
```

Verdicts that come back from a failed judge call are marked `unjudged`, never
silently downgraded to `theoretical`.

## Injection seams

Everything you'd want to fake in a test or swap in production is injected:

- **ChatClient** — `.complete(model, prompt, max_tokens) -> str`. Ships with
  `OpenAIChatClient` and `MockChatClient`.
- **RoutePool** — health, liveness, assignment. Bring one client or a
  per-route `client_map`.
- **UltraLoop** — `builder`, `test_runner`, `fixer` callables.
- **CommandBroker** — `approver`, `sandbox_argv`, `is_read_only`.
- **MemoryHooks** — five generic events, all no-ops by default.

See [panel.md](panel.md), [ultra-loop.md](ultra-loop.md),
[command-broker.md](command-broker.md), [proof-gates.md](proof-gates.md),
[adapter-guide.md](adapter-guide.md), and [security.md](security.md).
