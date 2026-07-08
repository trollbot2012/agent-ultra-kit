"""verify_claim — the refute-first entry point, plus the budgeted Verifier.

``verify_claim`` is a pure function over injected collaborators; ``Verifier``
wraps it with an escalation budget and (optionally) a receipts bus to emit a
``kind=verifier`` receipt.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable

from .budget import EscalationBudget, BudgetExceeded

# The refute-first instruction handed to an injected judge. It is deliberately
# biased toward refutation: absence of evidence is treated as refutation.
REFUTE_FIRST_PROMPT = (
    "You are a REFUTING verifier. Your default answer is REFUTED. "
    "Only return refuted=false if the provided evidence CONCLUSIVELY supports "
    "the claim. If evidence is missing, ambiguous, or merely plausible, return "
    "refuted=true. Respond with refuted (bool), confidence (0..1), and a short "
    "reason.\n\nCLAIM:\n{claim}\n\nEVIDENCE:\n{evidence}\n"
)


def claim_sha256(claim_text: str) -> str:
    """Stable hash of the claim text — the binding key for verifier receipts."""
    return hashlib.sha256((claim_text or "").encode("utf-8")).hexdigest()


# injected collaborator signatures:
#   run_check(cmd, cwd) -> {"exit_code": int, "ledger_ref": str}
#   judge(prompt)       -> {"refuted": bool, "confidence": float, "reason": str}
#   emit_receipt(env_fields: dict) -> receipt_id
RunCheck = Callable[[str, str], dict]
Judge = Callable[[str], dict]
EmitReceipt = Callable[[dict], str]


@dataclass
class VerifierResult:
    refuted: bool
    confidence: float
    reason: str
    checks_run: list = field(default_factory=list)
    claim_sha256: str = ""
    receipt_id: str = ""

    def as_dict(self) -> dict:
        d = {"refuted": self.refuted, "confidence": self.confidence,
             "reason": self.reason, "checks_run": self.checks_run,
             "claim_sha256": self.claim_sha256}
        if self.receipt_id:
            d["receipt_id"] = self.receipt_id
        return d


def verify_claim(
    claim_text: str,
    *,
    workspace: str = "",
    session_id: str = "",
    refs: list | None = None,
    declared_verify_cmd: str = "",
    run_check: RunCheck | None = None,
    judge: Judge | None = None,
    emit_receipt: EmitReceipt | None = None,
    actor: str = "reviewer",
) -> dict:
    """Refute-first verification of ``claim_text``.

    Channels, in priority order:
      1. engine re-check — ``declared_verify_cmd`` + ``run_check``: exit 0
         confirms (refuted=False), non-zero refutes. No LLM.
      2. LLM refute — no command: ``judge`` with the refute-first prompt.
      3. neither — refuted (honest default).

    Emits a ``kind=verifier`` receipt (if ``emit_receipt`` given) stamped with
    ``claim_sha256``; a REFUTED verdict maps to a non-acceptable receipt
    verdict (``failed``) so it can never satisfy a gate.
    """
    csha = claim_sha256(claim_text)
    checks_run: list = []

    if declared_verify_cmd and run_check is not None:
        res = run_check(declared_verify_cmd, workspace)
        exit_code = int(res.get("exit_code", 1))
        checks_run.append({"cmd": declared_verify_cmd,
                           "exit_code": exit_code,
                           "ledger_ref": res.get("ledger_ref", "")})
        refuted = exit_code != 0
        confidence = 0.99 if not refuted else 0.95
        reason = (f"engine re-check exit {exit_code}: "
                  + ("confirmed" if not refuted else "refuted"))
        result = VerifierResult(refuted, confidence, reason, checks_run, csha)

    elif judge is not None:
        evidence = "\n".join(str(r) for r in (refs or [])) or "(none provided)"
        prompt = REFUTE_FIRST_PROMPT.format(claim=claim_text, evidence=evidence)
        j = judge(prompt) or {}
        # refute-first: default to refuted if the judge is silent/ambiguous
        refuted = bool(j.get("refuted", True))
        confidence = float(j.get("confidence", 0.5))
        reason = j.get("reason", "judge did not justify; defaulting to refuted")
        checks_run.append({"judge": True, "refuted": refuted})
        result = VerifierResult(refuted, confidence, reason, checks_run, csha)

    else:
        result = VerifierResult(
            True, 0.5,
            "no declared command and no judge: insufficient evidence -> refuted",
            checks_run, csha)

    if emit_receipt is not None:
        verdict = "failed" if result.refuted else "completed"
        fields = {
            "kind": "verifier",
            "actor": actor,
            "verdict": verdict,
            "workspace": workspace,
            "session_id": session_id,
            "claim_sha256": csha,
            "canonical_command": declared_verify_cmd,
            "evidence": [{"type": "check", "value": c} for c in checks_run],
        }
        result.receipt_id = emit_receipt(fields) or ""

    return result.as_dict()


@dataclass
class Verifier:
    """Budgeted, dedup-aware wrapper over :func:`verify_claim`.

    ``escalate`` is the budgeted entry point: identical claims (same
    ``claim_sha256``) are answered from cache and cost nothing; fresh claims
    are charged against the per-session / per-window caps and raise
    ``BudgetExceeded`` past a cap.
    """

    run_check: RunCheck | None = None
    judge: Judge | None = None
    emit_receipt: EmitReceipt | None = None
    budget: EscalationBudget = field(default_factory=EscalationBudget)
    actor: str = "reviewer"

    def escalate(self, claim_text: str, *, workspace: str = "",
                 session_id: str = "", refs: list | None = None,
                 declared_verify_cmd: str = "") -> dict:
        csha = claim_sha256(claim_text)
        cached = self.budget.cached(csha)
        if cached is not None:
            return cached
        self.budget.check(session_id, csha)   # raises BudgetExceeded past a cap
        result = verify_claim(
            claim_text, workspace=workspace, session_id=session_id, refs=refs,
            declared_verify_cmd=declared_verify_cmd, run_check=self.run_check,
            judge=self.judge, emit_receipt=self.emit_receipt, actor=self.actor)
        self.budget.charge(session_id, csha)
        self.budget.remember(csha, result)
        return result
