"""ULTRA loop demo — build -> test -> panel -> finding -> fix -> re-panel -> ship.

    python examples/ultra_loop_demo/run.py

Fully offline: a fake test runner + fake fixer stand in for a real coding
agent so the whole loop runs with no network. In real use you pass a builder
and fixer that drive your agent, and the default broker-backed test runner.
"""

import tempfile
from pathlib import Path

from agent_ultra.routes.pool import RoutePool
from agent_ultra.routes.mock import demo_panel_client
from agent_ultra.panel.engine import PanelEngine
from agent_ultra.broker.broker import CommandBroker, TRUSTED_OWNER_TIERS
from agent_ultra.ultra_loop.loop import UltraLoop, TestResult

SOURCE = '''"""Toy auth service."""
SESSIONS = {}

def authenticate(request):
    # BUG: only checks the header KEY exists, not that the token is non-empty
    return "token" in request.headers

def login(user, token):
    SESSIONS[user] = token
    return SESSIONS[user]
'''


def main():
    ws = Path(tempfile.mkdtemp())
    (ws / "service.py").write_text(SOURCE, encoding="utf-8")

    pool = RoutePool(["mock-a"], client=demo_panel_client())
    engine = PanelEngine(pool)
    broker = CommandBroker(ledger_path=ws / ".ultra" / "broker.jsonl",
                           auto_run_tiers=TRUSTED_OWNER_TIERS)

    # Fakes standing in for a real coding agent (offline demo):
    def fake_tests(workspace, cmd, timeout):
        return TestResult(cmd or "pytest", 0, True, "16 passed", 0.1)

    fixed = []

    def fake_fixer(fix_task, workspace):
        fixed.append(fix_task.claim)
        return True   # pretend the agent patched the finding

    loop = UltraLoop(ws, panel=engine, broker=broker,
                     test_runner=fake_tests, fixer=fake_fixer)

    report = loop.run("implement token authentication", risk="high",
                      test_cmd="pytest -q")

    print("BUILD  -> (agent would implement here)")
    print(f"TEST   -> tests_before: passed={report.tests_before.get('passed')}")
    print(f"PANEL  -> ran={report.panel_ran}, "
          f"counts={report.panel1.get('counts')}")
    print(f"FINDING-> {len(fixed)} blocking finding(s) became fix task(s):")
    for c in fixed:
        print(f"           - {c[:90]}")
    print(f"FIX    -> fix_tasks: "
          f"{[t['status'] for t in report.fix_tasks]}")
    print(f"SHIP   -> shipped={report.shipped}: {report.ship_reason}")
    print(f"\nArtifacts: {report.artifact_dir}")
    if not report.shipped:
        print("\nNote: the deterministic mock re-panel keeps returning the same "
              "findings,\nso the ship gate correctly HOLDS — that is the gate "
              "doing its job, not\na failure. Against a real model, an applied "
              "fix clears the finding and ships.")


if __name__ == "__main__":
    main()
