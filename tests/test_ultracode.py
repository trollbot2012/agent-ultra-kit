"""Offline tests for the ultracode engine — stdlib + pytest, no network.

Everything runs against the deterministic mock route; the journal and receipt
are asserted as the source of truth, and the terminal card is asserted to be
ASCII-safe and crash-proof.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_ultra.ultracode import (
    UltracodeEngine, demo_pool, MockUltracodeClient,
    summarize_events, render_card, print_card, supports_rich, verify_receipt,
    DEFAULTS,
)
from agent_ultra.ultracode.engine import _sha  # noqa: for parity checks
from agent_ultra.routes.pool import RoutePool


SMOKE = '''
META = {"name": "t-smoke", "description": "test", "phases": ["P"]}
async def run(wf):
    wf.phase("P")
    a, b = await wf.parallel([
        lambda: wf.agent("Reply with exactly: one", label="one"),
        lambda: wf.agent("Reply with exactly: two", label="two"),
    ])
    return {"a": a, "b": b}
'''


def write(tmp_path, body, name="script.py"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def make_engine(tmp_path, pool=None, **cfg):
    return UltracodeEngine(pool or demo_pool(), config=cfg,
                           home=tmp_path / "home",
                           usage_source=(pool or demo_pool()).client_for("mock-a")
                           if pool is None else None)


def read_events(rep):
    return [json.loads(l) for l in
            Path(rep.events_path).read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------

def test_smoke_completes_with_receipt_and_journal(tmp_path):
    eng = UltracodeEngine(demo_pool(), home=tmp_path / "h")
    rep = eng.run_script(write(tmp_path, SMOKE))
    assert rep.status == "completed" and rep.final_state == "COMPLETE"
    assert rep.result == {"a": "one", "b": "two"}
    assert rep.model_calls == 2
    journal = Path(rep.journal_path).read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(l)["type"] for l in journal]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert kinds.count("agent") == 2
    receipt = json.loads(Path(rep.receipt_path).read_text(encoding="utf-8"))
    assert verify_receipt(receipt) and len(receipt["agents"]) == 2
    receipt["model_calls"] = 999
    assert not verify_receipt(receipt)


def test_events_match_reality_not_prose(tmp_path):
    # a mock that CLAIMS success for many agents; the stream must show 2
    class Liar(MockUltracodeClient):
        def complete(self, model, prompt, max_tokens):
            super().complete(model, prompt, max_tokens)
            return "All done! 8 agents completed successfully."
    pool = RoutePool(["mock-a"], client=Liar())
    eng = UltracodeEngine(pool, home=tmp_path / "h")
    rep = eng.run_script(write(tmp_path, SMOKE))
    names = [e["event"] for e in read_events(rep)]
    assert names[0] == "ultracode_started" and names[-1] == "ultracode_completed"
    assert names.count("agent_completed") == 2   # NOT 8
    card = summarize_events(rep.events_path)
    assert card["counts"] == {"planned": 2, "deployed": 2, "completed": 2,
                              "failed": 0, "blocked": 0}
    assert [d["state"] for d in card["dots"]] == ["completed", "completed"]


def test_schema_retry_then_valid(tmp_path):
    class Flaky(MockUltracodeClient):
        def __init__(self):
            super().__init__()
            self.n = 0
        def complete(self, model, prompt, max_tokens):
            self.n += 1
            return "not json" if self.n == 1 else '{"k": "v"}'
    pool = RoutePool(["mock-a"], client=Flaky())
    eng = UltracodeEngine(pool, home=tmp_path / "h")
    script = '''
META = {"name": "t-schema", "description": "t"}
async def run(wf):
    return await wf.agent("x", schema={"type": "object",
        "properties": {"k": {"type": "string"}}, "required": ["k"]})
'''
    rep = eng.run_script(write(tmp_path, script))
    assert rep.result == {"k": "v"} and rep.model_calls == 2


def test_budget_hold_blocks_and_holds(tmp_path):
    eng = UltracodeEngine(demo_pool(), home=tmp_path / "h", config={"max_calls": 1})
    script = '''
META = {"name": "t-hold", "description": "t"}
async def run(wf):
    await wf.agent("one")
    await wf.agent("two")
    return "unreachable"
'''
    rep = eng.run_script(write(tmp_path, script))
    assert rep.status == "aborted_budget" and rep.final_state == "HOLD"
    card = summarize_events(rep.events_path)
    assert card["counts"]["completed"] == 1 and card["counts"]["blocked"] == 1


def test_failed_agent_is_failed_dot(tmp_path):
    pool = demo_pool(routes=["good", "bad"], fail_routes={"bad"})
    eng = UltracodeEngine(pool, home=tmp_path / "h")
    script = '''
META = {"name": "t-fail", "description": "t"}
async def run(wf):
    ok = await wf.agent("hi", route="good", label="ok")
    bad = await wf.agent("no", route="bad", label="bad")
    return {"ok": ok, "bad": bad}
'''
    rep = eng.run_script(write(tmp_path, script))
    card = summarize_events(rep.events_path)
    assert card["counts"]["completed"] == 1 and card["counts"]["failed"] == 1
    assert sorted(d["state"] for d in card["dots"]) == ["completed", "failed"]


def test_pipeline_drops_failed_item(tmp_path):
    eng = UltracodeEngine(demo_pool(), home=tmp_path / "h")
    script = '''
META = {"name": "t-pipe", "description": "t"}
async def run(wf):
    def boom(prev, item):
        if item == "b":
            raise ValueError("nope")
        return prev
    return await wf.pipeline(["a", "b"],
                             lambda item: wf.agent("x " + item), boom)
'''
    rep = eng.run_script(write(tmp_path, script))
    assert rep.status == "completed"
    assert rep.result[1] is None and rep.result[0] is not None


def test_resume_replays_without_calls(tmp_path):
    eng = UltracodeEngine(demo_pool(), home=tmp_path / "h")
    path = write(tmp_path, SMOKE)
    first = eng.run_script(path)
    assert first.model_calls == 2

    class Explode(MockUltracodeClient):
        def complete(self, *a):
            raise AssertionError("resume must not call the model")
    eng2 = UltracodeEngine(RoutePool(["mock-a"], client=Explode()),
                           home=tmp_path / "h")
    second = eng2.run_script(path, resume_run_id=first.run_id)
    assert second.status == "completed"
    assert second.model_calls == 0 and second.cached_hits == 2
    assert second.result == first.result


def test_resume_runs_only_new_calls(tmp_path):
    a = '''
META = {"name": "t-grow", "description": "t"}
async def run(wf):
    return {"x": await wf.agent("stage one")}
'''
    b = '''
META = {"name": "t-grow", "description": "t"}
async def run(wf):
    x = await wf.agent("stage one")
    y = await wf.agent("stage two: " + (x or ""))
    return {"x": x, "y": y}
'''
    eng = UltracodeEngine(demo_pool(), home=tmp_path / "h")
    first = eng.run_script(write(tmp_path, a, "grow.py"))
    assert first.model_calls == 1
    eng2 = UltracodeEngine(demo_pool(), home=tmp_path / "h")
    second = eng2.run_script(write(tmp_path, b, "grow.py"),
                             resume_run_id=first.run_id)
    assert second.cached_hits == 1 and second.model_calls == 1


def test_malformed_scripts_fail_loudly(tmp_path):
    from agent_ultra.ultracode import WorkflowError
    eng = UltracodeEngine(demo_pool(), home=tmp_path / "h")
    with pytest.raises(WorkflowError, match="META"):
        eng.run_script(write(tmp_path, "async def run(wf):\n return 1\n", "a.py"))
    with pytest.raises(WorkflowError, match="async def run"):
        eng.run_script(write(tmp_path, 'META={"name":"x"}\n', "b.py"))
    with pytest.raises(WorkflowError, match="failed to import"):
        eng.run_script(write(tmp_path, "def def def\n", "c.py"))


# -- terminal safety --------------------------------------------------------

class _TTY:
    def __init__(self, tty=True, encoding="utf-8"):
        self._tty, self.encoding = tty, encoding
    def isatty(self):
        return self._tty


def test_supports_rich_detection():
    good = {"TERM": "xterm-256color"}
    assert supports_rich(_TTY(), good) is True
    assert supports_rich(_TTY(tty=False), good) is False
    assert supports_rich(_TTY(), {"TERM": "dumb"}) is False
    assert supports_rich(_TTY(), {"TERM": "xterm", "CI": "1"}) is False
    assert supports_rich(_TTY(), {"TERM": "xterm", "NO_COLOR": "1"}) is False
    assert supports_rich(_TTY(), {"TERM": "xterm", "AGENT_ULTRA_PLAIN": "1"}) is False
    assert supports_rich(_TTY(encoding="cp1252"), good) is False

    class Evil:
        def isatty(self):
            raise RuntimeError("boom")
    assert supports_rich(Evil(), good) is False


def test_render_card_plain_is_ascii(tmp_path):
    pool = demo_pool(routes=["good", "bad"], fail_routes={"bad"})
    eng = UltracodeEngine(pool, home=tmp_path / "h")
    script = '''
META = {"name": "t-card", "description": "t"}
async def run(wf):
    await wf.agent("a", route="good", label="ok")
    await wf.agent("b", route="bad", label="bad")
'''
    rep = eng.run_script(write(tmp_path, script))
    card = render_card(summarize_events(rep.events_path), rich=False)
    card.encode("ascii")  # pure ASCII
    lines = card.splitlines()
    assert lines[0] == "ULTRACODE"
    assert "Agents: 1/2 complete" in lines and "Failed: 1" in lines
    bar = [l for l in lines if l.startswith("Progress: ")][0]
    assert bar.startswith("Progress: [") and sorted(bar[11:-1]) == ["!", "#"]


def test_render_card_never_raises_on_garbage():
    for g in (None, 42, "text", [], {"counts": "x", "dots": 7},
              {"dots": [None, 3, {"state": object()}]}):
        assert render_card(g, rich=False).startswith("ULTRACODE")
        assert render_card(g, rich=True).startswith("ULTRACODE")


def test_print_card_survives_broken_stream(tmp_path):
    eng = UltracodeEngine(demo_pool(), home=tmp_path / "h")
    rep = eng.run_script(write(tmp_path, SMOKE))

    class Broken:
        encoding = "utf-8"
        def isatty(self):
            return True
        def write(self, *a):
            raise OSError("pipe closed")
        def flush(self):
            raise OSError("pipe closed")
    print_card(summarize_events(rep.events_path), stream=Broken())  # no raise


# -- CLI end to end (subprocess, redirected -> plain ASCII) -----------------

def test_cli_run_and_resume(tmp_path):
    import os
    env = dict(os.environ)
    env["AGENT_ULTRA_HOME"] = str(tmp_path / "clihome")
    env["AGENT_ULTRA_PLAIN"] = "1"
    run = subprocess.run(
        [sys.executable, "-m", "agent_ultra.adapters.cli",
         "ultracode", "run", "smoke", "--mock"],
        capture_output=True, text=True, env=env, timeout=120)
    assert run.returncode == 0, run.stderr
    run.stdout.encode("ascii")  # plain fallback engaged
    assert "ULTRACODE" in run.stdout and "Status: COMPLETE" in run.stdout
    run_id = [l.split()[-1] for l in run.stdout.splitlines()
              if l.startswith("Resume:")][0]

    res = subprocess.run(
        [sys.executable, "-m", "agent_ultra.adapters.cli",
         "ultracode", "resume", run_id, "--mock"],
        capture_output=True, text=True, env=env, timeout=120)
    assert res.returncode == 0, res.stderr
    assert "Status: COMPLETE" in res.stdout
    assert '"calls_spent": 0' in res.stdout  # resume proved cached calls
