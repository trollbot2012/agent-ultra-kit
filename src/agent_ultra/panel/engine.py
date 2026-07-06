"""Adversarial panel engine — agent-agnostic.

A hard question fans out across bounded critic agents; a judge grades the
debate; proof/checks dispose. THE CLEAN RULE — panel agents are ROLES, not
models:

  panel agent = a bounded reasoning lens (one prompt-scoped critic call)
  route       = the model backend the agent happens to run on

One healthy model runs a 4-, 10-, or 16-lens panel by being called once per
lens with a different role. Value comes from diverse PERSPECTIVES, not diverse
providers. Routing modes:

  single (default)  one healthy route runs every agent (critics + judge).
  mixed (optional)  critics spread across healthy routes; a claim's steelman
                    prefers a different backend than its accuser. Collapses to
                    single automatically when only one route is healthy.

Phase protocol (every prompt LEADS with its marker):
  PROBE, CHALLENGE, STEELMAN, CROSS-EXAM, SYNTHESIS.

Verdicts: real_now | real_later | theoretical | wrong (plus an internal
UNJUDGED state when a cross-exam call fails — never silently 'theoretical').
Proof gates derive ONLY from accepted (real_now/real_later) claims' `check`
fields, never from anything synthesis proposes, and destructive checks are
split out before an operator ever sees them.

Stdlib only. Bring your own ChatClient (OpenAIChatClient, MockChatClient, or
anything with .complete(model, prompt, max_tokens) -> str).
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..routes.pool import RoutePool
from ..routes.client import RouteError
from ..evidence.reader import gather as gather_evidence, is_low_context
from ..memory.hooks import safe_call
from .json_extract import extract_json

# --------------------------------------------------------------------------
# Lens library + budgets
# --------------------------------------------------------------------------

DEFAULT_LENSES = (
    "correctness", "security", "failure-modes", "simplicity", "performance",
    "operator-experience", "data-integrity", "maintainability", "cost",
    "concurrency", "testability", "migration-risk", "observability",
    "compatibility", "recovery", "abuse-resistance",
)

# (min, max, default_count) per size budget.
SIZE_BUDGET = {
    "small": (3, 5, 4),
    "medium": (8, 12, 10),
    "large": (13, 999, 16),
}

VALID_VERDICTS = ("real_now", "real_later", "theoretical", "wrong")
ACCEPTED_VERDICTS = ("real_now", "real_later")

DEFAULTS = {
    "call_timeout": 120,
    "max_workers": 6,
    "max_claims_per_lens": 2,
    "context_max_chars": 8000,
    "prompt_max_chars": 24000,
    "low_context_threshold": 500,
    "max_tokens_challenge": 4000,
    "max_tokens_steelman": 2000,
    "max_tokens_cross_exam": 2000,
    "max_tokens_synthesis": 4000,
    "routing_mode": "single",
}


class PanelError(Exception):
    """A panel run could not produce findings (bad args or all routes dead)."""


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Finding:
    lens: str
    claim: str
    severity: str = ""
    severity_critic: str = ""
    anchor: str = ""              # verbatim quote it attacks, or 'assumption'
    check: str = ""
    origin_route: str = ""
    steelman: str = ""
    steelman_route: str = ""
    verdict: str = "theoretical"
    reasoning: str = ""
    confidence: str = ""
    unjudged: bool = False        # route failure, not a real verdict

    @property
    def accepted(self) -> bool:
        return (not self.unjudged) and self.verdict in ACCEPTED_VERDICTS


@dataclass
class PanelReport:
    question: str
    size: str
    lenses: list
    routes: dict
    findings: list
    synthesis: str = ""
    decision: str = ""
    disagreements: list = field(default_factory=list)
    proof_gates: list = field(default_factory=list)
    destructive_gates: list = field(default_factory=list)
    low_context: bool = False
    run_id: str = ""
    model_calls: int = 0
    duration_s: float = 0.0
    started_utc: str = ""
    errors: list = field(default_factory=list)

    @property
    def accepted(self) -> list:
        return [f for f in self.findings if f.accepted]

    @property
    def not_judged(self) -> list:
        return [f for f in self.findings if f.unjudged]

    def verdict_counts(self) -> dict:
        counts = {v: 0 for v in VALID_VERDICTS}
        for f in self.findings:
            if f.unjudged:
                continue
            counts[f.verdict] = counts.get(f.verdict, 0) + 1
        counts["unjudged"] = len(self.not_judged)
        return counts


# --------------------------------------------------------------------------
# Claim dedup (deterministic, pre-steelman)
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")


def _similarity(a: str, b: str) -> float:
    ta = set(_WORD_RE.findall((a or "").lower()))
    tb = set(_WORD_RE.findall((b or "").lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def dedupe_findings(findings: list, threshold: float = 0.7) -> list:
    kept: list = []
    for f in findings:
        for k in kept:
            if _similarity(f.claim, k.claim) >= threshold:
                if f.lens not in k.lens.split("+"):
                    k.lens = f"{k.lens}+{f.lens}"
                if f.check and not k.check:
                    k.check = f.check
                break
        else:
            kept.append(f)
    return kept


def _truncate_middle(text: str, limit: int,
                     marker: str = " ...[truncated]... ") -> str:
    text = text or ""
    if len(text) <= limit or limit <= len(marker):
        return text[:limit]
    keep = limit - len(marker)
    head = keep - keep // 2
    return text[:head] + marker + text[len(text) - keep // 2:]


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------

class PanelEngine:
    def __init__(self, pool: RoutePool, config: dict | None = None,
                 memory=None, judge_route: str = ""):
        self.pool = pool
        self.cfg = dict(DEFAULTS)
        if config:
            self.cfg.update({k: v for k, v in config.items() if k in DEFAULTS})
        self.memory = memory
        self._judge_pref = judge_route
        self._calls = 0
        self._calls_lock = threading.Lock()
        self._errors: list = []

    # -- public ------------------------------------------------------------

    def run(self, question: str, size: str = "small", lenses=None,
            context: str = "", evidence_dirs=None, evidence_paths=None,
            allow_large: bool = False, mode: str | None = None) -> PanelReport:
        question = (question or "").strip()
        if not question:
            raise PanelError("no question given")
        if size not in SIZE_BUDGET:
            raise PanelError(f"unknown panel size {size!r} (small/medium/large)")
        if size == "large" and not allow_large:
            raise PanelError("large panels (13+ lenses) need allow_large=True")
        mode = (mode or self.cfg["routing_mode"] or "single").lower()
        if mode not in ("single", "mixed"):
            mode = "single"

        self._calls = 0
        self._errors = []
        t0 = time.time()
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id = f"panel-{uuid.uuid4().hex[:10]}"

        # -- route health: agents are roles; pick backends by live probe -----
        healthy = self.pool.probe_all()
        if not healthy:
            raise PanelError(
                "all model routes unhealthy — panel cannot run. "
                f"Health: {self.pool.health_report()}")
        mode_effective = "mixed" if (mode == "mixed" and len(healthy) > 1) else "single"
        judge_route = self._pick_judge(healthy)

        # -- evidence: fold gathered source into the context -----------------
        ctx = context or ""
        if evidence_dirs or evidence_paths:
            ev = gather_evidence(paths=evidence_paths or (),
                                 dirs=evidence_dirs or (),
                                 max_total_chars=self.cfg["context_max_chars"])
            if ev.text:
                ctx = (ctx + "\n\n" + ev.text) if ctx else ev.text
        ctx = ctx[: self.cfg["context_max_chars"]]
        low_context = is_low_context(context, self.cfg["low_context_threshold"]) \
            and is_low_context(ctx, self.cfg["low_context_threshold"])

        chosen = self._select_lenses(size, lenses)

        # -- phases ----------------------------------------------------------
        findings = self._challenge(question, ctx, chosen)
        if not findings:
            raise PanelError(
                f"all critic agents failed — no findings "
                f"({len(self._errors)} route errors): {self._errors[:3]}")
        findings = dedupe_findings(findings)
        self._steelman(question, ctx, findings, judge_route, mode_effective)
        self._cross_exam(question, ctx, findings, judge_route)
        synthesis, decision, disagreements = self._synthesis(
            question, findings, judge_route)

        if not decision:
            acc = [f for f in findings if f.accepted]
            decision = (f"Address {len(acc)} accepted concern(s) "
                        f"({', '.join(f.lens for f in acc)}) before proceeding."
                        if acc else
                        "No real-now/real-later concerns found; proceed with "
                        "theoretical risks noted.")

        proof_gates, destructive_gates = self._proof_gates(findings)

        report = PanelReport(
            question=question, size=size, lenses=chosen,
            routes={"mode_requested": mode, "mode_effective": mode_effective,
                    "judge": judge_route, "healthy": healthy,
                    "health": self.pool.health_report()},
            findings=findings, synthesis=synthesis, decision=decision,
            disagreements=disagreements, proof_gates=proof_gates,
            destructive_gates=destructive_gates, low_context=low_context,
            run_id=run_id, model_calls=self._calls,
            duration_s=round(time.time() - t0, 2), started_utc=started,
            errors=list(self._errors))

        safe_call(self.memory, "on_panel_decision", {
            "run_id": run_id, "question": question, "decision": decision,
            "accepted": len(report.accepted), "low_context": low_context})
        for f in report.accepted:
            safe_call(self.memory, "on_finding_accepted",
                      {"lens": f.lens, "claim": f.claim, "verdict": f.verdict,
                       "severity": f.severity})
        return report

    # -- helpers -----------------------------------------------------------

    def _bump(self) -> None:
        with self._calls_lock:
            self._calls += 1

    def _pick_judge(self, healthy: list) -> str:
        if self._judge_pref and self._judge_pref in healthy:
            return self._judge_pref
        return healthy[0]

    def _select_lenses(self, size: str, lenses) -> list:
        if lenses:
            return list(lenses)
        _, _, count = SIZE_BUDGET[size]
        return list(DEFAULT_LENSES[:count])

    def _chat(self, candidates: list, prompt: str, max_tokens: int,
              phase: str, label: str) -> tuple[str, str]:
        """Try each candidate route; mark dead on failure; one reserve revive.
        Returns (content, route). Raises RouteError if every route fails."""
        prompt = _truncate_middle(prompt, self.cfg["prompt_max_chars"])
        last = None
        for route in candidates:
            if self.pool.is_dead(route):
                continue
            self._bump()
            try:
                return self.pool.chat(route, prompt, max_tokens), route
            except RouteError as e:
                self.pool.mark_dead(route)
                last = e
                self._errors.append({"phase": phase, "label": label,
                                     "route": route, "error": str(e)[:200]})
        # one revive attempt across dead routes
        for route in self.pool.dead():
            if self.pool.revive(route):
                self._bump()
                try:
                    return self.pool.chat(route, prompt, max_tokens), route
                except RouteError as e:
                    self.pool.mark_dead(route)
                    last = e
        raise RouteError(str(last) if last else "no routes to try")

    def _challenge(self, question: str, ctx: str, lenses: list) -> list:
        low = is_low_context(ctx, self.cfg["low_context_threshold"])
        banner = ("\nNOTE: little/no source context was provided — attack only "
                  "what is stated; do NOT invent code that is not shown.\n"
                  if low else "")

        def one(index_lens):
            index, lens = index_lens
            candidates = self.pool.assign(index)
            prompt = (
                f"CHALLENGE: You are the '{lens}' critic on an adversarial "
                f"review panel. Question under review:\n{question}\n\n"
                f"Context / source:\n{ctx or '(none provided)'}\n{banner}\n"
                f"Find at most {self.cfg['max_claims_per_lens']} concrete ways "
                f"this is wrong or unsafe THROUGH THE '{lens}' LENS. For each, "
                "return an object with keys: claim (specific, falsifiable), "
                "severity (critical|high|medium|low), anchor (a verbatim quote "
                "from the question/context you attack, or the literal string "
                "'assumption'), check (a READ-ONLY shell command that would "
                "prove or disprove it, or ''). Reply with ONLY a JSON array.")
            try:
                content, route = self._chat(candidates, prompt,
                                            self.cfg["max_tokens_challenge"],
                                            "challenge", lens)
            except RouteError:
                return []
            data = extract_json(content)
            out = []
            if isinstance(data, list):
                for item in data[: self.cfg["max_claims_per_lens"]]:
                    if not isinstance(item, dict) or not item.get("claim"):
                        continue
                    out.append(Finding(
                        lens=lens, claim=str(item.get("claim", "")).strip(),
                        severity=str(item.get("severity", "")).strip().lower(),
                        severity_critic=str(item.get("severity", "")).strip().lower(),
                        anchor=str(item.get("anchor", "")).strip(),
                        check=str(item.get("check", "")).strip(),
                        origin_route=route))
            return out

        results: list = []
        workers = max(1, min(self.cfg["max_workers"], len(lenses)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for got in ex.map(one, enumerate(lenses)):
                results.extend(got)
        return results

    def _steelman(self, question, ctx, findings, judge_route, mode) -> None:
        def one(index_finding):
            index, f = index_finding
            avoid = f.origin_route if mode == "mixed" else ""
            candidates = self.pool.assign(index, avoid=avoid)
            prompt = (
                f"STEELMAN: A '{f.lens}' critic claims:\n{f.claim}\n\n"
                f"Question:\n{question}\n\nContext:\n{ctx or '(none)'}\n\n"
                "Give the STRONGEST good-faith defense of the design against "
                "this claim — the best reason the claim is wrong, mitigated, or "
                "not real in practice. 2-4 sentences, prose only.")
            try:
                content, route = self._chat(candidates, prompt,
                                            self.cfg["max_tokens_steelman"],
                                            "steelman", f.lens)
                f.steelman = content.strip()
                f.steelman_route = route
            except RouteError:
                f.steelman = ""

        workers = max(1, min(self.cfg["max_workers"], len(findings) or 1))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, enumerate(findings)))

    def _cross_exam(self, question, ctx, findings, judge_route) -> None:
        candidates = [judge_route] + [r for r in self.pool.alive()
                                      if r != judge_route]

        def one(f):
            prompt = (
                f"CROSS-EXAM: You are the impartial judge. A '{f.lens}' critic "
                f"claims:\n{f.claim}\n\nThe steelman defense:\n"
                f"{f.steelman or '(none offered)'}\n\nQuestion:\n{question}\n\n"
                f"Context:\n{ctx or '(none)'}\n\n"
                "Rule on the claim. Return ONLY a JSON object with keys: "
                "verdict (real_now = a real problem triggerable now; "
                "real_later = real but needs a future condition; theoretical = "
                "plausible but no concrete path shown; wrong = the steelman "
                "refutes it), severity (critical|high|medium|low), confidence "
                "(high|low), reasoning (one sentence).")
            try:
                content, _ = self._chat(candidates, prompt,
                                        self.cfg["max_tokens_cross_exam"],
                                        "cross-exam", f.lens)
            except RouteError:
                f.unjudged = True
                return
            data = extract_json(content)
            if not isinstance(data, dict):
                f.unjudged = True
                return
            verdict = str(data.get("verdict", "")).strip().lower()
            f.verdict = verdict if verdict in VALID_VERDICTS else "theoretical"
            sev = str(data.get("severity", "")).strip().lower()
            if sev:
                f.severity = sev  # judge re-grades; critics don't grade own work
            f.confidence = str(data.get("confidence", "")).strip().lower()
            f.reasoning = str(data.get("reasoning", "")).strip()

        workers = max(1, min(self.cfg["max_workers"], len(findings) or 1))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, findings))

    def _synthesis(self, question, findings, judge_route):
        candidates = [judge_route] + [r for r in self.pool.alive()
                                      if r != judge_route]
        judged = [f for f in findings if not f.unjudged]
        lines = "\n".join(
            f"- [{f.verdict}/{f.severity}] ({f.lens}) {f.claim}"
            for f in judged) or "(no judged findings)"
        prompt = (
            f"SYNTHESIS: You are the judge. Question:\n{question}\n\n"
            f"Adjudicated findings:\n{lines}\n\n"
            "Summarize what the panel concluded and give a clear decision. "
            "Return ONLY a JSON object with keys: synthesis (2-4 sentences), "
            "decision (one actionable sentence), disagreements (array of "
            "strings, may be empty).")
        try:
            content, _ = self._chat(candidates, prompt,
                                    self.cfg["max_tokens_synthesis"],
                                    "synthesis", "judge")
        except RouteError:
            return "", "", []
        data = extract_json(content)
        if not isinstance(data, dict):
            return content.strip()[:2000], "", []
        return (str(data.get("synthesis", "")).strip(),
                str(data.get("decision", "")).strip(),
                [str(d) for d in (data.get("disagreements") or [])
                 if isinstance(d, str)])

    def _proof_gates(self, findings) -> tuple[list, list]:
        from ..broker.broker import classify, DANGEROUS
        proof, destructive, seen = [], [], set()
        for f in findings:
            chk = (f.check or "").strip()
            if not (f.accepted and chk) or chk in seen:
                continue
            seen.add(chk)
            if classify(chk)[0] == DANGEROUS:
                destructive.append(chk)
            else:
                proof.append(chk)
        return proof, destructive
