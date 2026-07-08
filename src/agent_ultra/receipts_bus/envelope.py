"""Receipt envelope — shape, closed enums, and the two hash controls.

A receipt is a plain dict (JSON-serialisable) so it can cross process and
language boundaries. This module owns:

  * the closed enums (``kind``, ``actor``, ``verdict``) validated at write time,
  * the canonical body (the envelope minus its two hash fields), and
  * the two hashes: ``receipt_sha256`` (integrity) and ``receipt_hmac``
    (authenticity, keyed).

The enum members are GENERIC. A host maps its own engines/routes onto the
neutral actor names via config; it does not add members here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

# ---- closed enums --------------------------------------------------------
# Validated at write time. Generic members; hosts map their own concepts on.

KINDS = frozenset({
    "panel",          # an adversarial-panel run
    "loop",           # a build/test/fix loop iteration
    "command",        # a brokered command execution
    "verification",   # a check/gate result
    "verifier",       # a refute-first verifier verdict
    "route_health",   # a fleet/route probe result
    "repair",         # a repair/watchdog action
    "manual",         # an operator-attested receipt (never satisfies completion)
})

ACTORS = frozenset({
    "engine_a",       # a build/execution engine
    "engine_b",       # a second, independent engine (backend diversity)
    "engine_c",       # a third engine
    "reviewer",       # a critic/review engine
    "broker",         # the command broker
    "operator",       # a human operator
})

VERDICTS = frozenset({
    "shipped",
    "approved",
    "completed",
    "hold",
    "conditional",
    "blocked",
    "failed",
    "degraded",
})

# Verdicts a completion/enforce gate may accept. A refuted/blocked/failed
# receipt can never satisfy a gate — that is what makes refute-first safe.
ACCEPTABLE_VERDICTS = frozenset({"shipped", "approved", "completed"})

# Kinds that may, on their own, satisfy a completion claim. A command receipt
# proves a command ran, not that work is done; manual/repair/route_health are
# never completion evidence.
COMPLETION_KINDS = frozenset({"panel", "loop", "verification", "verifier"})


class EnvelopeError(ValueError):
    """A receipt violated the envelope contract (bad enum, missing field)."""


# fields excluded from the hashed body (they ARE the hashes)
_HASH_FIELDS = ("receipt_sha256", "receipt_hmac")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical_body(envelope: dict) -> str:
    """The canonical JSON body that both hashes cover: the envelope minus its
    two hash fields, ``json.dumps(sort_keys=True)``. Stable across dict
    ordering so the same logical receipt always hashes the same."""
    body = {k: v for k, v in envelope.items() if k not in _HASH_FIELDS}
    return json.dumps(body, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def receipt_sha256(envelope: dict) -> str:
    """Integrity hash of the canonical body."""
    return hashlib.sha256(canonical_body(envelope).encode("utf-8")).hexdigest()


def receipt_hmac(envelope: dict, key: bytes) -> str:
    """Authenticity hash of the canonical body, keyed by the per-install key."""
    return hmac.new(key, canonical_body(envelope).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _validate(kind: str, actor: str, verdict: str) -> None:
    if kind not in KINDS:
        raise EnvelopeError(f"unknown kind {kind!r}; allowed: {sorted(KINDS)}")
    if actor not in ACTORS:
        raise EnvelopeError(f"unknown actor {actor!r}; allowed: {sorted(ACTORS)}")
    if verdict not in VERDICTS:
        raise EnvelopeError(
            f"unknown verdict {verdict!r}; allowed: {sorted(VERDICTS)}")


def build_envelope(
    *,
    kind: str,
    actor: str,
    verdict: str,
    run_id: str = "",
    session_id: str = "",
    workspace: str = "",
    task_ref: dict | None = None,
    canonical_command: str = "",
    claim_sha256: str = "",
    inputs_hash: str = "",
    evidence: list | None = None,
    artifact_paths: list | None = None,
    key: bytes | None = None,
    receipt_id: str = "",
    ts: str = "",
) -> dict:
    """Build a fully-formed, hashed (and, if ``key`` given, signed) envelope.

    ``kind``/``actor``/``verdict`` are validated against the closed enums.
    ``task_ref`` is a small dict of optional claim keys (e.g. an issue id, a
    cited receipt id). Passing ``key`` stamps the HMAC; omitting it leaves
    ``receipt_hmac=""`` — such a receipt is integrity-checkable but NOT
    authentic, and enforce-mode consumers must reject it.
    """
    _validate(kind, actor, verdict)
    env: dict = {
        "receipt_id": receipt_id or uuid.uuid4().hex,
        "kind": kind,
        "run_id": run_id,
        "session_id": session_id,
        "ts": ts or _utc_now(),
        "actor": actor,
        "workspace": workspace,
        "task_ref": dict(task_ref or {}),
        "canonical_command": canonical_command,
        "claim_sha256": claim_sha256,
        "inputs_hash": inputs_hash,
        "evidence": list(evidence or []),
        "verdict": verdict,
        "artifact_paths": list(artifact_paths or []),
    }
    env["receipt_sha256"] = receipt_sha256(env)
    env["receipt_hmac"] = receipt_hmac(env, key) if key else ""
    return env


def verify_hashes(envelope: dict, key: bytes | None) -> tuple[bool, bool]:
    """Return ``(integrity, authentic)`` for an envelope.

    ``integrity`` — the stored ``receipt_sha256`` matches a recomputation.
    ``authentic`` — a non-empty ``receipt_hmac`` matches an HMAC recomputed
    with ``key`` (constant-time compare). No key or empty HMAC => not
    authentic, regardless of integrity.
    """
    integrity = hmac.compare_digest(
        str(envelope.get("receipt_sha256", "")), receipt_sha256(envelope))
    stored_mac = str(envelope.get("receipt_hmac", ""))
    if not key or not stored_mac:
        return integrity, False
    authentic = hmac.compare_digest(stored_mac, receipt_hmac(envelope, key))
    return integrity, authentic
