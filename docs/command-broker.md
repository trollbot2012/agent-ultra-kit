# Command broker

The security tension — model output should not directly execute arbitrary host
commands — is resolved **not** by forbidding host execution, but by routing
every model-authored command through a broker that classifies it, records it,
and only auto-runs what its tier allows.

## Risk tiers

| tier | examples | trusted-owner | critic mode |
|------|----------|---------------|-------------|
| **SAFE** | read files, list dirs, grep, `git status`/`diff`/`log` | auto-run | auto-run |
| **ELEVATED** | tests, builds, writes, installs, moves, `git commit`, service restarts | auto-run | park for approval |
| **DANGEROUS** | delete, credentials/secrets, deploy, `git push --force`, disable security, spend money, pipe remote → shell | gate | gate |

Unknown commands default to **ELEVATED** — powerful in owner mode, gated in
critic mode. Classification is a heuristic risk router, **not** a sandbox.

## The two modes are just different `auto_run_tiers`

```python
from agent_ultra.broker.broker import (
    CommandBroker, TRUSTED_OWNER_TIERS, CRITIC_TIERS)

owner  = CommandBroker(ledger_path="broker.jsonl", auto_run_tiers=TRUSTED_OWNER_TIERS)
critic = CommandBroker(ledger_path="broker.jsonl", auto_run_tiers=CRITIC_TIERS)

res = owner.run("pytest -q", reason="run the suite")
print(res.risk_tier, res.status, res.exit_code)
```

## The safe default: deny, don't silently park

A **DANGEROUS** command with **no approval path** (no `approver`, no
`sandbox_argv`) is `denied`, loudly, in the ledger. Parking it as
"requires_approval" when nothing can ever approve it is a silent drop, so the
broker refuses to do that.

- provide an `approver(BrokerResult) -> bool` to gate interactively, **or**
- provide a `sandbox_argv(command) -> argv` to run it in a container
  (see the Docker adapter), **or**
- set `allow_dangerous_without_approval=True` to downgrade the deny back to a
  parked `requires_approval` entry. This **never** auto-runs the command — it
  only changes `denied` to `requires_approval`.

```python
b = CommandBroker(auto_run_tiers=TRUSTED_OWNER_TIERS)
b.run("rm -rf build").status                      # 'denied'

b = CommandBroker(auto_run_tiers=TRUSTED_OWNER_TIERS, approver=my_gate)
b.run("rm -rf build")                             # asks my_gate

b = CommandBroker(auto_run_tiers=TRUSTED_OWNER_TIERS,
                  sandbox_argv=docker_sandbox_argv())
b.run("rm -rf build")                             # runs in a container
```

## Ledger

Every call appends one JSON line: command, cwd, reason, expected effect, tier,
tier reason, status, exit code, output, backend, timestamp. Output is passed
through secret redaction before writing. The command line is stored verbatim
for audit exactness — **do not put secrets on command lines**.

## Custom read-only test

Pass `is_read_only=your_fn` for a stricter pure-read gate than the built-in
pipeline check (the panel injects a hardened structural gate for
critic-proposed checks, for instance).
