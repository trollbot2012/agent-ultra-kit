"""Proof gates — completion requires evidence."""

import pytest

from agent_ultra.proof.gates import GateSet, ProofGate, ProofError, Evidence, gates_from_findings


def test_unsatisfied_gate_blocks_ship():
    gs = GateSet()
    gs.add("tests pass")
    assert not gs.all_satisfied
    with pytest.raises(ProofError):
        gs.assert_shippable()


def test_satisfied_gate_allows_ship():
    gs = GateSet()
    g = gs.add("tests pass")
    g.satisfy(Evidence(kind="test_report", ref="pytest", summary="exit 0"))
    assert gs.all_satisfied
    gs.assert_shippable()  # no raise


def test_destructive_check_is_flagged():
    gs = GateSet()
    g = gs.add("clean the build dir", check="rm -rf build")
    assert g.destructive is True


def test_safe_check_not_flagged_destructive():
    gs = GateSet()
    g = gs.add("confirm file exists", check="test -f app.py")
    assert g.destructive is False


def test_gates_from_findings_only_accepted_with_checks():
    class F:
        def __init__(self, accepted, check, lens="x", claim="c"):
            self.accepted = accepted
            self.check = check
            self.lens = lens
            self.claim = claim
    findings = [
        F(True, "grep -n token app.py"),
        F(False, "grep -n skip app.py"),   # not accepted -> excluded
        F(True, ""),                        # no check -> excluded
        F(True, "rm -rf build"),            # destructive -> present but flagged
    ]
    gs = gates_from_findings(findings)
    assert len(gs.gates) == 2
    assert any(g.destructive for g in gs.gates)


def test_run_checks_via_broker(tmp_path):
    from agent_ultra.broker.broker import CommandBroker, TRUSTED_OWNER_TIERS
    gs = GateSet()
    gs.add("echo proof", check="echo ok", gate_id="G1")
    broker = CommandBroker(ledger_path=tmp_path / "l.jsonl",
                           auto_run_tiers=TRUSTED_OWNER_TIERS)
    gs.run_checks(broker)
    assert gs.get("G1").status == "satisfied"


def test_save_writes_json(tmp_path):
    gs = GateSet()
    gs.add("something")
    p = gs.save(tmp_path / "gates.json")
    assert p.exists()
