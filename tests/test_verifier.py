"""verifier — refute-first, engine re-check, judge path, budget, binding.

Offline: run_check and judge are injected fakes; no network, no host commands.
"""

import pytest

from agent_ultra.verifier import (
    verify_claim, Verifier, EscalationBudget, BudgetExceeded, claim_sha256,
)
from agent_ultra.verifier.verify import claim_sha256 as csha


# -- injected fakes --------------------------------------------------------

def make_run_check(exit_code):
    calls = []

    def run_check(cmd, cwd):
        calls.append((cmd, cwd))
        return {"exit_code": exit_code, "ledger_ref": "ledger://1"}
    run_check.calls = calls
    return run_check


class FakeJudge:
    """Deterministic judge: returns whatever verdict it was constructed with."""
    def __init__(self, refuted, confidence=0.8, reason="fake"):
        self.payload = {"refuted": refuted, "confidence": confidence,
                        "reason": reason}
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return dict(self.payload)


# -- engine re-check (no LLM) ----------------------------------------------

def test_engine_recheck_pass_confirms():
    rc = make_run_check(0)
    out = verify_claim("tests pass", declared_verify_cmd="pytest -q",
                       run_check=rc)
    assert out["refuted"] is False
    assert out["checks_run"][0]["exit_code"] == 0
    assert len(rc.calls) == 1


def test_engine_recheck_fail_refutes():
    rc = make_run_check(1)
    out = verify_claim("tests pass", declared_verify_cmd="pytest -q",
                       run_check=rc)
    assert out["refuted"] is True


def test_engine_recheck_takes_priority_over_judge():
    rc = make_run_check(0)
    judge = FakeJudge(refuted=True)
    out = verify_claim("tests pass", declared_verify_cmd="pytest -q",
                       run_check=rc, judge=judge)
    assert out["refuted"] is False        # command won
    assert judge.prompts == []            # judge never consulted


# -- judge path ------------------------------------------------------------

def test_judge_can_refute():
    judge = FakeJudge(refuted=True, reason="no evidence")
    out = verify_claim("it works", judge=judge, refs=["log line"])
    assert out["refuted"] is True
    assert "REFUTED" in judge.prompts[0]   # refute-first prompt used


def test_judge_can_confirm():
    judge = FakeJudge(refuted=False, confidence=0.95)
    out = verify_claim("it works", judge=judge, refs=["proof"])
    assert out["refuted"] is False


def test_judge_silence_defaults_refuted():
    def silent_judge(prompt):
        return {}     # no verdict
    out = verify_claim("it works", judge=silent_judge)
    assert out["refuted"] is True


# -- neither channel -------------------------------------------------------

def test_no_channel_defaults_refuted():
    out = verify_claim("it works")
    assert out["refuted"] is True
    assert "insufficient evidence" in out["reason"]


# -- receipt emission + claim binding --------------------------------------

def test_refuted_receipt_uses_non_acceptable_verdict(tmp_path):
    from agent_ultra.receipts_bus import ReceiptsBus, Candidate
    key = tmp_path / "k"; key.write_bytes(b"k" * 32)
    bus = ReceiptsBus(db_path=tmp_path / "r.db", key_path=key)

    def emit(fields):
        return bus.append(**fields)["receipt_id"]

    out = verify_claim("bogus claim", judge=FakeJudge(refuted=True),
                       emit_receipt=emit, session_id="s1")
    rid = out["receipt_id"]
    receipt = bus.get(rid)
    assert receipt["verdict"] == "failed"          # non-acceptable
    # and it cannot satisfy a gate for its own claim
    ok, clause = bus.binds(
        {"session_id": "s1", "claim_sha256": out["claim_sha256"]},
        Candidate(receipt=receipt))
    assert ok is False and clause == "non-acceptable-verdict"


def test_verifier_receipt_binds_only_its_own_claim(tmp_path):
    from agent_ultra.receipts_bus import ReceiptsBus, Candidate
    key = tmp_path / "k"; key.write_bytes(b"k" * 32)
    bus = ReceiptsBus(db_path=tmp_path / "r.db", key_path=key)

    def emit(fields):
        return bus.append(**fields)["receipt_id"]

    # confirm claim A
    out = verify_claim("claim A", declared_verify_cmd="pytest -q",
                       run_check=make_run_check(0), emit_receipt=emit,
                       session_id="s1")
    receipt = bus.get(out["receipt_id"])
    # binds A
    okA, _ = bus.binds({"session_id": "s1", "claim_sha256": csha("claim A")},
                       Candidate(receipt=receipt))
    # never binds B
    okB, clauseB = bus.binds(
        {"session_id": "s1", "claim_sha256": csha("claim B")},
        Candidate(receipt=receipt))
    assert okA is True
    assert okB is False and clauseB == "verifier-claim-mismatch"


# -- budget: dedup + caps --------------------------------------------------

def test_budget_dedup_same_claim_is_free():
    calls = {"n": 0}

    def judge(prompt):
        calls["n"] += 1
        return {"refuted": True, "confidence": 0.5, "reason": "x"}

    v = Verifier(judge=judge, budget=EscalationBudget(per_window=1, per_session=1))
    a = v.escalate("same claim", session_id="s1")
    b = v.escalate("same claim", session_id="s1")   # deduped, no charge
    assert a == b
    assert calls["n"] == 1     # judge only called once


def test_budget_per_window_cap_raises():
    v = Verifier(judge=FakeJudge(refuted=True),
                 budget=EscalationBudget(per_window=2, window_seconds=1000,
                                         per_session=100))
    v.escalate("claim 1", session_id="s1")
    v.escalate("claim 2", session_id="s1")
    with pytest.raises(BudgetExceeded):
        v.escalate("claim 3", session_id="s1")


def test_budget_per_session_cap_raises():
    v = Verifier(judge=FakeJudge(refuted=True),
                 budget=EscalationBudget(per_window=100, per_session=1))
    v.escalate("claim 1", session_id="s1")
    with pytest.raises(BudgetExceeded):
        v.escalate("claim 2", session_id="s1")


def test_claim_sha256_stable():
    assert claim_sha256("x") == claim_sha256("x")
    assert claim_sha256("x") != claim_sha256("y")
