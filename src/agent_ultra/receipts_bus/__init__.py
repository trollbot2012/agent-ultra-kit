"""receipts_bus — a unified, authenticated receipt index.

Every unit of agent work can leave a *receipt*: a signed record of what was
done, what it claimed, and what evidence backs the claim. The bus is the store
and the resolver: given a claim, it returns the receipts that could bind to it,
and it tells you — by an explicit rule — which ones actually do.

Two properties are load-bearing:

  * **Authenticity is separate from integrity.** ``receipt_sha256`` proves the
    body was not altered; ``receipt_hmac`` proves it was written by something
    holding the per-install key. A hand-authored envelope with a correct
    sha256 but no valid HMAC is ``authentic=False`` and any enforce-mode
    consumer must reject it.

  * **A failed read never returns empty.** A busy or corrupt store raises
    ``BusUnavailable`` rather than silently returning ``[]`` (which a caller
    would misread as "no evidence exists").

Everything host-specific — the key path, the store path, TTLs, caps — is a
constructor parameter with a neutral default. Stdlib only.
"""

from __future__ import annotations

from .envelope import (  # noqa: F401
    KINDS,
    ACTORS,
    VERDICTS,
    ACCEPTABLE_VERDICTS,
    COMPLETION_KINDS,
    canonical_body,
    receipt_sha256,
    build_envelope,
    EnvelopeError,
)
from .bus import (  # noqa: F401
    ReceiptsBus,
    BusUnavailable,
    Candidate,
)

__all__ = [
    "KINDS",
    "ACTORS",
    "VERDICTS",
    "ACCEPTABLE_VERDICTS",
    "COMPLETION_KINDS",
    "canonical_body",
    "receipt_sha256",
    "build_envelope",
    "EnvelopeError",
    "ReceiptsBus",
    "BusUnavailable",
    "Candidate",
]
