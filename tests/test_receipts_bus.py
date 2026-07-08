"""receipts_bus — authenticity, tamper, binding, availability, gate audit.

All offline: the store is SQLite on disk (tmp_path) and the HMAC key is a
tmp file. No network, no injected LLM here (the bus makes none).
"""

import json
import sqlite3

import pytest

from agent_ultra.receipts_bus import (
    ReceiptsBus, BusUnavailable, build_envelope, EnvelopeError,
)
from agent_ultra.receipts_bus.envelope import verify_hashes, receipt_sha256


def _bus(tmp_path, **kw):
    key = tmp_path / "install.key"
    key.write_bytes(b"per-install-secret-key-0123456789")
    return ReceiptsBus(db_path=tmp_path / "receipts.db", key_path=key, **kw)


# -- enums -----------------------------------------------------------------

def test_closed_enums_reject_unknown_members():
    with pytest.raises(EnvelopeError):
        build_envelope(kind="not-a-kind", actor="broker", verdict="shipped")
    with pytest.raises(EnvelopeError):
        build_envelope(kind="panel", actor="nobody", verdict="shipped")
    with pytest.raises(EnvelopeError):
        build_envelope(kind="panel", actor="broker", verdict="maybe")


# -- authenticity vs integrity (the forgery case) --------------------------

def test_authentic_separate_from_integrity_forgery_rejected(tmp_path):
    bus = _bus(tmp_path)
    # A hand-authored envelope with a CORRECT sha256 but NO hmac.
    forged = build_envelope(kind="panel", actor="engine_a", verdict="shipped")
    forged["receipt_hmac"] = ""                      # strip authenticity
    forged["receipt_sha256"] = receipt_sha256(forged)  # but keep integrity valid
    bus.put(forged)
    res = bus.verify(forged["receipt_id"])
    assert res["integrity"] is True      # sha256 checks out
    assert res["authentic"] is False     # but it is NOT authentic
    assert res["ok"] is False            # enforce-mode rejects it


def test_forged_hmac_rejected(tmp_path):
    bus = _bus(tmp_path)
    env = bus.append(kind="panel", actor="engine_a", verdict="shipped")
    env["receipt_hmac"] = "deadbeef" * 8   # wrong key / made up
    bus.put(env)
    res = bus.verify(env["receipt_id"])
    assert res["authentic"] is False


def test_genuine_receipt_is_authentic(tmp_path):
    bus = _bus(tmp_path)
    env = bus.append(kind="panel", actor="engine_a", verdict="shipped")
    res = bus.verify(env["receipt_id"])
    assert res["integrity"] and res["authentic"] and res["ok"]


# -- tamper detection ------------------------------------------------------

def test_tamper_breaks_integrity(tmp_path):
    bus = _bus(tmp_path)
    env = bus.append(kind="panel", actor="engine_a", verdict="hold")
    env["verdict"] = "shipped"    # flip the body, leave the old hashes
    bus.put(env)
    res = bus.verify(env["receipt_id"])
    assert res["integrity"] is False
    assert res["ok"] is False


# -- BusUnavailable never empty --------------------------------------------

def test_corrupt_store_raises_not_empty(tmp_path):
    bus = _bus(tmp_path)
    bus.append(kind="panel", actor="engine_a", verdict="shipped")
    # Corrupt the DB file so a read errors.
    (tmp_path / "receipts.db").write_bytes(b"not a sqlite database at all")
    with pytest.raises(BusUnavailable):
        bus.list()


def test_missing_receipt_is_none_not_error(tmp_path):
    bus = _bus(tmp_path)
    assert bus.get("does-not-exist") is None


def test_concurrent_writers_both_resolve(tmp_path):
    # Two bus instances over the same on-disk store both persist.
    bus_a = _bus(tmp_path)
    bus_b = ReceiptsBus(db_path=tmp_path / "receipts.db",
                        key_path=tmp_path / "install.key")
    a = bus_a.append(kind="panel", actor="engine_a", verdict="shipped",
                     session_id="s1")
    b = bus_b.append(kind="loop", actor="engine_b", verdict="completed",
                     session_id="s1")
    ids = {r["receipt_id"] for r in bus_a.list(session_id="s1")}
    assert a["receipt_id"] in ids and b["receipt_id"] in ids


# -- binding rule ----------------------------------------------------------

def test_cited_binds(tmp_path):
    bus = _bus(tmp_path)
    env = bus.append(kind="panel", actor="engine_a", verdict="shipped",
                     session_id="s1")
    ctx = {"session_id": "s1", "cited": env["receipt_id"]}
    cand = bus.resolve(ctx)[0]
    ok, clause = bus.binds(ctx, cand)
    assert ok and clause == "cited"


def test_command_never_satisfies_completion(tmp_path):
    bus = _bus(tmp_path)
    env = bus.append(kind="command", actor="broker", verdict="completed",
                     session_id="s1", canonical_command="pytest -q")
    ctx = {"session_id": "s1", "verify_command": "pytest -q"}
    from agent_ultra.receipts_bus import Candidate
    ok, clause = bus.binds(ctx, Candidate(receipt=env))
    assert ok is False
    assert clause == "command-not-completion"


def test_verifier_binds_only_on_claim_hash_match(tmp_path):
    bus = _bus(tmp_path)
    env = bus.append(kind="verifier", actor="reviewer", verdict="completed",
                     session_id="s1", claim_sha256="AAAA")
    from agent_ultra.receipts_bus import Candidate
    ok_match, cl_match = bus.binds(
        {"session_id": "s1", "claim_sha256": "AAAA"}, Candidate(receipt=env))
    ok_other, cl_other = bus.binds(
        {"session_id": "s1", "claim_sha256": "BBBB"}, Candidate(receipt=env))
    assert ok_match and cl_match == "verifier-claim-match"
    assert not ok_other and cl_other == "verifier-claim-mismatch"


def test_verify_command_scope_and_freshness(tmp_path):
    bus = _bus(tmp_path)
    fresh = bus.append(kind="verification", actor="engine_a", verdict="completed",
                       session_id="s1", workspace="/w",
                       canonical_command="pytest -q")
    from agent_ultra.receipts_bus import Candidate
    ok, clause = bus.binds(
        {"session_id": "s1", "workspace": "/w", "verify_command": "pytest -q"},
        Candidate(receipt=fresh))
    assert ok and clause == "verify-command"
    # scope mismatch -> weak, not a pass
    ok2, clause2 = bus.binds(
        {"session_id": "s1", "workspace": "/other", "verify_command": "pytest -q"},
        Candidate(receipt=fresh))
    assert not ok2 and clause2 == "ledger-weak"


def test_stale_verify_command_is_ledger_weak(tmp_path):
    bus = _bus(tmp_path, freshness_seconds=0)
    env = bus.append(kind="verification", actor="engine_a", verdict="completed",
                     session_id="s1", canonical_command="pytest -q")
    import time as _t
    _t.sleep(1.1)
    from agent_ultra.receipts_bus import Candidate
    ok, clause = bus.binds(
        {"session_id": "s1", "verify_command": "pytest -q"},
        Candidate(receipt=env))
    assert not ok and clause == "ledger-weak"


def test_task_ref_binds(tmp_path):
    bus = _bus(tmp_path)
    env = bus.append(kind="loop", actor="engine_a", verdict="completed",
                     session_id="s1", task_ref={"issue": "ABC-1"})
    from agent_ultra.receipts_bus import Candidate
    ok, clause = bus.binds(
        {"session_id": "s1", "task_ref": {"issue": "ABC-1"}},
        Candidate(receipt=env))
    assert ok and clause == "task-ref"


def test_manual_never_completes(tmp_path):
    bus = _bus(tmp_path)
    env = bus.append(kind="manual", actor="operator", verdict="approved",
                     session_id="s1", task_ref={"issue": "ABC-1"})
    from agent_ultra.receipts_bus import Candidate
    ok, clause = bus.binds(
        {"session_id": "s1", "task_ref": {"issue": "ABC-1"}},
        Candidate(receipt=env))
    assert ok is False
    assert clause == "kind-cannot-complete"


def test_non_acceptable_verdict_never_binds(tmp_path):
    bus = _bus(tmp_path)
    env = bus.append(kind="verifier", actor="reviewer", verdict="failed",
                     session_id="s1", claim_sha256="AAAA")
    from agent_ultra.receipts_bus import Candidate
    ok, clause = bus.binds(
        {"session_id": "s1", "claim_sha256": "AAAA"}, Candidate(receipt=env))
    assert ok is False and clause == "non-acceptable-verdict"


def test_unauthenticated_candidate_never_binds(tmp_path):
    bus = _bus(tmp_path)
    env = build_envelope(kind="panel", actor="engine_a", verdict="shipped",
                        session_id="s1")            # no key -> no hmac
    env["receipt_sha256"] = receipt_sha256(env)
    bus.put(env)
    from agent_ultra.receipts_bus import Candidate
    ok, clause = bus.binds({"session_id": "s1", "cited": env["receipt_id"]},
                          Candidate(receipt=env))
    assert ok is False and clause == "unauthenticated"


def test_federated_reader_annotates_origin(tmp_path):
    def external(ctx):
        env = build_envelope(kind="panel", actor="engine_a", verdict="shipped")
        return [env]
    bus = _bus(tmp_path)
    bus.federated_readers["external"] = external
    origins = {c.origin for c in bus.resolve({"session_id": "s1"})}
    assert "external" in origins


# -- gate_audit hash chain -------------------------------------------------

def test_gate_audit_chain_verifies(tmp_path):
    bus = _bus(tmp_path)
    bus.append_audit("ship", "enforce", "allow", "engine_a", "tests green")
    bus.append_audit("ship", "enforce", "block", "reviewer", "stale evidence")
    out = bus.verify_audit("ship")
    assert out["ok"] is True and out["first_broken"] is None


def test_gate_audit_tamper_detected(tmp_path):
    bus = _bus(tmp_path)
    e1 = bus.append_audit("ship", "enforce", "allow", "engine_a", "green")
    bus.append_audit("ship", "enforce", "block", "reviewer", "stale")
    # Tamper with the first row's reason directly in the DB.
    conn = sqlite3.connect(str(tmp_path / "receipts.db"))
    conn.execute("UPDATE gate_audit SET reason='forged' WHERE event_id=?", (e1,))
    conn.commit()
    conn.close()
    out = bus.verify_audit("ship")
    assert out["ok"] is False
    assert out["first_broken"] == e1


def test_get_audit_event(tmp_path):
    bus = _bus(tmp_path)
    eid = bus.append_audit("ship", "enforce", "allow", "operator", "ok")
    ev = bus.get_audit_event(eid)
    assert ev["decision"] == "allow" and ev["gate"] == "ship"
