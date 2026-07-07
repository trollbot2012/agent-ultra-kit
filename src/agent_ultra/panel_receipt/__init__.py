"""Panel execution receipt — structural proof that a REAL panel ran.

A phase labelled PANEL is not proof. Only a valid panel execution receipt with
real executed lenses satisfies PANEL. An agent that self-reads and calls it a
"panel" produces no PanelReport with model_calls > 0, so it cannot forge a
passing receipt. This is the public equivalent of the enforcement proven in the
private reference implementation.

Additive and stdlib-only: does not weaken the existing proof gates and adds no
dependencies.
"""

from .receipt import (
    RECEIPT_NAME,
    ERR_ZERO_AGENTS,
    ERR_REPORT_MISSING,
    ERR_REPORT_ZERO,
    build_receipt,
    write_receipt,
    validate_receipt,
    gate_report,
)

__all__ = [
    "RECEIPT_NAME", "ERR_ZERO_AGENTS", "ERR_REPORT_MISSING", "ERR_REPORT_ZERO",
    "build_receipt", "write_receipt", "validate_receipt", "gate_report",
]
