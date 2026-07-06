# Proof gates

"Done" requires evidence. A proof gate is a named requirement that must be
satisfied by **recorded** evidence before work is declared complete. This is
the mechanical enforcement of "no unsupported completion claims."

```python
from agent_ultra.proof.gates import GateSet, Evidence

gs = GateSet()
gs.add("tests pass", gate_id="TESTS")
gs.add("confirm the fix", check="grep -n validate_token app.py", gate_id="FIX")

gs.get("TESTS").satisfy(Evidence(kind="test_report", ref="pytest", summary="exit 0"))

gs.all_satisfied          # False — FIX still open
gs.assert_shippable()     # raises ProofError listing the open gate
```

## From panel findings

Accepted findings that carry a read-only `check` become gates automatically:

```python
from agent_ultra.proof.gates import gates_from_findings
gs = gates_from_findings(report.findings)
```

## Running checks through the broker

```python
gs.run_checks(broker)     # SAFE checks auto-run and satisfy their gate;
                          # side-effectful checks come back requires_approval;
                          # destructive checks are never sent.
```

## Destructive checks are split out

A check that classifies **DANGEROUS** is flagged `destructive=True`, is never
auto-run, and is never presented as safe-to-paste proof. It surfaces as a
`destructive_gate` for a human to review. This mirrors the panel's own
`report.destructive_gates`.

## Persist the trail

```python
gs.save(".ultra/proof_gates.json")
```
