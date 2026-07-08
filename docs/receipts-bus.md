# Receipts bus

A unified, **authenticated** receipt index. Every unit of agent work can leave
a signed receipt — a record of what was done, what it claimed, and what
evidence backs the claim. The bus stores receipts, verifies them, and resolves
a claim to the receipts that actually *bind* to it.

```python
from agent_ultra.receipts_bus import ReceiptsBus, Candidate

bus = ReceiptsBus(db_path="receipts.db", key_path="install.key")

env = bus.append(kind="panel", actor="engine_a", verdict="shipped",
                 session_id="s1", workspace="/repo")

bus.verify(env["receipt_id"])
# -> {ok, authentic, integrity, refs_ok, receipt}
```

## Authenticity is separate from integrity

Two hashes cover the canonical body (the envelope minus its two hash fields,
`json.dumps(sort_keys=True)`):

- **`receipt_sha256`** — SHA-256. Integrity only: proves the body was not
  altered.
- **`receipt_hmac`** — HMAC-SHA256 keyed by a per-install key read from
  `key_path`. This is the authenticity control.

A hand-authored envelope with a correct `receipt_sha256` but no/invalid HMAC is
`authentic=False`, and any enforce-mode consumer must reject it (`ok=False`).

> **Key custody.** The host is responsible for fencing `key_path` from
> untrusted writers. Honest residual: a same-user process that can *read* the
> key can still forge an authentic receipt — closing that needs an
> out-of-process signer, which is out of scope for v1.

## A failed read is never empty

The store is SQLite in WAL mode with the **default** auto-checkpoint left in
place. A read that fails after a bounded retry raises `BusUnavailable` — it
never degrades to `[]`, because a caller cannot tell "no receipts" from "the
store was unreachable" and would wrongly conclude a claim is unsupported.

## Binding rule

`resolve(claim_ctx)` returns origin-annotated `Candidate`s scoped by
session / workspace / window (optionally federating external sources via
injected readers). `binds(claim_ctx, candidate) -> (bool, clause)` decides
which candidates back a **completion** claim:

- an explicitly **cited** receipt id, or
- a canonical **verify-command** match with scope coverage **and** freshness, or
- a **task_ref** match.

Never binds: a `command`-kind candidate to a completion claim alone; a
`verifier`-kind candidate unless its `claim_sha256` equals the claim's;
`manual` / `repair` / `route_health` to completion; anything with a
non-acceptable verdict; anything unauthenticated. Weak-but-present evidence
(uncovered / stale) returns the distinct `ledger-weak` clause — not a pass.

## Gate audit (hash chain)

```python
bus.append_audit("ship", "enforce", "allow", "engine_a", "tests green")
bus.verify_audit("ship")          # -> {ok, first_broken}
bus.get_audit_event(event_id)     # explain one row
```

Each row carries `prev_sha256` + `row_sha256`; `verify_audit` recomputes the
chain and reports the first broken link.

## CLI

```
agent-ultra receipts list   [--session S] [--workspace W]
agent-ultra receipts show    RECEIPT_ID
agent-ultra receipts verify  RECEIPT_ID
agent-ultra receipts why     [--claim-sha ...] [--verify-command ...] [--cited ...]
agent-ultra receipts attest  # interactive only; writes kind=manual
```

`attest` is the only creation path outside engine code. It refuses
non-interactive use and writes a `kind=manual` receipt — and manual receipts
never satisfy a completion gate.
