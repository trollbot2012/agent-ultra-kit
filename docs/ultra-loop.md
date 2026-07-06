# ULTRA loop

Tests prove the **known** contract; the adversarial panel finds the
**unknown** failure modes. Green tests alone are not enough for risky code — a
service can pass every test and still ship an auth bypass.

```
build → test → panel → classify → fix → re-test → re-panel → ship-gate
```

1. **BUILD** (optional) — a coding agent implements the task.
2. **TEST** — run tests; record cmd + exit + output as proof. Red tests stop
   the loop *before* the panel (fix the known contract first).
3. **PANEL** — adversarial review, only when risk-worthy, always with real
   source context. Low-context panels are refused, not run weakly.
4. **CLASSIFY** — keep `real_now`/`real_later` verdicts after steelman +
   cross-exam. That stage kills weak findings; it is load-bearing.
5. **FIX** — accepted critical/high `real_now` findings become proof-gated fix
   tasks; the injected fixer applies them; tests rerun.
6. **RE-PANEL** — a small panel confirms the fix.
7. **SHIP** — only when tests pass **and** no critical/high `real_now`
   findings remain **and** artifacts exist. Residual risks are recorded.

## Usage

```python
from agent_ultra.ultra_loop.loop import UltraLoop
from agent_ultra.broker.broker import CommandBroker, TRUSTED_OWNER_TIERS

broker = CommandBroker(ledger_path=".ultra/broker.jsonl",
                       auto_run_tiers=TRUSTED_OWNER_TIERS)
loop = UltraLoop(
    workspace=".",
    panel=engine,                    # a PanelEngine
    broker=broker,
    builder=my_builder,              # optional: (workspace, task) -> str
    fixer=my_fixer,                  # optional: (fix_task, workspace) -> bool
)
report = loop.run("add token auth", risk="high", test_cmd="pytest -q")
print(report.shipped, report.ship_reason)
```

## Injection seams

- **builder(workspace, task) -> str** — drive your coding agent. Optional.
- **test_runner(workspace, cmd, timeout) -> TestResult** — defaults to a
  broker-backed runner (tests are ELEVATED and auto-run in owner mode).
- **fixer(fix_task, workspace) -> bool** — drive your agent to fix one
  finding. Without a fixer the loop still reports and gates; it just doesn't
  auto-fix.

## Risk → panel size

`small`→small, `medium`→medium, `high`→medium (or large with
`allow_large=True`). A low nominal risk still triggers a panel when the task or
context touches a sensitive keyword (auth, sql, exec, crypto, …).

## Artifacts

Every run writes a JSON + Markdown record and a `proof_gates.json` under
`<workspace>/.ultra/runs/<stamp>-<slug>/`.
