"""verifier — refute-first claim verification.

The verifier's job is to try to REFUTE a completion claim, not to confirm it.
It defaults to *refuted* whenever evidence is insufficient, so a claim only
survives if something concrete backs it:

  * **Engine re-check.** If the claim declares a verify command and a
    ``run_check`` runner is injected, run it: exit 0 confirms, non-zero
    refutes. No LLM is consulted — a real command is the strongest evidence.

  * **LLM refute.** With no declared command, an injected ``judge`` is asked,
    with a refute-first prompt that defaults to refuted on insufficient
    evidence.

  * **Neither channel => refuted** (the honest default).

A verifier receipt is stamped with the claim's ``claim_sha256`` so it can only
ever resolve the claim it actually checked. A refuted verdict uses a
non-acceptable receipt verdict, so it can never satisfy a gate.

Both the command runner and the judge are INJECTED — the kit runs no commands
and makes no network calls by default.
"""

from __future__ import annotations

from .verify import (  # noqa: F401
    verify_claim,
    claim_sha256,
    Verifier,
    VerifierResult,
    REFUTE_FIRST_PROMPT,
)
from .budget import EscalationBudget, BudgetExceeded  # noqa: F401

__all__ = [
    "verify_claim",
    "claim_sha256",
    "Verifier",
    "VerifierResult",
    "REFUTE_FIRST_PROMPT",
    "EscalationBudget",
    "BudgetExceeded",
]
