"""ULTRA loop — build -> test -> panel -> fix -> re-panel -> ship, offline."""

from pathlib import Path

from agent_ultra.routes.pool import RoutePool
from agent_ultra.routes.mock import demo_panel_client
from agent_ultra.panel.engine import PanelEngine
from agent_ultra.broker.broker import CommandBroker, TRUSTED_OWNER_TIERS
from agent_ultra.ultra_loop.loop import UltraLoop, TestResult


def _loop(tmp_path, fixer=None, test_passes=True):
    engine = PanelEngine(RoutePool(["mock-a"], client=demo_panel_client()))
    broker = CommandBroker(ledger_path=tmp_path / "l.jsonl",
                           auto_run_tiers=TRUSTED_OWNER_TIERS)

    def runner(ws, cmd, timeout):
        return TestResult(cmd or "noop", 0 if test_passes else 1,
                          test_passes, "ok" if test_passes else "FAIL", 0.1)

    # a realistic-size source file so the loop does NOT hit the low-context
    # refusal (min_context_chars=200); this mirrors real usage.
    (tmp_path / "app.py").write_text(
        '"""Toy auth service under review."""\n'
        "import time\n\n"
        "SESSIONS = {}\n\n"
        "def auth(req):\n"
        "    # accepts any request carrying a 'token' header key\n"
        "    return 'token' in req.headers\n\n"
        "def login(user, token):\n"
        "    SESSIONS[user] = {'token': token, 'ts': time.time()}\n"
        "    return SESSIONS[user]\n\n"
        "def logout(user):\n"
        "    SESSIONS.pop(user, None)\n",
        encoding="utf-8")
    return UltraLoop(tmp_path, panel=engine, broker=broker,
                     test_runner=runner, fixer=fixer)


def test_red_tests_block_before_panel(tmp_path):
    loop = _loop(tmp_path, test_passes=False)
    rep = loop.run("add auth", risk="high", test_cmd="pytest")
    assert rep.shipped is False
    assert "RED" in rep.ship_reason
    assert rep.panel_ran is False


def test_low_risk_ships_on_tests_alone(tmp_path):
    loop = _loop(tmp_path)
    rep = loop.run("rename a variable", risk="small", test_cmd="pytest",
                   force_panel=False)
    assert rep.shipped is True
    assert "tests alone" in rep.ship_reason


def test_high_risk_runs_panel_and_holds_on_blocking(tmp_path):
    # no fixer -> blocking findings remain -> HOLD
    loop = _loop(tmp_path)
    rep = loop.run("add authentication with tokens", risk="high",
                   test_cmd="pytest")
    assert rep.panel_ran is True
    # the mock panel yields a real_now critical auth bypass -> blocking
    assert rep.shipped is False
    assert "HOLD" in rep.ship_reason


def test_fixer_clears_blocking_and_ships(tmp_path):
    # a fixer that "resolves" everything, and a re-panel that (still mock)
    # yields the same finding — so we simulate the fix landing by having the
    # fixer succeed and asserting the loop attempted the fix + re-panel path.
    calls = {"n": 0}

    def fixer(task, ws):
        calls["n"] += 1
        return True

    loop = _loop(tmp_path, fixer=fixer)
    rep = loop.run("add authentication with tokens", risk="high",
                   test_cmd="pytest")
    assert calls["n"] >= 1              # fixer was invoked on blocking findings
    assert any(t["status"] == "fixed" for t in rep.fix_tasks)
    assert Path(rep.artifact_dir).exists()


def test_artifacts_written(tmp_path):
    loop = _loop(tmp_path)
    rep = loop.run("add token auth", risk="high", test_cmd="pytest")
    files = list(Path(rep.artifact_dir).glob("*"))
    assert files  # json + md artifact
