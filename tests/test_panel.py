"""Panel engine — fully offline via the mock route."""

import pytest

from agent_ultra.routes.pool import RoutePool
from agent_ultra.routes.mock import demo_panel_client, MockChatClient
from agent_ultra.panel.engine import PanelEngine, PanelError


def _engine(mode="single"):
    client = demo_panel_client()
    routes = ["mock-a", "mock-b"] if mode == "mixed" else ["mock-a"]
    return PanelEngine(RoutePool(routes, client=client),
                       config={"routing_mode": mode})


def test_single_route_panel_runs():
    eng = _engine("single")
    report = eng.run("Is this authentication service safe?", size="small",
                     context="def auth(req): return 'token' in req.headers")
    assert report.findings
    assert report.decision
    counts = report.verdict_counts()
    assert sum(counts.values()) >= len(report.findings)


def test_accepted_findings_and_proof_gates():
    eng = _engine("single")
    report = eng.run("Is this auth code safe?", size="small",
                     lenses=["security", "correctness"],
                     context="def auth(req): return 'token' in req.headers")
    # the mock judge accepts the empty-token bypass as real_now critical
    assert any(f.verdict == "real_now" for f in report.accepted)
    # accepted findings with a read-only check become proof gates
    assert all(isinstance(g, str) for g in report.proof_gates)


def test_mixed_mode_collapses_to_single_when_one_route_dead():
    client = demo_panel_client()
    client.fail_routes = {"mock-b"}
    eng = PanelEngine(RoutePool(["mock-a", "mock-b"], client=client),
                      config={"routing_mode": "mixed"})
    report = eng.run("review this", size="small", context="x" * 600)
    assert report.routes["mode_effective"] == "single"
    assert report.findings


def test_all_routes_dead_raises():
    client = MockChatClient(fail_routes={"dead-1", "dead-2"})
    eng = PanelEngine(RoutePool(["dead-1", "dead-2"], client=client))
    with pytest.raises(PanelError):
        eng.run("anything", size="small", context="x" * 600)


def test_low_context_flag():
    eng = _engine("single")
    report = eng.run("tiny", size="small", context="short")
    assert report.low_context is True


def test_large_needs_allow_large():
    eng = _engine("single")
    with pytest.raises(PanelError):
        eng.run("q", size="large", context="x" * 600)


def test_unknown_size_raises():
    eng = _engine("single")
    with pytest.raises(PanelError):
        eng.run("q", size="enormous")


def test_empty_question_raises():
    eng = _engine("single")
    with pytest.raises(PanelError):
        eng.run("   ", size="small")
