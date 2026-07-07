"""ULTRA loop — tests prove the KNOWN contract; the adversarial panel finds
the UNKNOWN failure modes. Green tests alone are not enough for risky code.

  build -> test -> panel -> classify -> fix -> re-test -> re-panel -> ship-gate

  1. BUILD    (optional) a coding agent implements the task.
  2. TEST     run tests; record cmd + exit + output + changed files (proof).
  3. PANEL    adversarial review, only when risk-worthy, always with real
              source context (low-context panels are refused).
  4. CLASSIFY keep the panel's real_now/real_later verdicts after steelman +
              cross-exam (that stage kills weak findings — it is load-bearing).
  5. FIX      accepted critical/high real_now findings become proof-gated fix
              tasks; the fixer applies them; tests rerun.
  6. RE-PANEL a smaller panel confirms the fix.
  7. SHIP     only when tests pass AND no critical/high real_now findings
              remain AND artifacts exist; residual risks recorded explicitly.

The builder, test runner, and fixer are INJECTABLE seams. Defaults: the test
runner shells out through a CommandBroker; there is no default builder or
fixer (bring your agent's). Stdlib only.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from ..panel.engine import PanelEngine, SIZE_BUDGET
from ..artifacts.records import RunRecord, write_run, slug
from ..proof.gates import GateSet
from ..evidence.reader import gather as gather_evidence
from ..memory.hooks import safe_call

RISK_TO_SIZE = {"small": "small", "medium": "medium", "high": "medium"}
RISK_TO_SIZE_LARGE = {"small": "small", "medium": "medium", "high": "large"}
REPANEL_SIZE = "small"

SENSITIVE_KEYWORDS = (
    "auth", "login", "password", "token", "secret", "credential", "session",
    "cookie", "jwt", "oauth", "sql", "query", "database", "persist", "migrat",
    "concurren", "thread", "lock", "race", "async", "network", "api", "http",
    "socket", "exec", "subprocess", "command", "shell", "eval", "deserial",
    "pickle", "crypto", "encrypt", "decrypt", "hash", "permission", "authoriz",
    "upload", "download", "injection", "sanitiz", "validat", "csrf", "xss",
    "ssrf", "payment", "billing",
)


class UltraError(Exception):
    pass


@dataclass
class TestResult:
    __test__ = False  # not a pytest test class
    cmd: str
    exit_code: int | None
    passed: bool
    output: str
    duration_s: float


@dataclass
class FixTask:
    id: str
    lens: str
    severity: str
    verdict: str
    claim: str
    check: str = ""
    status: str = "open"     # open | fixed | fix_failed | skipped


@dataclass
class UltraReport:
    task: str
    workspace: str
    risk: str
    started_utc: str
    tests_before: dict = field(default_factory=dict)
    panel_ran: bool = False
    panel1: dict = field(default_factory=dict)
    fix_tasks: list = field(default_factory=list)
    tests_after: dict = field(default_factory=dict)
    panel2: dict = field(default_factory=dict)
    residual_risks: list = field(default_factory=list)
    shipped: bool = False
    ship_reason: str = ""
    artifact_dir: str = ""
    # structural PANEL enforcement: a self-review cannot satisfy PANEL
    receipt_path: str = ""
    artifact_hash: str = ""
    panel_enforced: bool = False
    duration_s: float = 0.0


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_test_runner(broker):
    """A test runner backed by a CommandBroker (tests are ELEVATED — they
    auto-run in trusted-owner mode)."""
    def run(workspace: Path, cmd: str, timeout: int) -> TestResult:
        if not cmd:
            return TestResult("", 0, True, "(no test command given)", 0.0)
        t0 = time.time()
        res = broker.run(cmd, cwd=str(workspace), reason="ULTRA test phase",
                         expected_effect="proves the known contract")
        return TestResult(cmd=cmd, exit_code=res.exit_code,
                          passed=(res.status == "passed"), output=res.output,
                          duration_s=round(time.time() - t0, 2))
    return run


class UltraLoop:
    def __init__(self, workspace, panel: PanelEngine, broker,
                 home=None, test_runner=None, fixer=None, builder=None,
                 memory=None, config: dict | None = None):
        self.workspace = Path(workspace).resolve()
        self.panel = panel
        self.broker = broker
        self.home = Path(home) if home else (self.workspace / ".ultra")
        self._test_runner = test_runner or default_test_runner(broker)
        self._fixer = fixer          # callable(fix_task, workspace) -> bool
        self._builder = builder      # callable(workspace, task) -> str
        self._memory = memory
        self.cfg = {"test_timeout": 600, "min_context_chars": 200,
                    "context_max_chars": 24000, "gather_max_files": 40}
        if config:
            self.cfg.update(config)
        self._receipt_path = ""   # set by _write_panel_receipt (enforcement)

    def run(self, task: str, risk: str = "medium", context: str = "",
            evidence_dirs=None, test_cmd: str | None = None, lenses=None,
            do_fix: bool = True, allow_large: bool = False,
            force_panel: bool | None = None) -> UltraReport:
        task = (task or "").strip()
        if not task:
            raise UltraError("no task given")
        if risk not in RISK_TO_SIZE:
            raise UltraError(f"unknown risk {risk!r} (small/medium/high)")
        t0 = time.time()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        art_dir = self.home / "runs" / f"{stamp}-{slug(task)}"
        art_dir.mkdir(parents=True, exist_ok=True)
        rep = UltraReport(task=task, workspace=str(self.workspace), risk=risk,
                          started_utc=_now_utc(), artifact_dir=str(art_dir))

        # 1. BUILD (optional)
        if self._builder is not None:
            try:
                self._builder(self.workspace, task)
            except Exception:
                pass

        # 2. TEST
        tb = self._test_runner(self.workspace, test_cmd, self.cfg["test_timeout"])
        rep.tests_before = asdict(tb)
        if not tb.passed:
            rep.shipped = False
            rep.ship_reason = ("tests are RED — fix the known contract before "
                               "the adversarial panel runs")
            return self._finish(rep, art_dir, t0)

        # 3. PANEL (only when risk-worthy; always with real context)
        want = self._should_panel(risk, task, context) if force_panel is None else force_panel
        if not want:
            rep.shipped = True
            rep.ship_reason = ("tests pass; task not panel-worthy — shipped on "
                               "tests alone")
            return self._finish(rep, art_dir, t0)

        ctx = context.strip() or self._gather(evidence_dirs)
        if len(ctx) < self.cfg["min_context_chars"]:
            rep.shipped = False
            rep.ship_reason = ("REFUSED low-context panel — pass context/"
                               "evidence_dirs or run inside the repo")
            return self._finish(rep, art_dir, t0)

        ev_dirs = [str(self.workspace)] + [str(d) for d in (evidence_dirs or [])]
        size = (RISK_TO_SIZE_LARGE if allow_large else RISK_TO_SIZE)[risk]
        p1 = self._run_panel(task, ctx, size, lenses, ev_dirs, allow_large)
        rep.panel_ran = True
        rep.panel1 = self._panel_summary(p1)
        # STRUCTURAL PANEL ENFORCEMENT: write an execution receipt from the REAL
        # PanelReport (model_calls, lenses, per-finding origins). A self-review
        # cannot forge this — it never produces model_calls > 0. The receipt
        # gates REPORT (Level 1 in _finish; gate_report() for callers/CLI).
        rep.artifact_hash = self._write_panel_receipt(p1, task, ctx, art_dir)
        blocking = self._blocking(p1)

        # 4/5. CLASSIFY -> FIX
        fix_tasks = [FixTask(id=f"ULTRA-{i+1}", lens=f.lens, severity=f.severity,
                             verdict=f.verdict, claim=f.claim,
                             check=(f.check or "").strip())
                     for i, f in enumerate(blocking)]
        ta = tb
        if do_fix and fix_tasks and self._fixer is not None:
            for ft in fix_tasks:
                try:
                    ft.status = "fixed" if self._fixer(ft, self.workspace) else "fix_failed"
                except Exception:
                    ft.status = "fix_failed"
            ta = self._test_runner(self.workspace, test_cmd, self.cfg["test_timeout"])
            rep.tests_after = asdict(ta)
        rep.fix_tasks = [asdict(t) for t in fix_tasks]

        # 6. RE-PANEL (small) if code changed
        remaining = blocking
        if do_fix and any(t.status == "fixed" for t in fix_tasks):
            ctx2 = self._gather(evidence_dirs) or ctx
            p2 = self._run_panel(task, ctx2, REPANEL_SIZE, lenses, ev_dirs, False)
            rep.panel2 = self._panel_summary(p2)
            remaining = self._blocking(p2)

        # 7. SHIP GATE — enforced through proof gates
        gs = GateSet()
        gs.add("tests pass after fixes", gate_id="TESTS")
        if ta.passed:
            from ..proof.gates import Evidence
            gs.get("TESTS").satisfy(Evidence(kind="test_report", ref=ta.cmd,
                                             summary=f"exit {ta.exit_code}"))
        for i, f in enumerate(remaining):
            gs.add(f"resolve blocking finding: [{f.lens}] {f.claim[:100]}",
                   gate_id=f"BLOCK-{i+1}")
        rep.residual_risks = [
            {"lens": f.lens, "severity": f.severity, "verdict": f.verdict,
             "claim": f.claim} for f in self._accepted(p1)
            if not (f.verdict == "real_now" and f.severity in ("high", "critical"))]
        rep.shipped = gs.all_satisfied
        rep.ship_reason = ("SHIP: tests pass and no critical/high real_now "
                           "findings remain" if rep.shipped else
                           ("HOLD: tests failing after fixes" if not ta.passed
                            else f"HOLD: {len(remaining)} blocking finding(s)"))
        gs.save(art_dir / "proof_gates.json")
        return self._finish(rep, art_dir, t0)

    # -- helpers -----------------------------------------------------------

    def _should_panel(self, risk, task, context) -> bool:
        if risk in ("medium", "high"):
            return True
        blob = (task + " " + (context or "")).lower()
        return any(k in blob for k in SENSITIVE_KEYWORDS)

    def _gather(self, evidence_dirs) -> str:
        dirs = [str(self.workspace)] + [str(d) for d in (evidence_dirs or [])]
        ev = gather_evidence(dirs=dirs,
                             max_files=self.cfg["gather_max_files"],
                             max_total_chars=self.cfg["context_max_chars"])
        return ev.text

    def _write_panel_receipt(self, report, task, ctx, art_dir) -> str:
        """Build + write the panel execution receipt from the REAL report.
        Returns the artifact_hash (sha256 of the reviewed input). Never raises."""
        try:
            from ..panel_receipt import build_receipt, write_receipt
            receipt = build_receipt(
                report, task_id=slug(task), reviewed_input=ctx or "",
                panel_artifact_path=str(Path(art_dir)))
            write_receipt(art_dir, receipt)
            self._receipt_path = str(Path(art_dir) / "panel_execution_receipt.json")
            return receipt.get("artifact_hash", "")
        except Exception:
            self._receipt_path = ""
            return ""

    def _run_panel(self, task, ctx, size, lenses, ev_dirs, allow_large):
        question = (
            f"Adversarial production-safety review. Task implemented: {task}. "
            "The source and tests are in the context. Find what breaks this "
            "under real-world / adversarial / concurrent / persistent "
            "conditions that the tests do NOT cover.")
        return self.panel.run(question, size=size, lenses=lenses, context=ctx,
                              evidence_dirs=ev_dirs, allow_large=allow_large)

    def _accepted(self, report):
        return [f for f in report.findings if f.accepted]

    def _blocking(self, report):
        return [f for f in report.findings
                if f.accepted and f.verdict == "real_now"
                and f.severity in ("high", "critical")]

    def _panel_summary(self, report) -> dict:
        return {"decision": report.decision, "counts": report.verdict_counts(),
                "accepted": len(report.accepted),
                "proof_gates": report.proof_gates,
                "destructive_gates": report.destructive_gates,
                "run_id": report.run_id}

    def _finish(self, rep: UltraReport, art_dir: Path, t0: float) -> UltraReport:
        rep.duration_s = round(time.time() - t0, 2)
        # LEVEL 1 PANEL ENFORCEMENT — the transition into REPORT. When a panel
        # was claimed (panel_ran), a valid execution receipt with real executed
        # lenses MUST exist. A self-review labelled PANEL cannot pass: it
        # produces no receipt with lens_count_executed > 0. On failure the run
        # cannot ship and REPORT records the exact failure message.
        if rep.panel_ran:
            rep.receipt_path = getattr(self, "_receipt_path", "") or str(
                art_dir / "panel_execution_receipt.json")
            try:
                from ..panel_receipt import validate_receipt
                ok, msg = validate_receipt(
                    rep.receipt_path, expect_task_id=slug(rep.task),
                    expect_artifact_hash=rep.artifact_hash or "")
            except Exception as e:
                ok, msg = False, ("REPORT blocked: missing or invalid panel "
                                  f"execution receipt. ({e})")
            rep.panel_enforced = ok
            if not ok:
                rep.shipped = False
                rep.ship_reason = msg
        rec = RunRecord(kind="ultra", question=rep.task, run_id=f"ultra-{slug(rep.task)}",
                        started_utc=rep.started_utc, duration_s=rep.duration_s,
                        decision=rep.ship_reason,
                        outputs={"shipped": rep.shipped,
                                 "panel1": rep.panel1, "panel2": rep.panel2},
                        extra={"risk": rep.risk, "fix_tasks": rep.fix_tasks,
                               "residual_risks": rep.residual_risks})
        write_run(rec, art_dir)
        safe_call(self._memory, "on_task_complete",
                  {"task": rep.task, "shipped": rep.shipped,
                   "reason": rep.ship_reason})
        return rep
