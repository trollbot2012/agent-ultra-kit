"""Artifact/ledger schema + generic memory hooks."""

import json
from pathlib import Path

from agent_ultra.artifacts.records import RunRecord, write_run, append_jsonl
from agent_ultra.memory.hooks import (
    MemoryHooks, CompositeHooks, JsonlHooks, safe_call,
)


def test_write_run_produces_json_and_md(tmp_path):
    rec = RunRecord(kind="panel", question="Is X safe?",
                    decision="Fix the thing.", lenses=["security"],
                    accepted=[{"lens": "security", "claim": "bypass",
                               "verdict": "real_now", "severity": "critical"}],
                    proof_gates=["grep -n token app.py"])
    jp, mp = write_run(rec, tmp_path)
    assert jp.exists() and mp.exists()
    data = json.loads(jp.read_text(encoding="utf-8"))
    assert data["kind"] == "panel"
    assert "Fix the thing." in mp.read_text(encoding="utf-8")


def test_write_run_redacts_secrets(tmp_path):
    rec = RunRecord(kind="panel", question="q",
                    outputs={"synthesis": "key sk-supersecret0123456789abcd here"})
    jp, mp = write_run(rec, tmp_path)
    assert "sk-supersecret" not in jp.read_text(encoding="utf-8")
    assert "sk-supersecret" not in mp.read_text(encoding="utf-8")


def test_ledger_append(tmp_path):
    led = tmp_path / "ledger.jsonl"
    append_jsonl(led, {"a": 1})
    append_jsonl(led, {"b": 2})
    lines = led.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_base_hooks_are_noops():
    h = MemoryHooks()
    h.on_panel_decision({})
    h.on_finding_accepted({})
    h.on_lesson_learned("x")  # no raise


def test_jsonl_hooks_write_events(tmp_path):
    h = JsonlHooks(tmp_path / "mem.jsonl")
    h.on_panel_decision({"q": "x"})
    h.on_finding_accepted({"lens": "sec"})
    lines = (tmp_path / "mem.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "panel_decision"


def test_composite_fans_out(tmp_path):
    a = JsonlHooks(tmp_path / "a.jsonl")
    b = JsonlHooks(tmp_path / "b.jsonl")
    comp = CompositeHooks(a, b)
    comp.on_lesson_learned("learned")
    assert (tmp_path / "a.jsonl").exists()
    assert (tmp_path / "b.jsonl").exists()


def test_safe_call_swallows_exceptions():
    class Boom(MemoryHooks):
        def on_panel_decision(self, record):
            raise RuntimeError("nope")
    safe_call(Boom(), "on_panel_decision", {})  # must not raise
    safe_call(None, "on_panel_decision", {})    # None is fine
