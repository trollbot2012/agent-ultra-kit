"""bob — the 10-step enforced build pipeline, public edition.

    SPEC -> RED -> GREEN -> REFACTOR -> CODE-QUALITY -> SECURITY-FANOUT
         -> WORKFLOW -> ULTRA -> QUIZ -> COMMIT

Every gated step leaves a hash-chained, HMAC-signed receipt written from real
execution (pytest output, ultracode run receipts, the panel's execution
receipt). The commit/report gate re-derives what it can (live pytest) and
cross-checks the rest — a skipped, faked, or edited step blocks.

    agent-ultra bob run "add a slugify helper" --mock     # no key needed
    agent-ultra bob gate                                  # validate the chain
"""

from .receipts import (
    STEPS, GATED_STEPS, BobReceiptError,
    build_step_receipt, load_receipt, validate_chain,
)
from .pipeline import (
    BobRun, BobProofError, GateResult,
    gate_check, run_pytest_step, assert_bob_done, load_or_create_key,
    install_hooks, scope_contains, scope_entry_is_infra,
)
from .runner import run_bob, BobOutcome

__all__ = [
    "STEPS", "GATED_STEPS", "BobReceiptError",
    "build_step_receipt", "load_receipt", "validate_chain",
    "BobRun", "BobProofError", "GateResult",
    "gate_check", "run_pytest_step", "assert_bob_done", "load_or_create_key",
    "install_hooks", "scope_contains", "scope_entry_is_infra",
    "run_bob", "BobOutcome",
]
