"""Proof gates — "done" requires evidence.

A proof gate is a named requirement that must be satisfied by RECORDED
evidence before work may be declared complete. Gates come from two places:

  - accepted panel findings that carry a `check` command, and
  - requirements the caller registers directly (tests pass, artifact exists).

Gates whose check command classifies DANGEROUS are split out as destructive
gates: they are never presented as safe-to-paste proof and never auto-run.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from ..broker.broker import classify, DANGEROUS


class ProofError(Exception):
    """Completion was claimed while gates remain unsatisfied."""


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Evidence:
    kind: str            # command_output | file | test_report | url | note
    ref: str = ""        # command line, file path, url...
    summary: str = ""
    ts: str = field(default_factory=_utc_now)


@dataclass
class ProofGate:
    id: str
    description: str
    check: str = ""              # optional command that proves/disproves
    destructive: bool = False    # check classifies DANGEROUS — never auto-run
    status: str = "open"         # open | satisfied | failed | requires_approval
    evidence: list = field(default_factory=list)   # Evidence dicts

    def satisfy(self, evidence: Evidence) -> None:
        self.evidence.append(asdict(evidence))
        self.status = "satisfied"

    def fail(self, evidence: Evidence) -> None:
        self.evidence.append(asdict(evidence))
        self.status = "failed"


class GateSet:
    """The gates for one unit of work, with an artifact trail."""

    def __init__(self, gates: list[ProofGate] | None = None):
        self.gates: list[ProofGate] = list(gates or [])

    def add(self, description: str, check: str = "", gate_id: str = "") -> ProofGate:
        gid = gate_id or f"GATE-{len(self.gates) + 1}"
        destructive = bool(check) and classify(check)[0] == DANGEROUS
        gate = ProofGate(id=gid, description=description, check=check,
                         destructive=destructive)
        self.gates.append(gate)
        return gate

    def get(self, gate_id: str) -> ProofGate | None:
        return next((g for g in self.gates if g.id == gate_id), None)

    def unresolved(self) -> list[ProofGate]:
        return [g for g in self.gates if g.status not in ("satisfied",)]

    @property
    def all_satisfied(self) -> bool:
        return not self.unresolved()

    def assert_shippable(self) -> None:
        """Raise ProofError unless every gate is satisfied. This is the
        no-unsupported-completion-claims rule, mechanically enforced."""
        open_gates = self.unresolved()
        if open_gates:
            lines = "; ".join(f"{g.id} [{g.status}] {g.description}"
                              for g in open_gates)
            raise ProofError(f"{len(open_gates)} unsatisfied proof gate(s): {lines}")

    def run_checks(self, broker) -> None:
        """Execute each gate's check through a CommandBroker. SAFE checks
        auto-run in critic mode; side-effectful checks come back
        requires_approval; destructive gates are never sent at all."""
        for g in self.gates:
            if not g.check or g.destructive or g.status == "satisfied":
                continue
            res = broker.run(g.check, reason=f"proof gate {g.id}",
                             expected_effect="proves or disproves the gate")
            ev = Evidence(kind="command_output", ref=g.check,
                          summary=f"{res.status} (exit {res.exit_code}) "
                                  f"{res.output[:200]}")
            if res.status == "passed":
                g.satisfy(ev)
            elif res.status in ("requires_approval", "denied", "rejected"):
                g.evidence.append(asdict(ev))
                g.status = "requires_approval"
            else:
                g.fail(ev)

    def to_dict(self) -> dict:
        return {"gates": [asdict(g) for g in self.gates],
                "all_satisfied": self.all_satisfied}

    def save(self, path: Path | str) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                      encoding="utf-8")
        return p


def gates_from_findings(findings) -> GateSet:
    """Build gates from ACCEPTED panel findings that carry a check command.
    Destructive checks are kept but flagged; they require a human."""
    gs = GateSet()
    seen: set[str] = set()
    for i, f in enumerate(findings):
        check = (getattr(f, "check", "") or "").strip()
        if not getattr(f, "accepted", False) or not check or check in seen:
            continue
        seen.add(check)
        gs.add(description=f"[{f.lens}] {f.claim[:140]}", check=check,
               gate_id=f"GATE-F{i + 1}")
    return gs
