"""bob surgical lane: entry criteria, C1 mode-downgrade defense, gate.

The lane's contract: INERT edits only, narrower than full mode on every
axis, and anything code-shaped routes to the FULL gate — surgical can never
launder a code change past Bob.
"""

import io
import json

import pytest

from agent_ultra.bob import (
    BobRun, gate_check, run_surgical, surgical_gate_check,
)
from agent_ultra.bob.surgical import (
    surgical_ext_allowed, surgical_file_denied, staged_set_qualifies,
    SURGICAL_MAX_DIFF_LINES,
)


# --------------------------------------------------------------------------
# entry criteria (pure)
# --------------------------------------------------------------------------

def test_inert_prose_and_dotfiles_qualify():
    for p in ("README.md", "docs/guide.rst", "notes.txt", ".gitignore",
              ".editorconfig"):
        assert surgical_ext_allowed(p), p
        assert not surgical_file_denied(p), p


def test_code_never_qualifies():
    for p in ("app.py", "src/x.js", "lib.rs", "run.sh", "mod.go"):
        assert not surgical_ext_allowed(p), p


def test_structured_config_needs_operator_opt_in(monkeypatch):
    monkeypatch.delenv("AGENT_ULTRA_SURGICAL_ALLOW_CONFIG", raising=False)
    assert not surgical_ext_allowed("settings.yaml")
    monkeypatch.setenv("AGENT_ULTRA_SURGICAL_ALLOW_CONFIG", "1")
    assert surgical_ext_allowed("settings.yaml")
    # even opted in, denied names stay denied
    assert not surgical_ext_allowed("package.json")
    assert not surgical_ext_allowed(".github/workflows/ci.yml")


def test_executing_configs_are_denied():
    for p in ("package.json", "pyproject.toml", "Makefile", "Dockerfile",
              ".github/workflows/ci.yml", ".pre-commit-config.yaml",
              "docker-compose.yml", "jest.config.json", ".mocharc.json",
              "conftest.py", ".env"):
        assert surgical_file_denied(p), p


def test_docs_about_tools_are_not_denied():
    # path-aware matching, not substring: a doc ABOUT Dockerfiles is a doc
    for p in ("docs/Dockerfile-explained.md", "docs/makefile-tips.md"):
        assert not surgical_file_denied(p), p
        assert surgical_ext_allowed(p), p


def test_enforcement_infra_is_never_surgical():
    assert surgical_file_denied(".git/hooks/pre-commit")
    assert surgical_file_denied(".agent-ultra/bob/ACTIVE")


def test_staged_set_qualification_is_strict():
    declared = ["README.md"]
    assert staged_set_qualifies(["README.md"], declared)
    assert staged_set_qualifies(["README.md", ".agent-ultra/bob/x.json"],
                                declared)          # machinery rides along
    assert not staged_set_qualifies(["app.py"], declared)      # code
    assert not staged_set_qualifies(["docs/x.md"], declared)   # undeclared


# --------------------------------------------------------------------------
# start: objective entry criteria, validated up front
# --------------------------------------------------------------------------

def test_surgical_start_rejects_code_and_denied(tmp_path):
    with pytest.raises(ValueError, match="rejects code file"):
        BobRun.start(tmp_path / "ws", goal="g", key_path=tmp_path / "k.key",
                     mode="surgical", surgical_files=["app.py"])
    with pytest.raises(ValueError, match="denied_path"):
        BobRun.start(tmp_path / "ws", goal="g", key_path=tmp_path / "k.key",
                     mode="surgical", surgical_files=["package.json"])
    with pytest.raises(ValueError, match="surgical_files is required"):
        BobRun.start(tmp_path / "ws", goal="g", key_path=tmp_path / "k.key",
                     mode="surgical")
    with pytest.raises(ValueError, match="NEVER valid for surgical"):
        BobRun.start(tmp_path / "ws", goal="g", key_path=tmp_path / "k.key",
                     mode="surgical", surgical_files=["a.md"],
                     operator_unbounded=True)


def test_surgical_scope_is_exactly_its_files(tmp_path):
    run = BobRun.start(tmp_path / "ws", goal="g",
                       key_path=tmp_path / "k.key",
                       mode="surgical", surgical_files=["docs/a.md"])
    assert run.mode() == "surgical"
    assert run.scope() == ["docs/a.md"]
    # scope-add on a surgical run rejects risky paths, accepts inert ones
    added, errors = run.add_scope(["evil.py"])
    assert not added and any("inert" in e for e in errors)
    added, errors = run.add_scope(["docs/b.md"])
    assert added == ["docs/b.md"] and not errors


# --------------------------------------------------------------------------
# the full lane end to end (mock, offline)
# --------------------------------------------------------------------------

@pytest.fixture()
def surgical_world(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ULTRA_HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# hello\nfixed wording\n",
                                  encoding="utf-8")
    outcome = run_surgical("fix readme wording", ws, ["README.md"],
                           mock=True, key_path=tmp_path / "k.key",
                           ultracode_home=tmp_path / "uc",
                           interactive=False, seal_on_pass=False,
                           out=io.StringIO())
    assert outcome.passed, f"surgical mock must pass: {outcome.blocked_at}"
    run = BobRun.active(ws, key_path=tmp_path / "k.key")
    assert run is not None
    return ws, run


def test_surgical_mock_lane_passes(surgical_world):
    ws, run = surgical_world
    res = surgical_gate_check(run, allow_mock=True, staged_files=[])
    assert res.passed, res.errors


def test_surgical_gate_blocks_without_review(surgical_world):
    ws, run = surgical_world
    run.receipt_path("step_surgical_review").unlink()
    res = surgical_gate_check(run, allow_mock=True, staged_files=[])
    assert not res.passed
    assert any("step_surgical_review" in e and "missing" in e
               for e in res.errors)


def test_surgical_quiz_passed_without_response_blocks(surgical_world):
    ws, run = surgical_world
    run.receipt_path("step_surgical_quiz").unlink()
    run.write("step_surgical_quiz",
              {"questions": ["q"], "outcome": "passed",
               "operator_response": ""}, writer="agent", mock=True)
    res = surgical_gate_check(run, allow_mock=True, staged_files=[])
    assert not res.passed
    assert any("self-graded" in e or "operator response" in e
               for e in res.errors)


def test_surgical_staged_undeclared_or_risky_blocks(surgical_world):
    ws, run = surgical_world
    res = surgical_gate_check(run, allow_mock=True,
                              staged_files=["other.md"])
    assert not res.passed
    assert any("not declared" in e for e in res.errors)
    res = surgical_gate_check(run, allow_mock=True,
                              staged_files=["app.py"])
    assert not res.passed
    assert any("not an inert type" in e for e in res.errors)
    res = surgical_gate_check(run, allow_mock=True,
                              staged_files=["package.json"])
    assert not res.passed
    assert any("denied_path" in e for e in res.errors)


def test_surgical_staleness_blocks_post_review_edit(surgical_world):
    ws, run = surgical_world
    (ws / "README.md").write_text("# edited AFTER the review\n",
                                  encoding="utf-8")
    res = surgical_gate_check(run, allow_mock=True, staged_files=[])
    assert not res.passed
    assert any("stale" in e for e in res.errors)


# --------------------------------------------------------------------------
# C1: mode downgrade cannot launder code past the full gate
# --------------------------------------------------------------------------

def test_forged_surgical_mode_with_code_routes_to_full_gate(surgical_world):
    """Flipping run.json's mode is a REQUEST, not a trusted assertion: with
    code staged, gate_check routes to the FULL gate, which demands the full
    receipt chain this run never produced."""
    ws, run = surgical_world
    res = gate_check(run, rerun_tests=False, allow_mock=True,
                     staged_files=["app.py"])
    assert not res.passed
    # blocked by FULL-pipeline demands, not by a surgical pass
    assert any("step02_red" in e for e in res.errors)
    assert any("step08_panel" in e for e in res.errors)


def test_qualifying_surgical_run_routes_to_surgical_gate(surgical_world):
    ws, run = surgical_world
    res = gate_check(run, rerun_tests=False, allow_mock=True,
                     staged_files=["README.md"])
    assert res.passed, res.errors
    assert "step_surgical_review" in res.checked


def test_full_run_never_routes_to_surgical(tmp_path):
    """A FULL-mode run with only inert files staged still gets the full
    gate — surgical is opt-in at start, never an automatic downgrade."""
    run = BobRun.start(tmp_path / "ws", goal="g",
                       key_path=tmp_path / "k.key", scope=["README.md"])
    res = gate_check(run, rerun_tests=False, staged_files=["README.md"])
    assert not res.passed
    assert any("step02_red" in e for e in res.errors)


def test_diff_budget_blocks_oversized_edit(tmp_path, monkeypatch):
    """An in-repo surgical run with a staged diff over the line budget
    blocks at the gate (fail-closed budget check)."""
    import subprocess
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    big = "\n".join(f"line {i}" for i in range(SURGICAL_MAX_DIFF_LINES + 20))
    (ws / "README.md").write_text(big + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=ws, check=True)
    run = BobRun.start(ws, goal="g", key_path=tmp_path / "k.key",
                       mode="surgical", surgical_files=["README.md"])
    res = surgical_gate_check(run, allow_mock=True,
                              staged_files=["README.md"])
    assert not res.passed
    assert any("budget" in e for e in res.errors)
