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
    STEPS, GATED_STEPS, SURGICAL_GATED_STEPS, BobReceiptError,
    build_step_receipt, load_receipt, validate_chain, utc_now,
)
from .surgical import (
    surgical_ext_allowed, surgical_file_denied, staged_set_qualifies,
    diff_budget_error,
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


def _norm_scope_entry(s: str) -> str:
    """Normalize a scope entry to a forward-slash, workspace-relative path
    with no leading ./ (a trailing / marks a directory)."""
    r = str(s).strip().replace("\\", "/")
    while r.startswith("./"):
        r = r[2:]
    return r


def scope_entry_is_infra(entry: str) -> bool:
    """True when a scope entry names enforcement infrastructure — the git
    hooks or bob's own receipt/marker machinery. Such paths are never a
    legitimate run target: a run must not be able to declare authority over
    the thing that enforces it."""
    segs = _norm_scope_entry(entry).lower().rstrip("/").split("/")
    if ".agent-ultra" in segs:
        return True
    if ".git" in segs:
        i = segs.index(".git")
        return i + 1 >= len(segs) or segs[i + 1] == "hooks"
    return False


def scope_contains(scope, target_rel: str) -> bool:
    """True when *target_rel* (workspace-relative) falls inside *scope* —
    an exact file, anything under a declared directory, or an fnmatch glob.
    Case-folded with .lower() on purpose: os.path.normcase would flip / to
    \\ on Windows and break the forward-slash prefix checks."""
    t = _norm_scope_entry(target_rel).lower()
    for raw in scope or []:
        e = _norm_scope_entry(raw).lower()
        if not e:
            continue
        ed = e.rstrip("/")
        if t == ed:
            return True
        if t.startswith(ed + "/"):
            return True
        if fnmatch.fnmatch(t, e) or fnmatch.fnmatch(t, ed):
            return True
    return False


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
              force: bool = False,
              scope: "list | None" = None,
              operator_unbounded: bool = False,
              mode: str = "full",
              surgical_files: "list | None" = None) -> "BobRun":
        """Open a run. Refuses (ValueError) if another run is already ACTIVE
        and unproven — starting over would silently orphan it and release it
        from enforcement. Pass ``force=True`` to abandon it deliberately.

        ``scope`` — the files/dirs/globs this run is FOR — is MANDATORY: the
        gate rejects staged files outside the declared scope, so a run
        opened for module A cannot smuggle in edits to module B. A run with
        no declared scope fails closed (start refuses; a scope stripped from
        run.json later blocks every staged file at the gate). Expand a live
        run's scope only through ``add_scope`` (validated + logged).

        ``operator_unbounded=True`` is the explicit, operator-only escape:
        no scope binding for this run. It is a Python-API parameter on
        purpose — no CLI verb exposes it, so an agent driving the CLI cannot
        reach it. It is loudly announced and durably logged, and it never
        covers the git hooks or bob's own machinery."""
        workspace = Path(workspace)
        if mode not in ("full", "surgical"):
            raise ValueError(f"unknown bob mode: {mode!r}")
        if mode == "surgical":
            # the surgical lane is for INERT edits only — objective entry
            # criteria validated up front; anything else is full-pipeline
            # work. Its scope IS its declared files, exactly.
            if operator_unbounded:
                raise ValueError(
                    "operator_unbounded is NEVER valid for surgical mode — "
                    "a surgical run's scope is exactly its declared inert "
                    "files")
            if not surgical_files:
                raise ValueError(
                    "surgical_files is required for surgical mode — "
                    "declare the files you intend to edit")
            for f in surgical_files:
                if surgical_file_denied(f):
                    raise ValueError(
                        f"surgical mode denied_path {f!r} — this file can "
                        "execute in CI/build/install contexts; run the "
                        "full pipeline")
                if not surgical_ext_allowed(f):
                    raise ValueError(
                        f"surgical mode rejects code file {f!r} — only "
                        "non-executable text types qualify; run the full "
                        "pipeline")
            scope = list(surgical_files)
        scope = [_norm_scope_entry(s) for s in (scope or []) if str(s).strip()]
        for s in scope:
            if scope_entry_is_infra(s):
                raise ValueError(
                    f"scope {s!r} names enforcement infrastructure "
                    "(.git/hooks or .agent-ultra) — never a run target")
        if operator_unbounded and scope:
            raise ValueError(
                "operator_unbounded cannot be combined with a declared "
                "scope — declare the files OR the operator escape, not both")
        if not scope and not operator_unbounded:
            raise ValueError(
                "a bob run must declare its target scope (the files/dirs "
                "this run is for) — no-scope runs fail closed; expand later "
                "with add_scope if the work legitimately grows")
        marker = cls._root(workspace) / "ACTIVE"
        if marker.is_file():
            if not force:
                prev = marker.read_text(encoding="utf-8").strip()
                raise ValueError(
                    f"a bob run ({prev}) is already active and unproven in "
                    "this workspace — finish it (`agent-ultra bob gate`) or "
                    "have the operator abandon it explicitly (`agent-ultra "
                    "bob abandon --operator-abandon`, logged). A run cannot "
                    "be dropped by starting another.")
            # force=True is the OPERATOR escape (CLI: --operator-force; the
            # command broker classifies operator flags as dangerous, so an
            # agent-driven shell cannot auto-run them). Never silent: the
            # abandonment is durably logged.
            cls.abandon(workspace, operator=True,
                        reason="superseded via start(force=True)")
        stamp = utc_stamp()
        run_dir = cls._root(workspace) / "runs" / stamp
        n = 0
        while run_dir.exists():        # same-second collision
            n += 1
            run_dir = cls._root(workspace) / "runs" / f"{stamp}-{n}"
        run_dir.mkdir(parents=True)
        meta = {
            "run_id": run_dir.name, "goal": goal, "started_utc": utc_now(),
            "scope": scope, "mode": mode,
        }
        if mode == "surgical":
            meta["surgical_files"] = [_norm_scope_entry(f)
                                      for f in surgical_files]
        if operator_unbounded:
            meta["operator_unbounded"] = True
        (run_dir / "run.json").write_text(json.dumps(
            meta, indent=2, ensure_ascii=False), encoding="utf-8")
        marker.write_text(run_dir.name, encoding="utf-8")
        if operator_unbounded:
            try:
                with open(run_dir / "scope-log.jsonl", "a",
                          encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "ts": utc_now(), "operator_unbounded": True,
                        "note": "UNBOUNDED run opened via the operator "
                                "escape — no scope binding for this run",
                    }) + "\n")
            except OSError:
                pass
            print("!!! bob: OPERATOR-UNBOUNDED run — no scope binding; the "
                  "gate will not reject out-of-scope staged files for this "
                  "run (hooks/.agent-ultra stay off limits). Logged.",
                  file=sys.stderr)
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

    @classmethod
    def abandon(cls, workspace: str | Path, *, operator: bool = False,
                reason: str = "") -> "str | None":
        """OPERATOR-ONLY: release the ACTIVE marker of an unproven run,
        leaving a durable abandonment record. This is the only sanctioned
        way to drop a run without proving it — start() refuses to orphan,
        and the command broker classifies the CLI's operator flags as
        dangerous so an agent-driven shell cannot auto-run them. Returns
        the abandoned run id, or None when no run is active. Works on an
        orphaned marker too (releases it, still on the record)."""
        if not operator:
            raise ValueError(
                "abandon requires the explicit operator flag — an agent "
                "cannot abandon an unproven run")
        workspace = Path(workspace)
        marker = cls._root(workspace) / "ACTIVE"
        if not marker.is_file():
            return None
        name = marker.read_text(encoding="utf-8").strip()
        rec = {"ts": utc_now(), "run_id": name, "reason": reason,
               "operator_abandon": True}
        try:
            with open(cls._root(workspace) / "abandoned.jsonl", "a",
                      encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError:
            pass
        run_dir = cls._root(workspace) / "runs" / name
        if run_dir.is_dir():
            try:
                (run_dir / "abandoned.json").write_text(
                    json.dumps(rec, indent=2), encoding="utf-8")
            except OSError:
                pass
        marker.unlink()
        print(f"!!! bob: run {name} ABANDONED by operator (unproven; "
              "logged to .agent-ultra/bob/abandoned.jsonl)",
              file=sys.stderr)
        return name

    @property
    def run_id(self) -> str:
        return self.run_dir.name

    def _meta(self) -> dict:
        try:
            meta = json.loads((self.run_dir / "run.json").read_text(
                encoding="utf-8"))
            return meta if isinstance(meta, dict) else {}
        except (OSError, ValueError):
            return {}

    def goal(self) -> str:
        return str(self._meta().get("goal", ""))

    def scope(self) -> list:
        """The run's declared target scope. Scope is mandatory: an empty
        scope authorizes NOTHING at the gate (fail closed) unless the run
        was opened with the explicit operator_unbounded escape."""
        return list(self._meta().get("scope") or [])

    def unbounded(self) -> bool:
        """True only for a run opened via the explicit, operator-only
        operator_unbounded escape. run.json is plaintext, so this flag is
        tamper-EVIDENT (loudly logged at start), not tamper-proof — same
        trust ceiling as the rest of the run metadata."""
        return bool(self._meta().get("operator_unbounded"))

    def mode(self) -> str:
        """Run mode from run.json; absent field = full (backward compat).
        run.json is plaintext, so the mode is a REQUEST the gate verifies
        (see gate_check's C1 dispatch), never a trusted assertion."""
        return str(self._meta().get("mode") or "full")

    def surgical_files(self) -> list:
        return list(self._meta().get("surgical_files") or [])

    def add_scope(self, paths) -> "tuple[list, list]":
        """Expand the run's scope — the approved, validated, logged
        mechanism. Rejects infrastructure paths always. Appends to run.json
        and records each expansion in scope-log.jsonl. Returns
        (added, errors)."""
        meta = self._meta()
        cur = list(meta.get("scope") or [])
        surgical = str(meta.get("mode") or "full") == "surgical"
        added, errors = [], []
        for raw in paths or []:
            e = _norm_scope_entry(raw)
            if not e:
                continue
            if scope_entry_is_infra(e):
                errors.append(f"{e}: enforcement infrastructure — never a "
                              "run target")
                continue
            if surgical and (surgical_file_denied(e)
                             or not surgical_ext_allowed(e)):
                errors.append(f"{e}: not an inert/allowed type — a surgical "
                              "run cannot expand into risky paths (use the "
                              "full pipeline)")
                continue
            if e not in cur:
                cur.append(e)
                added.append(e)
        if added:
            meta["scope"] = cur
            (self.run_dir / "run.json").write_text(json.dumps(
                meta, indent=2, ensure_ascii=False), encoding="utf-8")
            try:
                with open(self.run_dir / "scope-log.jsonl", "a",
                          encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": utc_now(),
                                         "added": added}) + "\n")
            except OSError:
                pass
        return added, errors

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

def pytest_available() -> bool:
    """The RED/GREEN steps execute pytest; the kit core is dependency-free,
    so pytest may be absent on a fresh install. Callers check this to give a
    clear 'pip install pytest' message instead of a cryptic empty RED."""
    import importlib.util
    return importlib.util.find_spec("pytest") is not None


PYTEST_MISSING_MSG = ("pytest is not installed in this environment — the "
                      "RED/GREEN steps need it: pip install pytest "
                      "(or: pip install 'agent-ultra-kit[dev]')")


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
    if not pytest_available():
        # write nothing: an honest "cannot run" beats a receipt recording a
        # spawn failure as if the tests were exercised
        return False, {}, PYTEST_MISSING_MSG

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


def cloned_fanout_error(run_id: str, done_agents) -> "str | None":
    """A fan-out of N agents that all produced the SAME output is one
    review wearing N hats — the per-agent output_sha256 in the ultracode
    receipt exposes the clone. Only the all-identical case is flagged
    (>=2 agents, one distinct digest): a single coincidental pair of short
    verdicts stays legal, a copy-pasted sweep does not."""
    digests = {str(a.get("output_sha256") or "") for a in done_agents
               if isinstance(a, dict) and a.get("output_sha256")}
    if len(done_agents) >= 2 and len(digests) == 1:
        return (f"ultracode run {run_id!r}: all {len(done_agents)} agents "
                "produced IDENTICAL output — a cloned fan-out is one "
                "review, not an independent sweep")
    return None


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
    # the offline mock route answers every agent identically by design, so
    # the cloned-fanout check would always trip in demo mode; mock receipts
    # are only acceptable under --allow-mock anyway, which is the boundary
    # that already scopes what a demo may prove
    if not (receipt_mock and allow_mock):
        if (err := cloned_fanout_error(run_id, done)):
            errors.append(err)
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
        # ---- mode dispatch (C1: mode-downgrade defense) --------------------
        # run.json's mode field is a REQUEST the gate verifies, never a
        # trusted assertion. The commit is routed to the surgical gate ONLY
        # when the run asks for it AND the actual staged set independently
        # qualifies (all inert, none denied, all declared). Flipping a
        # run's mode to "surgical" with code staged therefore routes it to
        # the FULL gate — which demands the full receipt chain and blocks.
        if run.mode() == "surgical" and not run.marker_orphaned():
            routed = staged_files
            if routed is None and _inside_work_tree(Path(run.workspace)):
                routed = _staged_files(Path(run.workspace))
            if routed is not None and staged_set_qualifies(
                    routed, run.surgical_files()):
                return surgical_gate_check(run, allow_mock=allow_mock,
                                           staged_files=routed)
            # falls through: the staged set does not qualify -> FULL gate
        return _gate_check(run, rerun_tests=rerun_tests, allow_mock=allow_mock,
                           staged_files=staged_files,
                           allow_uncovered=allow_uncovered)
    except Exception as e:   # fail closed: a malformed receipt blocks, not crashes
        return GateResult(passed=False,
                          errors=[f"gate error (fail-closed): "
                                  f"{type(e).__name__}: {e}"])


def surgical_gate_check(run: BobRun, *, allow_mock: bool = False,
                        staged_files=None) -> GateResult:
    """The surgical lane's gate — fail-closed throughout, and NARROWER than
    full mode on every axis it covers: declared-file allowlist, inert types
    only, a denylist, and a small diff budget. It can never loosen the full
    gate: gate_check routes here only when the staged set independently
    qualifies; everything else gets the full 10-step demands."""
    try:
        return _surgical_gate_check(run, allow_mock=allow_mock,
                                    staged_files=staged_files)
    except Exception as e:
        return GateResult(passed=False,
                          errors=[f"surgical gate error (fail-closed): "
                                  f"{type(e).__name__}: {e}"])


def _surgical_gate_check(run: BobRun, *, allow_mock: bool,
                         staged_files) -> GateResult:
    errors: list[str] = []
    checked: dict = {}

    if run.marker_orphaned():
        return GateResult(passed=False, errors=[
            f"run {run.run_id}: ACTIVE marker names a run directory that no "
            "longer exists — the run was deleted while unproven (fail closed)"])

    declared = run.surgical_files()
    if not declared:
        errors.append("surgical: no surgical_files declared in run.json — "
                      "fail closed")
    for f in declared:
        if surgical_file_denied(f) or not surgical_ext_allowed(f):
            errors.append(f"surgical: declared file {f} is not an inert/"
                          "allowed type — use the full pipeline")

    # chain spine: same signed/hash-chained demands as full mode
    chain = run.chain()
    errors.extend(validate_chain(chain, run.key or None))
    checked["chain"] = f"{len(chain)} receipt(s), hash-chained + signed"

    have = {r.get("step") for r in chain}
    for step in SURGICAL_GATED_STEPS:
        if step not in have:
            errors.append(f"{step}: receipt missing — the step never ran "
                          "(or its receipt was deleted)")

    def _ev(rec) -> dict:
        ev = rec.get("evidence") if isinstance(rec, dict) else None
        return ev if isinstance(ev, dict) else {}

    # ---- review: cross-checked against the ultracode run it names --------
    review = run.load("step_surgical_review")
    if review is not None:
        ev = _ev(review)
        rerrs = check_ultracode_evidence(
            ev, allow_mock=allow_mock, receipt_mock=bool(review.get("mock")),
            expect_workflow="bob-surgical-review")
        errors.extend(f"step_surgical_review: {e}" for e in rerrs)
        checked["step_surgical_review"] = ("ultracode run verified"
                                           if not rerrs else "INVALID")

    # ---- quiz: passed requires the captured operator response ------------
    quiz = run.load("step_surgical_quiz")
    if quiz is not None:
        ev = _ev(quiz)
        if (e := _mock_flag_error(quiz, "step_surgical_quiz", allow_mock)):
            errors.append(e)
        outcome = ev.get("outcome")
        if outcome not in ("passed", "skipped"):
            errors.append(f"step_surgical_quiz: outcome {outcome!r} is not "
                          "passed/skipped")
        if not ev.get("questions"):
            errors.append("step_surgical_quiz: no comprehension questions "
                          "recorded")
        if outcome == "passed" and not str(
                ev.get("operator_response", "") or "").strip():
            errors.append("step_surgical_quiz: outcome 'passed' without a "
                          "captured operator response — a quiz nobody "
                          "answered (self-graded quizzes do not count)")
        checked["step_surgical_quiz"] = f"outcome={outcome}"

    # ---- staleness: covered files must match disk -------------------------
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
                          "receipt — re-run the review")
    checked["staleness"] = f"{len(coverage)} file(s) covered"

    # ---- staged set: declared + inert only, fail-closed --------------------
    if staged_files is None:
        if _inside_work_tree(Path(run.workspace)):
            staged_files = _staged_files(Path(run.workspace))
            if staged_files is None:
                errors.append("surgical: could not read the staged list "
                              "(git error) — fail closed")
                staged_files = []
        else:
            staged_files = []
    declared_norm = {os.path.normpath(str(d)).replace("\\", "/").lower()
                     for d in declared}
    for rel in staged_files:
        norm = os.path.normpath(str(rel)).replace("\\", "/").lower()
        if norm.startswith(".agent-ultra/"):
            continue                     # run machinery rides along
        if surgical_file_denied(rel):
            errors.append(f"surgical: staged denied_path {rel} — this file "
                          "executes; use the full pipeline")
        elif not surgical_ext_allowed(rel):
            errors.append(f"surgical: staged {rel} is not an inert type — "
                          "the surgical lane is docs/config only")
        elif norm not in declared_norm:
            errors.append(f"surgical: staged {rel} was not declared in "
                          "surgical_files")

    # ---- diff budget: small edits only, fail-closed -------------------------
    if _inside_work_tree(Path(run.workspace)):
        if (berr := diff_budget_error(Path(run.workspace))):
            errors.append(berr)
        checked["diff_budget"] = "within budget" if not any(
            "budget" in e or "BINARY" in e for e in errors) else "EXCEEDED"

    return GateResult(passed=not errors, errors=errors, checked=checked)


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
    if rerun_tests and green is not None and test_file and not pytest_available():
        errors.append(f"live re-run: {PYTEST_MISSING_MSG}")
    elif rerun_tests and green is not None and test_file:
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

    # ---- staged files must be WITHIN the run's declared scope ------------------
    # A run opened for module A must not authorize commits touching module B.
    # Scope is mandatory: no scope + not the operator escape = every staged
    # file blocks (fail closed) and the missing declaration is itself an
    # error. Bob's own machinery (.agent-ultra receipts) is always exempt.
    scope = run.scope()
    if run.unbounded():
        checked["scope"] = "OPERATOR-UNBOUNDED (no scope binding — logged)"
    else:
        if not scope:
            errors.append(
                "scope: run declares no target scope — scope is mandatory "
                "and a no-scope run authorizes nothing (fail closed)")
        for rel in staged_files:
            if rel.startswith(".agent-ultra/") or rel == ".gitignore":
                continue
            if scope and scope_contains(scope, rel):
                continue
            errors.append(
                f"out-of-scope: staged file {rel} is outside the run's "
                "declared scope — if this edit is really part of the run, "
                "expand the scope explicitly (`agent-ultra bob scope-add`)")
        checked["scope"] = (f"{len(scope)} declared entr"
                            + ("y" if len(scope) == 1 else "ies"))

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
