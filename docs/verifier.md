# Verifier

Refute-first verification. The verifier tries to **refute** a completion claim,
not to confirm it, and defaults to *refuted* whenever evidence is insufficient.
A claim only survives if something concrete backs it.

```python
from agent_ultra.verifier import verify_claim

# Engine re-check: a real command is the strongest evidence (no LLM).
verify_claim("tests pass",
             declared_verify_cmd="pytest -q",
             run_check=run_check)          # exit 0 confirms, non-zero refutes

# LLM refute: no declared command -> ask an injected judge with a
# refute-first prompt that defaults to refuted on insufficient evidence.
verify_claim("the fix works", judge=judge, refs=[...])

# Neither channel -> refuted (the honest default).
verify_claim("it works")
```

Both collaborators are **injected** — the kit runs no commands and makes no
network calls by default:

- `run_check(cmd, cwd) -> {"exit_code": int, "ledger_ref": str}`
- `judge(prompt) -> {"refuted": bool, "confidence": float, "reason": str}`

## Result and receipt

`verify_claim` returns `{refuted, confidence, reason, checks_run,
claim_sha256[, receipt_id]}`. When `emit_receipt` is supplied it stamps a
`kind=verifier` receipt with the claim's `claim_sha256`, so the receipt can
only ever resolve the claim it actually checked. A **refuted** verdict maps to
a non-acceptable receipt verdict (`failed`), so it can never satisfy a gate.

## Budget

`Verifier.escalate` is the budgeted entry point. Identical claims (same
`claim_sha256`) are answered from cache and cost nothing; fresh claims are
charged against configurable per-session and per-window caps and raise
`BudgetExceeded` past a cap.

```python
from agent_ultra.verifier import Verifier, EscalationBudget

v = Verifier(run_check=run_check, judge=judge,
             budget=EscalationBudget(per_session=20, per_window=5,
                                     window_seconds=60))
v.escalate("tests pass", session_id="s1", declared_verify_cmd="pytest -q")
```
