"""Tests for structural PANEL enforcement in the public kit.

A phase labelled PANEL is not proof. Only a valid panel execution receipt with
real executed lenses satisfies PANEL. Covers: missing/zero-lens/malformed/
forged receipts blocked, self-review rejected, real receipt accepted, checksum
integrity, and the UltraReport enforcement status.
"""

from __future__ import annotations

import json

import pytest

from agent_ultra.panel_receipt import (build_receipt, write_receipt,
                                       validate_receipt, gate_report,
                                       RECEIPT_NAME, ERR_ZERO_AGENTS,
                                       ERR_REPORT_MISSING, ERR_REPORT_ZERO)


class _Finding:
    def __init__(self, lens, verdict="theoretical", severity="", claim="c"):
        self.lens = lens
        self.verdict = verdict
        self.severity = severity
        self.claim = claim
        self.origin_route = "route-a"

    @property
    def accepted(self):
        return self.verdict in ("real_now", "real_later")


class _Report:
    def __init__(self, lenses, findings, model_calls=6, errors=None):
        self.lenses = list(lenses)
        self.findings = list(findings)
        self.model_calls = model_calls
        self.errors = list(errors or [])
        self.routes = {"judge": "route-a"}
        self.run_id = "panel-abc123"

    @property
    def accepted(self):
        return [f for f in self.findings if f.accepted]


REVIEWED = "def f():\n    return 1\n" * 20


def _write(tmp_path, report):
    r = build_receipt(report, task_id="demo-task", reviewed_input=REVIEWED,
                      panel_artifact_path=str(tmp_path))
    write_receipt(tmp_path, r)
    return r


# valid real/mock panel receipt passes
def test_valid_panel_receipt_accepted(tmp_path):
    report = _Report(lenses=["security", "correctness", "failure-modes"],
                     findings=[_Finding("security", "real_now", "critical"),
                               _Finding("correctness", "theoretical")])
    r = _write(tmp_path, report)
    assert r["lens_count_executed"] == 3 and r["model_calls"] == 6
    assert r["artifact_hash"]      # bound to reviewed input
    ok, msg = validate_receipt(tmp_path, expect_task_id="demo-task")
    assert ok and msg == "PANEL valid."
    allowed, gmsg = gate_report(tmp_path)
    assert allowed and "REPORT allowed" in gmsg


# missing receipt blocks report
def test_missing_receipt_blocks_report(tmp_path):
    allowed, msg = gate_report(tmp_path)
    assert not allowed and msg == ERR_REPORT_MISSING


# zero-lens receipt blocks report
def test_zero_lens_receipt_blocks_report(tmp_path):
    (tmp_path / RECEIPT_NAME).write_text(json.dumps({
        "lens_count_executed": 0, "lenses": []}), encoding="utf-8")
    allowed, msg = gate_report(tmp_path)
    assert not allowed and msg == ERR_REPORT_ZERO


# malformed receipt blocks report
def test_malformed_receipt_blocks_report(tmp_path):
    (tmp_path / RECEIPT_NAME).write_text("not json {", encoding="utf-8")
    allowed, msg = gate_report(tmp_path)
    assert not allowed and msg == ERR_REPORT_MISSING


# self-review without panel calls does not count
def test_self_review_zero_calls_rejected(tmp_path):
    fake = _Report(lenses=["security", "correctness"], findings=[],
                   model_calls=0)
    r = _write(tmp_path, fake)
    assert r["lens_count_executed"] == 0 and r["final_verdict"] == "blocked"
    ok, msg = validate_receipt(tmp_path)
    assert not ok and msg == ERR_ZERO_AGENTS
    allowed, gmsg = gate_report(tmp_path)
    assert not allowed and gmsg == ERR_REPORT_ZERO


# forged/tampered checksum blocks report
def test_tampered_checksum_blocks(tmp_path):
    report = _Report(lenses=["security", "correctness"],
                     findings=[_Finding("security", "real_now", "critical")])
    _write(tmp_path, report)
    p = tmp_path / RECEIPT_NAME
    data = json.loads(p.read_text(encoding="utf-8"))
    data["lens_count_executed"] = 99   # forge more lenses than really ran
    p.write_text(json.dumps(data), encoding="utf-8")
    ok, _ = validate_receipt(tmp_path)
    assert not ok


# hand-forged receipt omitting the checksum is rejected (mandatory checksum)
def test_hand_forged_receipt_without_checksum_rejected(tmp_path):
    forged = {
        "panel_id": "x", "task_id": "t", "timestamp": "2026-01-01T00:00:00Z",
        "model_used": "m", "artifact_hash": "h", "lens_count_executed": 5,
        "lenses": [{"lens_name": "security", "status": "ok",
                    "output_hash": "deadbeef"}],
        "final_verdict": "ship",
    }
    (tmp_path / RECEIPT_NAME).write_text(json.dumps(forged), encoding="utf-8")
    ok, msg = validate_receipt(tmp_path)
    assert not ok and msg == ERR_REPORT_MISSING


# errored lenses are not counted as executed
def test_errored_lenses_not_counted(tmp_path):
    report = _Report(
        lenses=["security", "correctness", "failure-modes"],
        findings=[_Finding("security", "real_now", "high")],
        errors=[{"phase": "challenge", "label": "correctness"},
                {"phase": "challenge", "label": "failure-modes"}])
    r = _write(tmp_path, report)
    assert r["lens_count_executed"] == 1 and r["lens_count_errored"] == 2
    ok, _ = validate_receipt(tmp_path)
    assert ok


# the ULTRA loop records panel enforcement status end to end
def test_ultra_report_records_enforcement(tmp_path):
    from agent_ultra.ultra_loop.loop import UltraLoop, TestResult
    from agent_ultra.panel.engine import PanelEngine
    from agent_ultra.routes.pool import RoutePool
    from agent_ultra.routes.mock import demo_panel_client
    from agent_ultra.broker.broker import CommandBroker, TRUSTED_OWNER_TIERS

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svc.py").write_text(
        "def handler(req):\n    return req.get('token')\n" * 12,
        encoding="utf-8")
    engine = PanelEngine(RoutePool(["mock-a"], client=demo_panel_client()))
    broker = CommandBroker(ledger_path=ws / ".ultra" / "b.jsonl",
                           auto_run_tiers=TRUSTED_OWNER_TIERS)

    def tests_ok(w, c, t):
        return TestResult(c or "pytest", 0, True, "3 passed", 0.1)

    loop = UltraLoop(ws, panel=engine, broker=broker, test_runner=tests_ok,
                     fixer=lambda ft, w: True)
    rep = loop.run("token auth handler", risk="high", test_cmd="pytest -q")
    assert rep.panel_ran is True
    assert rep.panel_enforced is True          # real mock panel -> valid receipt
    assert rep.receipt_path
    receipt = json.loads((tmp_path / "ws" / ".ultra").rglob(RECEIPT_NAME).__next__()
                         .read_text(encoding="utf-8"))
    assert receipt["lens_count_executed"] > 0
    assert receipt["model_calls"] > 0
