"""bob pipeline: receipt chain, gate enforcement, and anti-fake proofs.

The expensive full mock pipeline runs ONCE (module fixture); every tampering
scenario works on a disposable copy of that run so the proofs stay independent.
Fully offline: mock routes, real pytest subprocesses, real ultracode runs.
"""

import io
import json
import shutil

import pytest

from agent_ultra.bob import (
    BobRun, BobProofError, GATED_STEPS,
    build_step_receipt, validate_chain, gate_check, run_pytest_step,
    run_bob, assert_bob_done,
)
from agent_ultra.bob.pipeline import (
    check_ultracode_evidence, safe_rel, parse_pytest_nodes,
)

KEY = b"bob-test-key-0123456789abcdef"


# --------------------------------------------------------------------------
# receipt chain (pure, no subprocess)
# --------------------------------------------------------------------------

def _chain(n=3, key=KEY):
    steps = ["step01_spec", "step02_red", "step03_green"][:n]
    out, prev = [], ""
    for i, step in enumerate(steps):
        rec = build_step_receipt(step=step, run_id="r1", seq=i,
                                 writer="agent", evidence={"i": i},
                                 prev_sha256=prev, key=key)
        prev = rec["receipt_sha256"]
        out.append(rec)
    return out


def test_valid_chain_has_no_errors():
    assert validate_chain(_chain(), KEY) == []


def test_edited_receipt_breaks_integrity_and_chain():
    chain = _chain()
    chain[1]["evidence"]["i"] = 99          # tamper after signing
    errs = "\n".join(validate_chain(chain, KEY))
    assert "integrity FAIL" in errs


def test_unsigned_receipt_is_a_forgery():
    chain = _chain(key=None)                # correct sha256, no HMAC
    errs = "\n".join(validate_chain(chain, KEY))
    assert "authenticity FAIL" in errs


def test_wrong_key_fails_closed():
    errs = "\n".join(validate_chain(_chain(), b"some-other-install-key"))
    assert "authenticity FAIL" in errs


def test_reordered_chain_breaks_linkage():
    chain = _chain()
    chain[1], chain[2] = chain[2], chain[1]
    errs = "\n".join(validate_chain(chain, KEY))
    assert "seq" in errs or "prev_sha256" in errs


def test_removed_middle_receipt_breaks_linkage():
    chain = _chain()
    del chain[1]
    errs = "\n".join(validate_chain(chain, KEY))
    assert "prev_sha256" in errs


# --------------------------------------------------------------------------
# RED/GREEN system runner
# --------------------------------------------------------------------------

def test_red_rejects_passing_tests(tmp_path):
    (tmp_path / "test_t.py").write_text("def test_a():\n    assert True\n",
                                        encoding="utf-8")
    run = BobRun.start(tmp_path, goal="g", key_path=tmp_path / "k.key")
    ok, rec, msg = run_pytest_step(run, "red", "test_t.py")
    assert not ok and rec["writer"] == "system"
    assert rec["evidence"]["requirement_met"] is False


def test_red_rejects_collection_errors(tmp_path):
    (tmp_path / "test_t.py").write_text("import missing_module_xyz\n",
                                        encoding="utf-8")
    run = BobRun.start(tmp_path, goal="g", key_path=tmp_path / "k.key")
    ok, _rec, msg = run_pytest_step(run, "red", "test_t.py")
    assert not ok


# --------------------------------------------------------------------------
# the full mock pipeline (one real run, many disposable copies)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def bob_world(tmp_path_factory):
    base = tmp_path_factory.mktemp("bobworld")
    outcome = run_bob("add a slugify helper", base / "ws", mock=True,
                      key_path=base / "bob.key",
                      ultracode_home=base / "uc",
                      interactive=False, out=io.StringIO())
    assert outcome.passed, f"mock pipeline must pass: {outcome.blocked_at}"
    return base, outcome


@pytest.fixture()
def world_copy(bob_world, tmp_path):
    """A disposable copy of the passing run for tampering scenarios."""
    base, outcome = bob_world
    shutil.copytree(base / "ws", tmp_path / "ws")
    shutil.copy(base / "bob.key", tmp_path / "bob.key")
    ws = tmp_path / "ws"
    run = BobRun(workspace=ws,
                 run_dir=ws / ".agent-ultra" / "bob" / "runs" / outcome.run_id,
                 key=(base / "bob.key").read_bytes())
    return run


def test_mock_run_completes_and_gate_passes(bob_world):
    base, outcome = bob_world
    assert outcome.passed and outcome.gate.passed
    assert (base / "ws" / ".agent-ultra" / "bob" / "runs" / outcome.run_id
            / "gate-pass.json").is_file()
    assert (base / "ws" / ".agent-ultra" / "bob" / "runs" / outcome.run_id
            / "report.json").is_file()
    # the run was sealed: ACTIVE released
    assert not (base / "ws" / ".agent-ultra" / "bob" / "ACTIVE").exists()


def test_every_gated_step_left_a_receipt(world_copy):
    have = {r["step"] for r in world_copy.chain()}
    assert set(GATED_STEPS) <= have


def test_mock_receipts_blocked_outside_demo_mode(world_copy):
    result = gate_check(world_copy, allow_mock=False, rerun_tests=False)
    assert not result.passed
    assert any("mock" in e for e in result.errors)


def test_skipped_step_blocks(world_copy):
    world_copy.receipt_path("step06_security").unlink()
    result = gate_check(world_copy, allow_mock=True, rerun_tests=False)
    assert not result.passed
    assert any("step06_security" in e and "missing" in e
               for e in result.errors)


def test_fake_fanout_claim_blocks(bob_world, world_copy):
    """A re-signed receipt naming an invented ultracode run must block."""
    base, _ = bob_world
    world_copy.receipt_path("step07_workflow").unlink()
    world_copy.write("step07_workflow",
                     {"ultracode": {"run_id": "20990101T000000-fake-000000",
                                    "home": str(base / "uc")},
                      "dimensions": ["invented"]},
                     writer="engine", mock=True)
    result = gate_check(world_copy, allow_mock=True, rerun_tests=False)
    assert not result.passed
    assert any("fabricated" in e or "no receipt on disk" in e
               for e in result.errors)


def test_doctored_ultracode_receipt_blocks():
    """check_ultracode_evidence itself rejects a nonexistent receipt.json."""
    errs = check_ultracode_evidence(
        {"ultracode": {"run_id": "nope", "home": "does/not/exist"}},
        allow_mock=True, receipt_mock=True)
    assert errs and ("fabricated" in errs[0] or "no receipt on disk" in errs[0])


def test_fake_panel_claim_blocks(world_copy):
    receipt = (world_copy.run_dir / "panel" / "panel_execution_receipt.json")
    body = json.loads(receipt.read_text(encoding="utf-8"))
    body["lens_count_executed"] = 99        # checksum now stale
    receipt.write_text(json.dumps(body), encoding="utf-8")
    result = gate_check(world_copy, allow_mock=True, rerun_tests=False)
    assert not result.passed
    assert any("step08_panel" in e for e in result.errors)


def test_quiz_passed_without_operator_response_blocks(world_copy):
    world_copy.receipt_path("step09_quiz").unlink()
    world_copy.write("step09_quiz",
                     {"questions": ["q1"], "outcome": "passed",
                      "operator_response": ""},
                     writer="agent", mock=True)
    result = gate_check(world_copy, allow_mock=True, rerun_tests=False)
    assert not result.passed
    assert any("quiz nobody answered" in e for e in result.errors)


def test_stale_file_blocks(world_copy):
    impl = world_copy.workspace / "slug_util.py"
    impl.write_text(impl.read_text(encoding="utf-8") + "\n# edited later\n",
                    encoding="utf-8")
    result = gate_check(world_copy, allow_mock=True, rerun_tests=False)
    assert not result.passed
    assert any("stale" in e for e in result.errors)


def test_live_rerun_catches_broken_tests(world_copy):
    """Even with pristine receipts, the gate re-runs pytest — a state that
    fails its own tests blocks regardless of what the chain says.
    (Here the receipts also go stale; the live re-run must ALSO fire.)"""
    impl = world_copy.workspace / "slug_util.py"
    impl.write_text("def slugify(text):\n    return 'broken'\n",
                    encoding="utf-8")
    result = gate_check(world_copy, allow_mock=True, rerun_tests=True)
    assert not result.passed
    assert any("live re-run" in e for e in result.errors)


def test_assert_bob_done_raises_while_unproven(tmp_path):
    run = BobRun.start(tmp_path / "ws", goal="g",
                       key_path=tmp_path / "k.key")
    with pytest.raises(BobProofError):
        assert_bob_done(tmp_path / "ws", key_path=tmp_path / "k.key")
    assert run.run_id  # started, never proven


def test_assert_bob_done_none_when_no_active_run(tmp_path):
    assert assert_bob_done(tmp_path, key_path=tmp_path / "k.key") is None


# --------------------------------------------------------------------------
# hardening: findings from the adversarial review
# --------------------------------------------------------------------------

def test_safe_rel_rejects_absolute_and_traversal(tmp_path):
    assert safe_rel(tmp_path, "sub/x.py") == "sub/x.py"
    for bad in ("../evil.py", "../../x", "/etc/passwd", "C:/Windows/x"):
        try:
            safe_rel(tmp_path, bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")


def test_pytest_parser_handles_parametrized_space_ids():
    out = ("test_x.py::test_a[hello world] PASSED [ 50%]\n"
           "test_x.py::test_b[a b] FAILED [100%]\n")
    nodes = parse_pytest_nodes(out)
    assert nodes == {"test_x.py::test_a[hello world]": "PASSED",
                     "test_x.py::test_b[a b]": "FAILED"}


def test_borrowed_ultracode_run_blocks(bob_world, world_copy):
    """A valid receipt from the WRONG workflow (e.g. a trivial prior run) must
    not satisfy a gated fan-out step."""
    base, _ = bob_world
    # point step07 (expects bob-review) at the step06 security run instead
    sec = world_copy.load("step06_security")["evidence"]["ultracode"]
    world_copy.receipt_path("step07_workflow").unlink()
    world_copy.write("step07_workflow",
                     {"ultracode": {**sec, "workflow_name": "bob-security"},
                      "excerpt_sha256": "x", "dimensions": ["d"]},
                     writer="engine", mock=True)
    result = gate_check(world_copy, allow_mock=True, rerun_tests=False)
    assert not result.passed
    assert any("bob-review" in e for e in result.errors)


def test_edited_ultracode_receipt_breaks_fingerprint(bob_world, world_copy):
    base, _ = bob_world
    uc = world_copy.load("step06_security")["evidence"]["ultracode"]
    rec_path = (base / "uc" / "runs" / uc["run_id"] / "receipt.json")
    rec_path.write_text(rec_path.read_text(encoding="utf-8") + "\n",
                        encoding="utf-8")   # one byte changes the file sha
    result = gate_check(world_copy, allow_mock=True, rerun_tests=False)
    assert not result.passed
    assert any("fingerprint" in e for e in result.errors)


def test_borrowed_panel_receipt_blocks(bob_world, world_copy):
    """A structurally valid panel receipt for a DIFFERENT task must fail the
    step-8 binding (expect_task_id / expect_artifact_hash)."""
    import json as _json
    receipt = world_copy.run_dir / "panel" / "panel_execution_receipt.json"
    body = _json.loads(receipt.read_text(encoding="utf-8"))
    # forge a self-consistent receipt for a different task/artifact
    from agent_ultra.panel_receipt.receipt import _sha256
    body["task_id"] = "some-other-run"
    b = {k: v for k, v in body.items() if k != "receipt_sha256"}
    body["receipt_sha256"] = _sha256(_json.dumps(b, sort_keys=True,
                                                 ensure_ascii=False))
    receipt.write_text(_json.dumps(body), encoding="utf-8")
    result = gate_check(world_copy, allow_mock=True, rerun_tests=False)
    assert not result.passed
    assert any("step08_panel" in e for e in result.errors)


def test_malformed_receipt_fails_closed_not_crash(world_copy):
    import json as _json
    p = world_copy.receipt_path("step02_red")
    body = _json.loads(p.read_text(encoding="utf-8"))
    body["evidence"]["counts"] = "not-a-dict"
    p.write_text(_json.dumps(body), encoding="utf-8")
    # must return a blocked GateResult, never raise
    result = gate_check(world_copy, allow_mock=True, rerun_tests=False)
    assert not result.passed


def test_deleting_run_dir_blocks_via_orphaned_marker(bob_world, tmp_path):
    """A sealed marker pointing at a missing run dir is tampering, not 'done'."""
    import shutil as _shutil
    base, outcome = bob_world
    _shutil.copytree(base / "ws", tmp_path / "ws")
    _shutil.copy(base / "bob.key", tmp_path / "bob.key")
    root = tmp_path / "ws" / ".agent-ultra" / "bob"
    (root / "ACTIVE").write_text(outcome.run_id, encoding="utf-8")
    _shutil.rmtree(root / "runs" / outcome.run_id)   # delete the receipts
    run = BobRun.active(tmp_path / "ws", key_path=tmp_path / "bob.key")
    assert run is not None and run.marker_orphaned()
    assert not gate_check(run, allow_mock=True, rerun_tests=False).passed


def test_start_refuses_to_orphan_an_active_run(tmp_path):
    BobRun.start(tmp_path / "ws", goal="first", key_path=tmp_path / "k.key")
    try:
        BobRun.start(tmp_path / "ws", goal="second", key_path=tmp_path / "k.key")
    except ValueError:
        pass
    else:
        raise AssertionError("second start should refuse while one is active")
    # force= abandons it deliberately
    r2 = BobRun.start(tmp_path / "ws", goal="second",
                      key_path=tmp_path / "k.key", force=True)
    assert r2.run_id


def test_mock_panel_receipt_blocked_without_allow_mock(world_copy):
    """The mock flag must be enforced on step 8 too, not only steps 6/7."""
    result = gate_check(world_copy, allow_mock=False, rerun_tests=False)
    assert not result.passed
    assert any("panel" in e and "mock" in e for e in result.errors)


# --------------------------------------------------------------------------
# commit lifecycle: mark at pre-commit, seal at post-commit (deadlock fix)
# --------------------------------------------------------------------------

import os
import subprocess
import sys


def _git(ws, *args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(["git", "-C", str(ws)] + list(args),
                          capture_output=True, text=True, timeout=120, env=e)


def test_mark_pass_keeps_active_and_seal_releases(tmp_path):
    run = BobRun.start(tmp_path / "ws", goal="g", key_path=tmp_path / "k.key")
    marker = tmp_path / "ws" / ".agent-ultra" / "bob" / "ACTIVE"
    # seal without a recorded pass must refuse
    assert run.seal() is False
    assert marker.is_file()
    run.mark_pass({"passed": True})
    assert marker.is_file(), "mark_pass must NOT release ACTIVE"
    assert (run.run_dir / "gate-pass.json").is_file()
    assert run.seal() is True
    assert not marker.exists(), "seal releases ACTIVE after the commit lands"


def test_commit_deadlock_lifecycle(tmp_path, monkeypatch):
    """The exact deadlock regression: a proven run must survive its own
    pre-commit gate, the commit must land, post-commit must seal, and a
    second commit without a new run must be blocked under REQUIRE_RUN."""
    home = tmp_path / "home"
    monkeypatch.setenv("AGENT_ULTRA_HOME", str(home))
    ws = tmp_path / "ws"

    # a passing pipeline that stays ACTIVE (git-workflow lifecycle)
    outcome = run_bob("add a slugify helper", ws, mock=True,
                      interactive=False, seal_on_pass=False, out=io.StringIO())
    assert outcome.passed
    marker = ws / ".agent-ultra" / "bob" / "ACTIVE"
    assert marker.is_file(), "run must stay ACTIVE until the commit lands"

    # a real git repo with the split hooks (demo mode: --allow-mock)
    assert _git(ws, "init", "-q").returncode == 0
    _git(ws, "config", "user.email", "bob@example.com")
    _git(ws, "config", "user.name", "bob")
    from agent_ultra.bob import install_hooks
    install_hooks(ws, python=sys.executable, gate_flags="--allow-mock")

    (ws / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    _git(ws, "add", "-A")

    env = {"AGENT_ULTRA_REQUIRE_RUN": "1", "AGENT_ULTRA_HOME": str(home)}
    p = _git(ws, "commit", "-m", "feat: slugify (bob run proven)", env=env)
    assert p.returncode == 0, (
        f"the proven commit must LAND (deadlock regression):\n"
        f"stdout={p.stdout}\nstderr={p.stderr}")

    # post-commit sealed the run — ACTIVE gone only AFTER the commit
    assert not marker.exists(), "post-commit must seal (release ACTIVE)"
    gp = json.loads((ws / ".agent-ultra" / "bob" / "runs" / outcome.run_id
                     / "gate-pass.json").read_text(encoding="utf-8"))
    head = _git(ws, "rev-parse", "HEAD").stdout.strip()
    assert gp.get("commit") == head, "seal must stamp the landed commit hash"
    assert gp.get("sealed_utc"), "seal must record when it sealed"

    # a second commit with NO active run is blocked under REQUIRE_RUN=1
    (ws / "unproven.py").write_text("x = 1\n", encoding="utf-8")
    _git(ws, "add", "unproven.py")
    p2 = _git(ws, "commit", "-m", "sneaky: no pipeline run", env=env)
    assert p2.returncode != 0, "commit without an active run must be blocked"
    assert "AGENT_ULTRA_REQUIRE_RUN" in (p2.stdout + p2.stderr)
    # without the policy env, non-interference still applies
    p3 = _git(ws, "commit", "-m", "operator commit (no policy)",
              env={"AGENT_ULTRA_HOME": str(home)})
    assert p3.returncode == 0
