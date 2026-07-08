"""The bob runner — drives the 10 steps end to end and gates the report.

    from agent_ultra.bob import run_bob
    outcome = run_bob("add a slugify helper", workspace, mock=True)

Mock mode (no API key) uses the bundled sample task for MODEL CONTENT only;
pytest, ultracode, the panel, and the gate all really execute (see sample.py).
On a real route the spec/tests/implementation come from the configured model;
if the model's output does not genuinely fail at RED and pass at GREEN, the
run ends blocked — the pipeline never pretends.

The final report is printed ONLY when ``gate_check`` passes; a failing chain
prints the exact errors and exits nonzero. Committing stays in the operator's
hands: the gate seals the run and tells you it is safe to commit.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .pipeline import (
    BobRun, BobProofError, gate_check, run_pytest_step, GateResult,
    sha256_file, safe_rel, ultracode_receipt_fingerprint,
)
from . import sample
from .workflows import SECURITY_WORKFLOW, REVIEW_WORKFLOW


@dataclass
class BobOutcome:
    run_id: str
    passed: bool
    blocked_at: str = ""          # step name when the pipeline stopped early
    gate: "GateResult | None" = None
    steps: list = field(default_factory=list)   # (step, one-line summary)
    report: dict = field(default_factory=dict)


def _say(out, line: str) -> None:
    print(line, file=out or sys.stdout)


def _excerpt(workspace: Path, files, limit: int = 6000) -> str:
    parts = []
    for rel in files:
        p = workspace / rel
        try:
            parts.append(f"### {rel}\n" + p.read_text(encoding="utf-8"))
        except OSError:
            continue
    return "\n".join(parts)[:limit]


# -- real-route content generation (mock mode uses sample.py instead) --------

def _model_json(pool, prompt: str, max_tokens: int = 4000):
    from ..panel.json_extract import extract_json
    model = pool.routes[0]
    raw = pool.client_for(model).complete(model, prompt, max_tokens)
    return extract_json(raw)


def _gen_spec(pool, task: str) -> str:
    obj = _model_json(pool,
        "Write a one-paragraph implementation spec for this task. Reply with "
        f'ONLY JSON: {{"spec": "<paragraph>"}}.\nTask: {task}')
    return str((obj or {}).get("spec", "") or "").strip()


def _gen_files(pool, task: str, spec: str) -> "tuple[str, str, str, str] | None":
    """Ask the model for (test_file, tests, impl_file, stub) then later the
    implementation. Returns None when the model output is unusable."""
    obj = _model_json(pool,
        "Write pytest contract tests (RED-first) plus a stub for this task. "
        'Reply with ONLY JSON: {"test_file": "test_x.py", "tests": "<file '
        'content>", "impl_file": "x.py", "stub": "<stub that raises '
        'NotImplementedError>"}.\n'
        f"Task: {task}\nSpec: {spec}", max_tokens=6000)
    if not isinstance(obj, dict):
        return None
    keys = ("test_file", "tests", "impl_file", "stub")
    if not all(str(obj.get(k, "") or "").strip() for k in keys):
        return None
    return tuple(str(obj[k]) for k in keys)   # type: ignore[return-value]


def _gen_impl(pool, task: str, spec: str, tests: str, impl_file: str) -> str:
    obj = _model_json(pool,
        "Implement the module so ALL these tests pass. The tests are "
        "READ-ONLY. Reply with ONLY JSON: "
        f'{{"content": "<full content of {impl_file}>"}}.\n'
        f"Task: {task}\nSpec: {spec}\nTests:\n{tests}", max_tokens=8000)
    return str((obj or {}).get("content", "") or "")


# -- the pipeline -------------------------------------------------------------

def run_bob(task: str, workspace: str | Path, *, mock: bool = False,
            pool=None, key_path=None, ultracode_home=None,
            interactive: "bool | None" = None, force: bool = False,
            seal_on_pass: bool = True, out=None) -> BobOutcome:
    """Run SPEC->...->COMMIT-gate in *workspace*. Returns a BobOutcome; the
    caller maps ``passed`` to the exit code. ``pool`` is a RoutePool (mock
    or real); ``ultracode_home`` and ``key_path`` exist so tests and the
    doctor can isolate all state. ``force`` abandons an existing unproven
    active run instead of refusing to start.

    ``seal_on_pass=False`` marks the gate pass but keeps the run ACTIVE —
    the git-workflow lifecycle, where the post-commit hook seals after the
    commit lands (see `bob hook-install`). The default seals immediately,
    which is correct for the report path (mock demo, no commit follows)."""
    from ..routes.pool import RoutePool
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    if mock:
        # two dialects of the offline route: the ultracode mock answers
        # schema prompts; the panel mock answers challenge/verdict prompts
        from ..ultracode import demo_pool
        from ..routes.mock import demo_panel_client
        pool = demo_pool()
        panel_pool = RoutePool(["mock-a"], client=demo_panel_client())
    elif pool is None:
        raise ValueError("a real run needs a configured RoutePool; "
                         "pass pool= or use mock=True")
    else:
        panel_pool = pool
    if interactive is None:
        interactive = (not mock) and sys.stdin.isatty()

    try:
        run = BobRun.start(workspace, goal=task, key_path=key_path, force=force)
    except ValueError as e:
        _say(out, f"cannot start: {e}")
        return BobOutcome(run_id="", passed=False, blocked_at="start")
    steps: list = []

    def record(step, summary):
        steps.append((step, summary))
        _say(out, f"  [{step}] {summary}")

    def blocked(step, why) -> BobOutcome:
        _say(out, f"\nBLOCKED at {step}: {why}")
        _say(out, f"run {run.run_id} stays ACTIVE — finish or redo the step, "
                  "then `agent-ultra bob gate`.")
        return BobOutcome(run_id=run.run_id, passed=False, blocked_at=step,
                          steps=steps)

    _say(out, f"bob run {run.run_id} — {task}"
              + ("  [mock: sample task, no key]" if mock else ""))

    # ---- 1. SPEC -----------------------------------------------------------
    spec = sample.SAMPLE_SPEC if mock else _gen_spec(pool, task)
    if not spec:
        return blocked("step01_spec", "no spec produced")
    run.write("step01_spec", {"goal": task, "spec": spec}, writer="agent",
              mock=mock)
    record("step01_spec", spec[:70] + ("..." if len(spec) > 70 else ""))

    # ---- 2. RED -------------------------------------------------------------
    if mock:
        test_file, tests, impl_file, stub = (sample.TEST_FILE,
                                             sample.SAMPLE_TESTS,
                                             sample.IMPL_FILE,
                                             sample.SAMPLE_STUB)
    else:
        gen = _gen_files(pool, task, spec)
        if gen is None:
            return blocked("step02_red", "model produced no usable tests")
        test_file, tests, impl_file, stub = gen
    # model-authored file names are untrusted: they must land INSIDE the
    # workspace (no absolute paths, no `..` traversal). This mirrors the rest
    # of the kit's "model output is untrusted input" stance.
    try:
        test_file = safe_rel(workspace, test_file)
        impl_file = safe_rel(workspace, impl_file)
    except ValueError as e:
        return blocked("step02_red", f"unsafe model-authored path: {e}")
    for rel, content in ((test_file, tests), (impl_file, stub)):
        p = workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    ok, _rec, msg = run_pytest_step(run, "red", test_file,
                                    extra_files=[impl_file], mock=mock)
    record("step02_red", msg)
    if not ok:
        return blocked("step02_red", msg)

    # ---- 3. GREEN -------------------------------------------------------------
    impl = sample.SAMPLE_IMPL if mock else _gen_impl(pool, task, spec, tests,
                                                     impl_file)
    if not impl:
        return blocked("step03_green", "model produced no implementation")
    (workspace / impl_file).write_text(impl, encoding="utf-8")
    ok, _rec, msg = run_pytest_step(run, "green", test_file,
                                    extra_files=[impl_file], mock=mock)
    record("step03_green", msg)
    if not ok:
        return blocked("step03_green", msg)

    # ---- 4/5. REFACTOR + CODE-QUALITY (judgment steps, notes only) ----------
    run.write("step04_refactor",
              {"notes": "reviewed for structure; no edits needed"
               if mock else "see step07 review notes"},
              writer="agent", files=[test_file, impl_file], mock=mock)
    record("step04_refactor", "no edits (hashes re-pinned)")
    run.write("step05_codequality",
              {"checklist": ["names read clearly", "no dead code",
                             "errors surface, not swallowed"]},
              writer="agent", files=[test_file, impl_file], mock=mock)
    record("step05_codequality", "checklist recorded")

    # ---- 6/7. fan-out steps via ultracode ------------------------------------
    from ..ultracode import UltracodeEngine
    eng = UltracodeEngine(pool, home=ultracode_home)
    excerpt = _excerpt(workspace, [test_file, impl_file])
    excerpt_sha = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()

    def _fanout_evidence(rep, wf_name, extra):
        """Bind the ultracode run to THIS bob run: the recorded fingerprint
        (file-bytes sha of the run's receipt.json) is stamped into the
        HMAC-signed bob receipt, so editing the ultracode receipt or pointing
        at a different run breaks the signed chain (see gate cross-check)."""
        fp = ultracode_receipt_fingerprint(eng.home, rep.run_id)
        ev = {"ultracode": {"run_id": rep.run_id, "home": str(eng.home),
                            "workflow_name": wf_name,
                            "receipt_file_sha256": fp},
              "excerpt_sha256": excerpt_sha}
        ev.update(extra)
        return ev

    rep = eng.run_script(SECURITY_WORKFLOW,
                         args={"goal": task, "target_excerpt": excerpt})
    if rep.final_state != "COMPLETE":
        return blocked("step06_security",
                       f"security fan-out ended {rep.final_state}")
    result = rep.result or {}
    run.write("step06_security",
              _fanout_evidence(rep, "bob-security",
                               {"lenses": result.get("lenses", []),
                                "findings": result.get("findings", [])}),
              writer="engine", files=[test_file, impl_file], mock=mock)
    record("step06_security",
           f"{rep.agents_run} agents, {len(result.get('findings', []))} finding(s)")

    rep = eng.run_script(REVIEW_WORKFLOW,
                         args={"goal": task, "spec": spec,
                               "target_excerpt": excerpt})
    if rep.final_state != "COMPLETE":
        return blocked("step07_workflow",
                       f"review fan-out ended {rep.final_state}")
    result = rep.result or {}
    run.write("step07_workflow",
              _fanout_evidence(rep, "bob-review",
                               {"dimensions": result.get("dimensions", []),
                                "reviews": result.get("reviews", {})}),
              writer="engine", files=[test_file, impl_file], mock=mock)
    record("step07_workflow",
           f"{rep.agents_run} agents over {len(result.get('dimensions', []))} dimensions")

    # ---- 8. ULTRA (adversarial panel + its execution receipt) ----------------
    from ..panel.engine import PanelEngine, PanelError
    from ..panel_receipt import build_receipt, write_receipt
    engine = PanelEngine(panel_pool)
    reviewed = _excerpt(workspace, [impl_file])
    try:
        report = engine.run(f"Is this implementation of '{task}' safe and "
                            "correct for production?",
                            size="small",
                            lenses=["security", "correctness", "failure-modes"],
                            context=reviewed)
    except PanelError as e:
        return blocked("step08_panel", f"panel error: {e}")
    panel_dir = run.run_dir / "panel"
    reviewed_sha = hashlib.sha256(reviewed.encode("utf-8")).hexdigest()
    panel_path = write_receipt(panel_dir,
                               build_receipt(report, task_id=run.run_id,
                                             reviewed_input=reviewed))
    # cover the panel receipt file itself so the chain's staleness check
    # re-hashes it — an edit-then-recompute of its own checksum still trips
    # the signed bob receipt.
    run.write("step08_panel",
              {"panel_run_dir": panel_dir.relative_to(workspace).as_posix(),
               "expect_task_id": run.run_id,
               "expect_artifact_hash": reviewed_sha,
               "decision": report.decision,
               "accepted_findings": len(report.accepted)},
              writer="engine",
              files=[test_file, impl_file, panel_path], mock=mock)
    record("step08_panel", f"decision: {str(report.decision)[:60]}")

    # ---- 9. QUIZ ---------------------------------------------------------------
    questions = list(sample.SAMPLE_QUIZ_QUESTIONS) if mock else [
        f"What behavior does {test_file} pin down, in your own words?",
        "Which edge case would you add a test for next?",
        "What would make you revert this change?",
    ]
    operator_response = ""
    outcome = "skipped"
    if interactive:
        _say(out, "\nquiz — answer to claim 'passed' (empty answer skips):")
        for q in questions:
            _say(out, f"  Q: {q}")
        try:
            operator_response = input("  A: ").strip()
        except (EOFError, OSError):
            operator_response = ""
        outcome = "passed" if operator_response else "skipped"
    run.write("step09_quiz",
              {"questions": questions, "outcome": outcome,
               "operator_response": operator_response},
              writer="agent" if outcome == "passed" else "engine", mock=mock)
    record("step09_quiz", f"outcome={outcome} ({len(questions)} questions)")

    # ---- 10. COMMIT gate ---------------------------------------------------------
    # report path: nothing is staged yet, so staged_files=[] — the receipts
    # themselves must hold. The pre-commit `bob gate` enforces staged coverage.
    gate = gate_check(run, allow_mock=mock, staged_files=[])
    if not gate.passed:
        _say(out, "\ngate BLOCKED the report:")
        for e in gate.errors:
            _say(out, f"  ! {e}")
        return BobOutcome(run_id=run.run_id, passed=False,
                          blocked_at="step10_commit", gate=gate, steps=steps)
    if seal_on_pass:
        run.complete(gate.to_dict())
    else:
        run.mark_pass(gate.to_dict())
    report_doc = {
        "run_id": run.run_id, "task": task, "spec": spec,
        "files": [test_file, impl_file],
        "gate": gate.to_dict(), "mock": mock,
    }
    (run.run_dir / "report.json").write_text(
        json.dumps(report_doc, indent=2, ensure_ascii=False), encoding="utf-8")
    _say(out, f"\ngate PASSED — receipt chain validates "
              f"({len(run.chain())} receipts).")
    _say(out, f"report: {run.run_dir / 'report.json'}")
    if seal_on_pass:
        _say(out, "safe to commit: git add + git commit (re-run "
                  "`agent-ultra bob gate` after staging).")
    else:
        _say(out, "pass marked; run stays ACTIVE — the post-commit hook "
                  "seals it once your commit lands (`bob hook-install`).")
    return BobOutcome(run_id=run.run_id, passed=True, gate=gate, steps=steps,
                      report=report_doc)
