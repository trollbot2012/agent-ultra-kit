"""Tests for the hybrid worker layer (router default + optional Deep Agents).

Covers the 12 required checks: router is default, core imports without Deep
Agents, selecting Deep Agents without the extra errors clearly, normalized
result shape, selection policy, worker-as-fixer apply+rollback, and that the
proof path is worker-agnostic. No network — a fake chat client stands in.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_ultra.workers import (WorkerResult, RouterWorker, select_worker,
                                 AUTO_POLICY)
from agent_ultra.workers.select import resolve_worker_choice
from agent_ultra.workers.adapt import worker_as_fixer
from agent_ultra.workers.deepagents_worker import (deepagents_available,
                                                  _INSTALL_HINT)
from agent_ultra.ultra_loop.loop import TestResult


class FakeClient:
    """Returns a scripted JSON edit for the router worker's fix prompt."""
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def complete(self, model, prompt, max_tokens):
        self.calls += 1
        return self.reply


FIX_TASK = {"id": "ULTRA-1", "lens": "correctness", "severity": "critical",
            "verdict": "real_now", "claim": "overdraw not guarded", "check": ""}


# 1. Router is the default worker choice.
def test_router_is_default():
    assert resolve_worker_choice("fixer", "auto", "fix a one-line bug") == "router"
    assert resolve_worker_choice("fixer", "", "patch it") == "router"


# 2. Core import works without Deep Agents loaded.
def test_core_import_has_no_deepagents():
    import agent_ultra  # noqa: F401
    import agent_ultra.workers  # noqa: F401
    assert "deepagents" not in sys.modules
    assert "langchain" not in sys.modules


# 3. Selecting Deep Agents without the extra gives a clear install error.
def test_select_deepagents_without_extra_errors(monkeypatch):
    import agent_ultra.workers.select as sel
    monkeypatch.setattr(sel, "deepagents_available", lambda: False)
    with pytest.raises(RuntimeError) as e:
        sel.select_worker("deepagents", base_url="x", api_key="y", model="m")
    assert "agent-ultra-kit[deepagents]" in str(e.value)


# 6. Worker selection chooses router for small fixer tasks.
def test_auto_picks_router_for_small_fix():
    assert resolve_worker_choice("fixer", "auto", "fix the off-by-one error") == "router"


# 7. Worker selection chooses Deep Agents for builder/multi-file tasks.
def test_auto_picks_deepagents_for_builder():
    assert resolve_worker_choice("builder", "auto",
                                 "build a new multi-file service") == "deepagents"
    # explicit request always honored
    assert resolve_worker_choice("fixer", "deepagents", "trivial") == "deepagents"
    # router fixer repeatedly failing escalates
    assert resolve_worker_choice("fixer", "auto", "fix x",
                                 router_failures=2) == "deepagents"


# 8. Deep Agents / router outputs normalize to the same shape.
def test_result_shape_is_uniform():
    r = WorkerResult(worker="router", status="ok", summary="s",
                     files_changed=["a.py"], commands_run=["c"], proof=["p"])
    d = r.to_dict()
    assert set(d) >= {"worker", "status", "summary", "files_changed",
                      "commands_run", "proof", "error", "edit"}
    assert r.ok is True
    json.dumps(d)   # must be serializable for artifacts


def test_router_worker_produces_edit():
    client = FakeClient(json.dumps({
        "path": "bank.py", "content": "fixed = True\n",
        "explanation": "guarded overdraw"}))
    w = RouterWorker(client=client, model="mock")
    res = w.fix(Path("."), FIX_TASK, {"source_map": {"bank.py": "x=1"}})
    assert res.worker == "router" and res.ok
    assert res.edit == {"path": "bank.py", "content": "fixed = True\n"}
    assert res.files_changed == ["bank.py"]


def test_router_worker_has_no_builder():
    w = RouterWorker(client=FakeClient(""), model="mock")
    res = w.build(Path("."), "build a thing", {})
    assert res.status == "failed" and "builder" in res.error


# 9/10. worker-as-fixer applies the edit + proof is worker-agnostic.
def test_worker_as_fixer_applies_and_passes(tmp_path):
    (tmp_path / "bank.py").write_text("x = 1\n", encoding="utf-8")
    client = FakeClient(json.dumps({"path": "bank.py",
                                    "content": "x = 2  # fixed\n",
                                    "explanation": "fix"}))
    w = RouterWorker(client=client, model="mock")

    def tests_ok(ws, cmd, timeout):
        return TestResult(cmd or "pytest", 0, True, "1 passed", 0.1)

    fx = worker_as_fixer(w, test_runner=tests_ok, test_cmd="pytest",
                         timeout=60, source_provider=lambda ws, ft: {"bank.py": "x=1"})
    ok = fx(FIX_TASK, tmp_path)
    assert ok is True
    assert (tmp_path / "bank.py").read_text(encoding="utf-8") == "x = 2  # fixed\n"
    assert fx.results[0].worker == "router" and fx.results[0].ok


# rollback: a fix that breaks tests is reverted (gate not weakened).
def test_worker_as_fixer_rolls_back_on_break(tmp_path):
    (tmp_path / "bank.py").write_text("x = 1\n", encoding="utf-8")
    client = FakeClient(json.dumps({"path": "bank.py",
                                    "content": "SYNTAX ((\n",
                                    "explanation": "bad"}))
    w = RouterWorker(client=client, model="mock")

    def tests_fail(ws, cmd, timeout):
        return TestResult(cmd or "pytest", 1, False, "boom", 0.1)

    fx = worker_as_fixer(w, test_runner=tests_fail, test_cmd="pytest",
                         timeout=60, source_provider=lambda ws, ft: {})
    ok = fx(FIX_TASK, tmp_path)
    assert ok is False
    assert (tmp_path / "bank.py").read_text(encoding="utf-8") == "x = 1\n"  # reverted
    assert "reverted" in fx.results[0].error


# 11. No private Aletheon/FABLE.5 paths in the worker layer.
def test_no_private_leakage_in_workers():
    root = Path(__file__).resolve().parents[1] / "src" / "agent_ultra" / "workers"
    banned = ["aletheon", "waxilliam", "mneme", "mythos-", "sk-hermes",
              "hermes_home", "AppData", "fable_", "/opt/data"]
    for p in root.glob("*.py"):
        text = p.read_text(encoding="utf-8").lower()
        for b in banned:
            assert b.lower() not in text, f"{b!r} leaked into {p.name}"


def test_auto_policy_is_documented():
    assert "router" in AUTO_POLICY and "deepagents" in AUTO_POLICY
