"""Bob receipt schema — one hash-chained, HMAC-signed receipt per pipeline step.

A bob receipt is a plain JSON dict (public-safe, stdlib-only) with three
tamper controls, reusing the receipts-bus hash helpers:

  * ``receipt_sha256`` — integrity: the body was not edited after writing.
  * ``receipt_hmac``   — authenticity: it was written by something holding the
    per-install key (a hand-authored receipt with a correct sha256 but no
    valid HMAC is a forgery and the gate rejects it).
  * ``prev_sha256``    — chaining: each receipt embeds the sha256 of the one
    before it, so editing or re-writing ANY earlier step breaks every later
    receipt. The chain is the pipeline's spine.

``writer`` records WHO produced the evidence: ``system`` receipts are written
by the pytest runner from real subprocess output; ``engine`` receipts are
written by the bob runner from real ultracode / panel artifacts it can
re-verify; ``agent`` receipts carry judgment-step notes the gate does not
treat as proof. The gate trusts nothing it cannot re-derive or cross-check.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from ..receipts_bus.envelope import receipt_sha256, receipt_hmac, verify_hashes

STEPS = (
    "step01_spec", "step02_red", "step03_green", "step04_refactor",
    "step05_codequality", "step06_security", "step07_workflow",
    "step08_panel", "step09_quiz", "step10_commit",
    # surgical lane (lightweight tier for INERT doc/config edits — never a
    # substitute for the full pipeline; code always routes to full)
    "step_surgical_review", "step_surgical_quiz",
)

# Steps the gate demands a receipt for. 1/4/5 are judgment steps where a
# mechanical gate would fake enforcement; 10's record is gate-pass.json itself.
GATED_STEPS = ("step02_red", "step03_green", "step06_security",
               "step07_workflow", "step08_panel", "step09_quiz")

# the surgical lane's own (narrower, harder) receipt demands
SURGICAL_GATED_STEPS = ("step_surgical_review", "step_surgical_quiz")

WRITERS = frozenset({"system", "engine", "agent"})


class BobReceiptError(ValueError):
    """A bob receipt violated the schema (bad step, bad writer, bad chain)."""


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_step_receipt(
    *,
    step: str,
    run_id: str,
    seq: int,
    writer: str,
    evidence: dict,
    files: dict | None = None,
    prev_sha256: str = "",
    mock: bool = False,
    key: bytes | None = None,
    ts: str = "",
) -> dict:
    """Build a fully-formed, hashed (and, with ``key``, signed) step receipt.

    ``files`` maps workspace-relative paths to their sha256 at receipt time —
    the gate's staleness check re-hashes them later. ``mock`` marks evidence
    produced on the keyless mock route; the gate only accepts mock receipts
    when explicitly told to (demo mode), never by default.
    """
    if step not in STEPS:
        raise BobReceiptError(f"unknown step {step!r}; allowed: {list(STEPS)}")
    if writer not in WRITERS:
        raise BobReceiptError(f"unknown writer {writer!r}; allowed: {sorted(WRITERS)}")
    rec: dict = {
        "schema": "bob-step-receipt/1",
        "receipt_id": uuid.uuid4().hex,
        "step": step,
        "run_id": run_id,
        "seq": int(seq),
        "ts": ts or utc_now(),
        "writer": writer,
        "mock": bool(mock),
        "evidence": dict(evidence or {}),
        "files": dict(files or {}),
        "prev_sha256": prev_sha256,
    }
    rec["receipt_sha256"] = receipt_sha256(rec)
    rec["receipt_hmac"] = receipt_hmac(rec, key) if key else ""
    return rec


def load_receipt(path: str | Path) -> dict | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def validate_chain(receipts: list[dict], key: bytes | None) -> list[str]:
    """Validate an ordered receipt chain. Returns a list of errors (empty =
    chain holds). Checks, per receipt: schema fields, integrity sha256,
    HMAC authenticity (fail closed — no key or empty HMAC is a failure),
    strictly increasing ``seq``, and ``prev_sha256`` linkage.
    """
    errors: list[str] = []
    prev_sha = ""
    prev_seq = -1
    for rec in receipts:
        label = rec.get("step", "?")
        if rec.get("schema") != "bob-step-receipt/1":
            errors.append(f"{label}: not a bob step receipt (schema field)")
            continue
        integrity, authentic = verify_hashes(rec, key)
        if not integrity:
            errors.append(f"{label}: integrity FAIL — receipt edited after writing")
        if not authentic:
            errors.append(f"{label}: authenticity FAIL — missing/invalid HMAC "
                          "(hand-authored receipt?)")
        seq = rec.get("seq", -1)
        if not isinstance(seq, int) or seq <= prev_seq:
            errors.append(f"{label}: seq {seq!r} does not increase (chain "
                          "reordered or receipt re-written)")
        if rec.get("prev_sha256", "") != prev_sha:
            errors.append(f"{label}: prev_sha256 does not match the previous "
                          "receipt — the chain is broken")
        prev_sha = rec.get("receipt_sha256", "")
        prev_seq = seq if isinstance(seq, int) else prev_seq
    return errors
