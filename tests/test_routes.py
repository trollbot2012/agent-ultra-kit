"""Route pool — health probing, degraded mode, assignment."""

from agent_ultra.routes.pool import RoutePool
from agent_ultra.routes.mock import MockChatClient


def test_probe_marks_healthy_and_dead():
    client = MockChatClient(fail_routes={"bad"})
    pool = RoutePool(["good", "bad"], client=client)
    assert pool.probe("good") is True
    assert pool.probe("bad") is False
    assert pool.alive() == ["good"]
    assert pool.dead() == ["bad"]


def test_probe_all_returns_healthy_in_order():
    client = MockChatClient(fail_routes={"b"})
    pool = RoutePool(["a", "b", "c"], client=client)
    assert pool.probe_all() == ["a", "c"]


def test_revive_dead_route():
    client = MockChatClient()  # nothing fails
    pool = RoutePool(["a"], client=client)
    pool.mark_dead("a")
    assert pool.revive("a") is True
    assert "a" in pool.alive()


def test_assign_round_robin_and_avoid():
    pool = RoutePool(["a", "b", "c"], client=MockChatClient())
    assert pool.assign(0)[0] == "a"
    assert pool.assign(1)[0] == "b"
    ordered = pool.assign(0, avoid="a")
    assert ordered[-1] == "a"  # avoid sorts last


def test_client_map_per_route():
    ca = MockChatClient(default="A")
    cb = MockChatClient(default="B")
    pool = RoutePool(["a", "b"], client_map={"a": ca, "b": cb})
    assert pool.chat("a", "PROBE: x", 10) == "A"
    assert pool.chat("b", "PROBE: x", 10) == "B"


def test_requires_a_client():
    import pytest
    with pytest.raises(ValueError):
        RoutePool(["a"])
