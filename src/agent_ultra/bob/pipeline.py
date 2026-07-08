"""Bob pipeline core — runs, the RED/GREEN pytest runner, and the gate.

Doctrine (public port of the 10-step enforced build pipeline):

    SPEC -> RED -> GREEN -> REFACTOR -> CODE-QUALITY -> SECURITY-FANOUT
         -> WORKFLOW -> ULTRA -> QUIZ -> COMMIT

    real execution -> receipt -> gate validates the chain -> commit/report

Enforcement comes from WHO writes a receipt and what the gate can RE-DERIVE
or CROSS-CHECK — never from trusting a claim:

  * steps 2/3 (RED/GREEN): receipts are written by ``run_pytest_step`` from
    actual pytest output (``writer="system"``); the gate re-runs pytest live.
  * steps 6/7 (fan-out): receipts point at an ultracode run; the gate loads
    that run's own checksummed receipt, re-verifies it, and demands real
    agent calls. An invented run_id, an empty run, or a doctored receipt
    all block.
  * step 8 (panel): the panel engine writes its execution receipt from real
    model calls; the gate validates it structurally — a self-review is not
    a panel.
  * step 9 (quiz): ``passed`` requires a captured operator response;
    otherwise the honest outcome is ``skipped``.
  * every receipt is hash-chained and HMAC-signed (see receipts.py), so
    skipping, re-ordering, editing, or hand-authoring a step breaks the
    chain and the gate fails closed.

Receipts live in ``<workspace>/.agent-ultra/bob/runs/<stamp>/``, one JSON per
step. Stdlib only.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .receipts import (
    STEPS, GATED_STEPS, BobReceiptError,
    build_step_receipt, load_receipt, validate_chain, utc_now,
)

# staged files never produced by the build itself
DEFAULT_ALLOW_UNCOVERED = ("*.md", ".agent-ultra/*", ".gitignore")


class BobProofError(Exception):
    """Raised when completion is claimed while an active run is unproven."""


def default_key_path() -> Path:
    home = os.environ.get("AGENT_ULTRA_HOME", "")
    base = Path(home) if home else Path.home() / ".agent-ultra"
    return base / "bob.key"


def load_or_create_key(key_path: str | Path | None = None) -> bytes:
    """Per-install signing key. Created on first use, never committed
    (it lives under the agent-ultra home, not the workspace)."""
    p = Path(key_path) if key_path else default_key_path()
    if p.is_file():
        return p.read_bytes()
    p.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    p.write_bytes(key)
    return key


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_rel(workspace: Path, name: str) -> str:
    """Return *name* as a workspace-relative POSIX path, or raise ValueError
    if it is absolute or escapes the workspace via ``..``. Model-authored file
    names pass through here before anything is written."""
    workspace = Path(workspace).resolve()
    cand = Path(name)
    if cand.is_absolute() or cand.drive or cand.anchor:
        raise ValueError(f"{name!r} is absolute")
    target = (workspace / cand).resolve()
    try:
        rel = target.relative_to(workspace)
    except ValueError:
        raise ValueError(f"{name!r} escapes the workspace")
    if not rel.parts:
        raise ValueError(f"{name!r} is the workspace root, not a file")
    return rel.as_posix()


def ultracode_receipt_fingerprint(home, run_id: str) -> str:
    """The sha256 of an ultracode run's receipt.json FILE BYTES, or '' when it
    does not exist. Recorded into the (signed) bob receipt so the gate can
    detect a swapped or edited ultracode receipt regardless of that receipt's
    own weaker self-checksum."""
    p = Path(home) / "runs" / str(run_id) / "receipt.json"
    if not p.is_file():
        return ""
    return sha256_file(p)


def utc_stamp() -> str:
    import time
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

@dataclass
class BobRun:
    """One pipeline run: a receipt directory plus the ACTIVE marker."""

    workspace: Path
    run_dir: Path
    key: bytes = b""

    @staticmethod
    def _root(workspace: Path) -> Path:
        return Path(workspace) / ".agent-ultra" / "bob"

    @classmethod
    def start(cls, workspace: str | Path, goal: str = "",
              key_path: str | Path | None = None,
              force: bool = False) -> "BobRun":
        """Open a run. Refuses (ValueError) if another run is already ACTIVE
        and unproven — starting over would silently orphan it and release it
        from enforcement. Pass ``force=True`` to abandon it deliberately."""
        workspace = Path(workspace)
        marker = cls._root(workspace) / "ACTIVE"
        if marker.is_file() and not force:
            prev = marker.read_text(encoding="utf-8").strip()
            raise ValueError(
                f"a bob run ({prev}) is already active and unproven in this "
                "workspace — finish it (`agent-ultra bob gate`) or pass "
                "force=True to abandon it. Starting over would orphan it.")
        stamp = utc_stamp()
        run_dir = cls._root(workspace) / "runs" / stamp
        n = 0
        while run_dir.exists():        # same-second collision
            n += 1
            run_dir = cls._root(workspace) / "runs" / f"{stamp}-{n}"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(json.dumps({
            "run_id": run_dir.name, "goal": goal, "started_utc": utc_now(),
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        marker.write_text(run_dir.name, encoding="utf-8")
        return cls(workspace=workspace, run_dir=run_dir,
                   key=load_or_create_key(key_path))

    @classmethod
    def active(cls, workspace: str | Path,
               key_path: str | Path | None = None) -> "BobRun | None":
        workspace = Path(workspace)
        marker = cls._root(workspace) / "ACTIVE"
        if not marker.is_file():
            return None
        name = marker.read_text(encoding="utf-8").strip()
        run_dir = cls._root(workspace) / "runs" / name
        if not run_dir.is_dir():
            # A marker naming a missing run dir is TAMPERING, not "no run":
            # complete() is the only code that removes the marker. Surface a
            # sentinel run whose (empty) chain fails every gate, so the gate
            # blocks instead of reporting "nothing to enforce".
            return cls(workspace=workspace, run_dir=run_dir,
                       key=load_or_create_key(key_path))
        return cls(workspace=workspace, run_dir=run_dir,
                   key=load_or_create_key(key_path))

    def marker_orphaned(self) -> bool:
        return not self.run_dir.is_dir()

    @property
    def run_id(self) -> str:
        return self.run_dir.name

    def goal(self) -> str:
        try:
            meta = json.loads((self.run_dir / "run.json").read_text(
                encoding="utf-8"))
            return str(meta.get("goal", ""))
        except (OSError, ValueError):
            return ""

    def receipt_path(self, step: str) -> Path:
        if step not in STEPS:
            raise BobReceiptError(f"unknown pipeline step: {step}")
        return self.run_dir / f"{step}.json"

    def load(self, step: str) -> dict | None:
        return load_receipt(self.receipt_path(step))

    def chain(self) -> list[dict]:
        """All written receipts in seq order (the order they were written)."""
        recs = [r for s in STEPS if (r := self.load(s)) is not None]
        return sorted(recs, key=lambda r: r.get("seq", 0))

    def _next_link(self) -> tuple[int, str]:
        chain = self.chain()
        if not chain:
            return 0, ""
        last = chain[-1]
        return int(last.get("seq", 0)) + 1, str(last.get("receipt_sha256", ""))

    def hash_files(self, files) -> dict:
        hashes: dict = {}
        for f in files or []:
            f = Path(f)
            if not f.is_absolute():
                f = self.workspace / f
            rel = str(f.resolve().relative_to(self.workspace.resolve()))
            hashes[rel.replace("\\", "/")] = sha256_file(f)
        return hashes

    def write(self, step: str, evidence: dict, writer: str,
              files=None, mock: bool = False) -> dict:
        seq, prev = self._next_link()
        rec = build_step_receipt(
            step=step, run_id=self.run_id, seq=seq, writer=writer,
            evidence=evidence, files=self.hash_files(files),
            prev_sha256=prev, mock=mock, key=self.key or None)
        self.receipt_path(step).write_text(
            json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        return rec

    def mark_pass(self, gate_result: dict) -> None:
        """Record the gate's pass log WITHOUT releasing the ACTIVE marker.
        Called by the pre-commit gate: the run must stay active while the
        commit it just validated is still in flight — releasing here is the
        commit deadlock (pass -> ACTIVE gone -> a require-run policy blocks
        the very commit that passed). ``seal()`` releases, after the commit
        lands."""
        (self.run_dir / "gate-pass.json").write_text(
            json.dumps({"completed_utc": utc_now(),
                        "git_head": git_head(self.workspace),
                        "gate": gate_result},
                       indent=2, ensure_ascii=False), encoding="utf-8")

    def seal(self) -> bool:
        """Release the ACTIVE marker after the commit landed (post-commit
        hook). Refuses unless ``mark_pass()`` ran — a run must never be
        sealed without a recorded gate pass. Records the landed commit hash;
        that hash plus gate-pass.json is the step-10 record (no agent-written
        step10 receipt on purpose). Returns True if sealed."""
        gp = self.run_dir / "gate-pass.json"
        if not gp.is_file():
            return False
        try:
            data = json.loads(gp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data["sealed_utc"] = utc_now()
        data["commit"] = git_head(self.workspace)
        gp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                      encoding="utf-8")
        marker = self._root(self.workspace) / "ACTIVE"
        if marker.is_file() and marker.read_text(
                encoding="utf-8").strip() == self.run_id:
            marker.unlink()
        return True

    def complete(self, gate_result: dict) -> None:
        """mark_pass + seal in one step — for callers that validate and
        finish OUTSIDE a commit (the mock demo's report path, an operator
        closing a run manually). Inside a git workflow use the split:
        pre-commit marks the pass, post-commit seals."""
        self.mark_pass(gate_result)
        self.seal()


# --------------------------------------------------------------------------
# system pytest runner (steps 2 and 3)
# --------------------------------------------------------------------------

# -v node lines: "tests/test_x.py::test_a PASSED [ 50%]" and parametrized ids
# that contain spaces: "test_x.py::test_a[hello world] PASSED [ 50%]". The id
# is everything up to the status word, which pytest right-pads before the
# result; require "::" so prose lines don't match.
_NODE_RE = re.compile(
    r"^(\S+::.+?)\s+(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)"
    r"(?=\s|$|\s*\[)", re.M)


def _run_pytest(workspace: Path, test_file: str, timeout: int) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "pytest", test_file, "-v", "--tb=short",
           "-p", "no:cacheprovider"]
    try:
        p = subprocess.run(cmd, cwd=str(workspace), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + "\n" + (p.stderr or "")
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
        return -1, f"TIMEOUT or spawn error after {timeout}s"


def parse_pytest_nodes(output: str) -> dict:
    """{node_id: status} from verbose pytest output."""
    return {m.group(1): m.group(2) for m in _NODE_RE.finditer(output)}


def run_pytest_step(run: BobRun, step: str, test_file: str,
                    extra_files=None, timeout: int = 600,
                    mock: bool = False) -> tuple[bool, dict, str]:
    """Execute pytest and write the RED or GREEN receipt from its ACTUAL
    output (``writer="system"``). Returns (requirement_met, receipt, message).
    The receipt is written either way — it records what really happened, and
    the gate rejects a chain whose evidence misses the step's requirement.
    The pytest invocation is classified through the command broker's risk
    tiers and the tier is recorded in the evidence.
    """
    if step not in ("red", "green"):
        raise ValueError("step must be 'red' or 'green'")
    step_name = "step02_red" if step == "red" else "step03_green"

    from ..broker.broker import classify
    shown_cmd = f"{Path(sys.executable).name} -m pytest {test_file} -v"
    tier, _why = classify(shown_cmd)

    exit_code, output = _run_pytest(run.workspace, test_file, timeout)
    nodes = parse_pytest_nodes(output)
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "other": 0}
    for status in nodes.values():
        k = status.lower()
        counts[k if k in counts else "other"] += 1

    evidence = {
        "test_file": test_file,
        "command": shown_cmd,
        "broker_tier": tier,
        "exit_code": exit_code,
        "test_ids": sorted(nodes),
        "statuses": nodes,
        "counts": counts,
        "output_tail": output[-4000:],
        "output_sha256": hashlib.sha256(
            output.encode("utf-8", errors="ignore")).hexdigest(),
    }

    if step == "red":
        # failing tests, not broken ones: import/collection mistakes are
        # ERRORs and they don't count as a proper RED
        ok = bool(nodes) and counts["failed"] >= 1 and counts["error"] == 0
        msg = (f"RED: {counts['failed']} failing / {len(nodes)} collected"
               if ok else
               f"RED requirement NOT met: failed={counts['failed']} "
               f"error={counts['error']} collected={len(nodes)} exit={exit_code}")
    else:
        ok = exit_code == 0 and counts["passed"] >= 1 and counts["failed"] == 0
        msg = (f"GREEN: {counts['passed']} passing" if ok else
               f"GREEN requirement NOT met: passed={counts['passed']} "
               f"failed={counts['failed']} exit={exit_code}")

    evidence["requirement_met"] = bool(ok)
    files = [Path(test_file)] + [Path(f) for f in (extra_files or [])]
    receipt = run.write(step_name, evidence, writer="system", files=files,
                        mock=mock)
    return bool(ok), receipt, msg


# --------------------------------------------------------------------------
# cross-checks for the fan-out and panel steps
# --------------------------------------------------------------------------

def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def check_ultracode_evidence(evidence: dict, *, allow_mock: bool,
                             receipt_mock: bool,
                             expect_workflow: str = "") -> list[str]:
    """Cross-check a step-6/7 receipt against the ultracode run it names.

    The bob step receipt (HMAC-signed) records the ultracode receipt's
    file-bytes fingerprint and the workflow name; here we re-derive both and
    demand the run actually fanned out. A fabricated run_id (no file), an
    edited receipt (fingerprint mismatch), the wrong workflow (borrowed run),
    zero agents, or a checksum failure all fail closed.
    """
    from ..ultracode import verify_receipt
    if not isinstance(evidence, dict):
        return ["fan-out evidence malformed"]
    errors: list[str] = []
    uc = evidence.get("ultracode")
    uc = uc if isinstance(uc, dict) else {}
    run_id = str(uc.get("run_id", "") or "")
    home = str(uc.get("home", "") or "")
    if not run_id or not home:
        return ["no ultracode run recorded — fan-out may be fabricated"]
    receipt_path = Path(home) / "runs" / run_id / "receipt.json"
    if not receipt_path.is_file():
        return [f"ultracode run {run_id!r} has no receipt on disk — "
                "fabricated run_id?"]
    # the signed bob receipt pinned the ultracode receipt's file bytes: an
    # edited or swapped receipt breaks this without needing the ultracode
    # engine's (weaker, keyless) self-checksum to catch it.
    recorded_fp = str(uc.get("receipt_file_sha256", "") or "")
    actual_fp = sha256_file(receipt_path)
    if not recorded_fp:
        errors.append("fan-out receipt has no recorded ultracode fingerprint "
                      "— cannot bind it to this run (fail closed)")
    elif recorded_fp != actual_fp:
        errors.append(f"ultracode receipt for {run_id!r} does not match the "
                      "fingerprint pinned at run time — swapped or edited")
    try:
        rec = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return errors + [f"ultracode receipt unreadable: {e}"]
    if not isinstance(rec, dict):
        return errors + ["ultracode receipt is not an object"]
    if not verify_receipt(rec):
        errors.append(f"ultracode receipt for {run_id!r} fails its checksum "
                      "— edited after the run")
    if str(rec.get("run_id", "")) != run_id:
        errors.append(f"ultracode receipt's own run_id {rec.get('run_id')!r} "
                      f"!= claimed {run_id!r} — mismatched receipt")
    recorded_name = str(uc.get("workflow_name", "") or "")
    actual_name = str(rec.get("name", "") or "")
    if expect_workflow and recorded_name != expect_workflow:
        errors.append(f"fan-out claims workflow {recorded_name!r}, expected "
                      f"{expect_workflow!r} for this step")
    if expect_workflow and actual_name != expect_workflow:
        errors.append(f"ultracode run is workflow {actual_name!r}, not the "
                      f"expected {expect_workflow!r} — borrowed run?")
    agents = rec.get("agents")
    agents = agents if isinstance(agents, list) else []
    done = [a for a in agents if isinstance(a, dict) and a.get("status") == "ok"]
    if not done:
        errors.append(f"ultracode run {run_id!r} completed 0 agents — "
                      "an empty fan-out proves nothing")
    if _as_int(rec.get("model_calls")) + _as_int(rec.get("cached_hits")) <= 0:
        errors.append(f"ultracode run {run_id!r} made 0 model calls")
    if receipt_mock and not allow_mock:
        errors.append("receipt is marked mock — mock evidence only satisfies "
                      "the gate in demo mode (--allow-mock)")
    return errors


def check_panel_evidence(evidence: dict, workspace: Path) -> list[str]:
    """Step 8: validate the panel's own execution receipt AND bind it to this
    run — the signed step receipt pins the expected task_id and the sha256 of
    the reviewed source, so a borrowed panel (different task/code) is rejected
    even though its own checksum is intact. A self-review is not a panel; a
    panel of someone else's code is not a review of this change."""
    from ..panel_receipt import validate_receipt
    if not isinstance(evidence, dict):
        return ["panel evidence malformed"]
    art = str(evidence.get("panel_run_dir", "") or "")
    if not art:
        return ["no panel run dir recorded"]
    path = Path(art)
    if not path.is_absolute():
        path = workspace / path
    expect_task = str(evidence.get("expect_task_id", "") or "")
    expect_hash = str(evidence.get("expect_artifact_hash", "") or "")
    if not expect_task or not expect_hash:
        return ["panel evidence is not bound to this run (missing "
                "expect_task_id/expect_artifact_hash) — fail closed"]
    ok, msg = validate_receipt(path, expect_task_id=expect_task,
                               expect_artifact_hash=expect_hash)
    return [] if ok else [msg]


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------

@dataclass
class GateResult:
    passed: bool
    errors: list = field(default_factory=list)
    checked: dict = field(default_factory=dict)   # step -> one-line summary

    def to_dict(self) -> dict:
        return {"passed": self.passed, "errors": self.errors,
                "checked": self.checked}


def _staged_files(workspace: Path) -> "list[str] | None":
    """Staged paths RELATIVE TO THE WORKSPACE (``git diff --cached
    --relative``), or None when git cannot answer (not a repo, git missing) —
    the caller fails that closed rather than treating it as 'nothing staged'."""
    try:
        p = subprocess.run(["git", "-C", str(workspace), "diff", "--cached",
                            "--relative", "--name-only"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return [ln.strip().replace("\\", "/") for ln in p.stdout.splitlines()
            if ln.strip()]


def gate_check(run: BobRun, *, rerun_tests: bool = True,
               allow_mock: bool = False, staged_files=None,
               allow_uncovered: tuple = DEFAULT_ALLOW_UNCOVERED) -> GateResult:
    """Validate the whole receipt chain. Fail-closed: a check that cannot be
    performed (missing receipt, unreadable/malformed artifact, unverifiable
    chain, git unavailable for coverage) is a failure, not a warning — any
    unexpected error is caught and reported as a block, never a crash.

    ``staged_files=None`` reads git's staged list; pass ``[]`` to skip the
    coverage check explicitly (report path, where nothing is staged but the
    receipts themselves must still hold).
    """
    try:
        return _gate_check(run, rerun_tests=rerun_tests, allow_mock=allow_mock,
                           staged_files=staged_files,
                           allow_uncovered=allow_uncovered)
    except Exception as e:   # fail closed: a malformed receipt blocks, not crashes
        return GateResult(passed=False,
                          errors=[f"gate error (fail-closed): "
                                  f"{type(e).__name__}: {e}"])


def _mock_flag_error(rec: dict, step: str, allow_mock: bool) -> "str | None":
    if rec.get("mock") and not allow_mock:
        return (f"{step}: receipt marked mock — mock evidence only satisfies "
                "the gate in demo mode (--allow-mock)")
    return None


def _gate_check(run: BobRun, *, rerun_tests: bool, allow_mock: bool,
                staged_files, allow_uncovered: tuple) -> GateResult:
    errors: list[str] = []
    checked: dict = {}

    # An ACTIVE marker pointing at a missing run dir is tampering, not "done".
    if run.marker_orphaned():
        return GateResult(passed=False, errors=[
            f"run {run.run_id}: ACTIVE marker names a run directory that no "
            "longer exists — the run was deleted while unproven (fail closed)"])

    # ---- chain spine: order, hashes, signatures -------------------------
    chain = run.chain()
    errors.extend(validate_chain(chain, run.key or None))
    checked["chain"] = f"{len(chain)} receipt(s), hash-chained + signed"

    # ---- completeness ----------------------------------------------------
    have = {r.get("step") for r in chain}
    for step in GATED_STEPS:
        if step not in have:
            errors.append(f"{step}: receipt missing — the step never ran "
                          "(or its receipt was deleted)")

    def _ev(rec) -> dict:
        ev = rec.get("evidence") if isinstance(rec, dict) else None
        return ev if isinstance(ev, dict) else {}

    def _counts(ev) -> dict:
        c = ev.get("counts")
        return c if isinstance(c, dict) else {}

    # ---- step 2: RED ------------------------------------------------------
    red = run.load("step02_red")
    red_ids: set = set()
    if red is not None:
        ev = _ev(red)
        red_ids = set(ev.get("test_ids") or [])
        if red.get("writer") != "system":
            errors.append("step02_red: receipt not written by the pytest "
                          "runner (writer != system)")
        if not ev.get("requirement_met"):
            errors.append("step02_red: tests did not actually fail cleanly")
        if _as_int(_counts(ev).get("failed")) < 1:
            errors.append("step02_red: 0 test failures recorded")
        if _as_int(_counts(ev).get("error")) > 0:
            errors.append("step02_red: collection/import ERRORs recorded — "
                          "broken tests are not failing tests")
        if (e := _mock_flag_error(red, "step02_red", allow_mock)):
            errors.append(e)
        checked["step02_red"] = (f"{len(red_ids)} tests, "
                                 f"{_as_int(_counts(ev).get('failed'))} failing")

    # ---- step 3: GREEN ----------------------------------------------------
    green = run.load("step03_green")
    test_file = ""
    if green is not None:
        ev = _ev(green)
        test_file = str(ev.get("test_file", "") or "")
        green_ids = set(ev.get("test_ids") or [])
        if green.get("writer") != "system":
            errors.append("step03_green: receipt not written by the pytest "
                          "runner (writer != system)")
        if not ev.get("requirement_met"):
            errors.append("step03_green: tests did not all pass")
        if not test_file:
            errors.append("step03_green: no test_file recorded — the live "
                          "re-run cannot be performed (fail closed)")
        if red is not None and not red_ids <= green_ids:
            missing = sorted(red_ids - green_ids)[:5]
            errors.append(f"step03_green: RED tests missing from the GREEN "
                          f"run: {missing}")
        if (e := _mock_flag_error(green, "step03_green", allow_mock)):
            errors.append(e)
        checked["step03_green"] = f"{len(green_ids)} tests passing"

    # ---- live re-run (re-derived, never trusted) ---------------------------
    if rerun_tests and green is not None and test_file:
        exit_code, output = _run_pytest(run.workspace, test_file, timeout=600)
        live_ids = set(parse_pytest_nodes(output))
        if exit_code != 0:
            errors.append(f"live re-run: pytest exit {exit_code} — the "
                          "current state does not pass its own tests")
        if red is not None and not red_ids <= live_ids:
            errors.append("live re-run: tests recorded at RED are no longer "
                          "collected — the test file changed after receipts")
        checked["live_rerun"] = f"exit {exit_code}, {len(live_ids)} tests"

    # ---- steps 6/7: fan-out cross-checked against ultracode artifacts -----
    for step, extra, wf_name in (("step06_security", "", "bob-security"),
                                 ("step07_workflow", "dimensions", "bob-review")):
        rec = run.load(step)
        if rec is None:
            continue    # missing already reported
        ev = _ev(rec)
        step_errors = check_ultracode_evidence(
            ev, allow_mock=allow_mock, receipt_mock=bool(rec.get("mock")),
            expect_workflow=wf_name)
        errors.extend(f"{step}: {e}" for e in step_errors)
        if extra and not ev.get(extra):
            errors.append(f"{step}: no {extra} recorded")
        checked[step] = ("ultracode run verified" if not step_errors
                         else "INVALID")

    # ---- step 8: panel -----------------------------------------------------
    panel = run.load("step08_panel")
    if panel is not None:
        perrs = check_panel_evidence(_ev(panel), run.workspace)
        if (e := _mock_flag_error(panel, "panel", allow_mock)):
            perrs = perrs + [e]
        errors.extend(f"step08_panel: {e}" for e in perrs)
        checked["step08_panel"] = "receipt valid" if not perrs else "INVALID"

    # ---- step 9: quiz -------------------------------------------------------
    quiz = run.load("step09_quiz")
    if quiz is not None:
        ev = _ev(quiz)
        if (e := _mock_flag_error(quiz, "step09_quiz", allow_mock)):
            errors.append(e)
        outcome = ev.get("outcome")
        if outcome not in ("passed", "skipped"):
            errors.append(f"step09_quiz: outcome {outcome!r} is not "
                          "passed/skipped")
        if not ev.get("questions"):
            errors.append("step09_quiz: no comprehension questions recorded")
        if outcome == "passed" and not str(
                ev.get("operator_response", "") or "").strip():
            errors.append("step09_quiz: outcome 'passed' without a captured "
                          "operator response — a quiz nobody answered")
        checked["step09_quiz"] = f"outcome={outcome}"

    # ---- staleness: receipts vs the working tree -----------------------------
    coverage: dict = {}
    for rec in chain:
        coverage.update(rec.get("files") or {})
    for rel, expected in sorted(coverage.items()):
        f = Path(run.workspace) / rel
        if not f.is_file():
            errors.append(f"stale: {rel} covered by a receipt but no longer "
                          "exists")
        elif sha256_file(f) != expected:
            errors.append(f"stale: {rel} changed after its last covering "
                          "receipt — re-run the step that owns it")
    checked["staleness"] = f"{len(coverage)} file(s) covered"

    # ---- staged files must be covered -----------------------------------------
    # staged_files=[] (report/claim path) skips this deliberately. None means
    # "read git": inside a work tree, a diff failure fails closed; outside any
    # repo there is no `git commit` to smuggle into, so coverage is moot.
    if staged_files is None:
        if _inside_work_tree(Path(run.workspace)):
            staged_files = _staged_files(Path(run.workspace))
            if staged_files is None:
                errors.append("coverage check: `git diff --cached` failed "
                              "inside a work tree — cannot verify staged "
                              "files (fail closed)")
                staged_files = []
        else:
            staged_files = []
    for rel in staged_files:
        if rel in coverage:
            continue
        if any(fnmatch.fnmatch(rel, pat) for pat in allow_uncovered):
            continue
        errors.append(f"uncovered: staged file {rel} appears in no receipt — "
                      "it was never tested or fanned out for scrutiny")

    return GateResult(passed=not errors, errors=errors, checked=checked)


def _inside_work_tree(workspace: Path) -> bool:
    try:
        p = subprocess.run(["git", "-C", str(workspace), "rev-parse",
                            "--is-inside-work-tree"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and p.stdout.strip() == "true"


def git_head(workspace: Path) -> str:
    try:
        p = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=30)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------------------
# git hooks: pre-commit gates (mark only), post-commit seals
# ---------------------------------------------------------------------------

# The lifecycle that avoids the commit deadlock: the pre-commit gate MARKS a
# pass but keeps ACTIVE (the run must stay active for the commit it just
# validated — sealing here would make any require-run policy block that very
# commit); the post-commit hook SEALS once the commit has landed.

_PRE_HOOK = """#!/bin/sh
# bob pipeline gate (generated by agent-ultra bob hook-install).
# Blocks the commit while a bob run is active and unproven; on pass it only
# MARKS the pass — the post-commit hook seals the run after the commit lands.
# No active run -> exits 0 (non-interference) unless AGENT_ULTRA_REQUIRE_RUN=1.
"{python}" -m agent_ultra.adapters.cli bob gate --workspace "$(git rev-parse --show-toplevel)" --hook --mark-pass{flags}
"""

_POST_HOOK = """#!/bin/sh
# bob pipeline seal (generated by agent-ultra bob hook-install).
# The commit has landed: seal the active run (requires a recorded gate pass;
# refuses otherwise). No active run -> no-op.
"{python}" -m agent_ultra.adapters.cli bob seal --workspace "$(git rev-parse --show-toplevel)" --if-passed
"""


def install_hooks(workspace: Path, python: str = "",
                  gate_flags: str = "") -> Path:
    """Write the pre-commit (gate --mark-pass), pre-merge-commit (git fires
    that hook, not pre-commit, for `git merge` commits), and post-commit
    (seal) hooks in *workspace*. ``gate_flags`` appends extra flags to the
    pre-commit gate (e.g. ``--allow-mock`` for demo workspaces). Returns the
    pre-commit hook path."""
    import stat
    hooks = Path(workspace) / ".git" / "hooks"
    if not hooks.parent.is_dir():
        raise FileNotFoundError(f"not a git repo: {workspace}")
    hooks.mkdir(exist_ok=True)
    fmt = {"python": (python or sys.executable).replace("\\", "/"),
           "flags": (" " + gate_flags.strip()) if gate_flags.strip() else ""}
    written = []
    for name, tmpl in (("pre-commit", _PRE_HOOK),
                       ("pre-merge-commit", _PRE_HOOK),
                       ("post-commit", _POST_HOOK)):
        hook = hooks / name
        hook.write_text(tmpl.format(**fmt), encoding="utf-8", newline="\n")
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP
                   | stat.S_IXOTH)
        written.append(hook)
    return written[0]


def assert_bob_done(workspace: str | Path, *,
                    allow_mock: bool = False,
                    key_path: str | Path | None = None) -> "GateResult | None":
    """Raise BobProofError if a bob run is active in *workspace* and its
    receipt chain does not validate. Returns the GateResult (None when no run
    is active). ``staged_files=[]`` on purpose: at claim time nothing need be
    staged — the receipts themselves must hold."""
    run = BobRun.active(workspace, key_path=key_path)
    if run is None:
        return None
    result = gate_check(run, allow_mock=allow_mock, staged_files=[])
    if not result.passed:
        raise BobProofError(
            f"bob run {run.run_id}: completion claimed without a valid "
            f"receipt chain — {len(result.errors)} failure(s): "
            + "; ".join(result.errors[:5])
            + ("; ..." if len(result.errors) > 5 else ""))
    return result
