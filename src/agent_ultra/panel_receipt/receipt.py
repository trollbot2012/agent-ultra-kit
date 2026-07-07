"""Build and validate panel execution receipts.

The receipt is derived from a REAL ``PanelReport`` (model_calls, lenses,
per-finding origins, route errors). ``lens_count_executed`` can only exceed 0
when the panel actually invoked the model — a self-review yields 0 and fails.
A mandatory ``receipt_sha256`` integrity checksum stops a hand-authored JSON
file from forging a passing panel.

Stdlib only. No network. Reusable by the loop (Level 1) and the REPORT gate
(Level 2).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

RECEIPT_NAME = "panel_execution_receipt.json"

# the exact failure messages the pipeline surfaces — do not paraphrase
ERR_ZERO_AGENTS = ("PANEL phase completed with 0 agent calls — self-review is "
                   "not a panel.")
ERR_REPORT_MISSING = ("REPORT blocked: missing or invalid panel execution "
                      "receipt.")
ERR_REPORT_ZERO = ("REPORT blocked: PANEL phase completed with 0 agent calls "
                   "— self-review is not a panel.")


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_receipt(report, *, task_id: str, reviewed_input: str,
                  panel_artifact_path: str = "") -> dict:
    """Build a receipt from a real PanelReport.

    *reviewed_input* is the actual source/context the panel reviewed; its hash
    is ``artifact_hash`` (proof the receipt refers to the reviewed artifact, not
    just itself). If ``model_calls <= 0`` no lens can be counted as executed,
    regardless of how many were requested — the anti-fraud core.
    """
    requested = list(getattr(report, "lenses", []) or [])
    findings = list(getattr(report, "findings", []) or [])
    model_calls = int(getattr(report, "model_calls", 0) or 0)
    errors = list(getattr(report, "errors", []) or [])

    errored_labels = {str(e.get("label")) for e in errors
                      if e.get("phase") == "challenge" and e.get("label")}

    per_lens_findings: dict = {}
    per_lens_output: dict = {}
    for f in findings:
        for part in (getattr(f, "lens", "") or "").split("+"):
            part = part.strip()
            if not part:
                continue
            per_lens_findings[part] = per_lens_findings.get(part, 0) + 1
            per_lens_output.setdefault(part, []).append(
                getattr(f, "claim", "") or "")

    lenses = []
    executed = errored = 0
    for name in requested:
        is_errored = (model_calls <= 0) or (name in errored_labels)
        fcount = per_lens_findings.get(name, 0)
        out = ("\n".join(per_lens_output.get(name, [])) if fcount
               else f"NO_FINDING:{name}")
        if is_errored:
            errored += 1
        else:
            executed += 1
        lenses.append({
            "lens_id": _sha256(name)[:12],
            "lens_name": name,
            "role": f"adversarial:{name}",
            "model_call_id": (getattr(report, "run_id", "") or "") + ":" + name
            if not is_errored else None,
            "status": "error" if is_errored else "ok",
            "findings_count": fcount,
            "output_hash": _sha256(out),
            "error": "challenge call failed or no real model invocation"
            if is_errored else None,
        })

    receipt = {
        "panel_id": getattr(report, "run_id", "") or _sha256(
            reviewed_input + _now())[:16],
        "task_id": task_id,
        "timestamp": _now(),
        "model_used": _model_used(report, findings),
        "router": (getattr(report, "routes", {}) or {}).get("judge", "")
        or "router",
        "artifact_hash": _sha256(reviewed_input),
        "lens_count_requested": len(requested),
        "lens_count_executed": executed,
        "lens_count_errored": errored,
        "lenses": lenses,
        "total_findings_count": len(findings),
        "accepted_findings_count": len(getattr(report, "accepted", []) or []),
        "final_verdict": _map_verdict(report),
        "panel_artifact_path": panel_artifact_path,
        "model_calls": model_calls,
    }
    receipt["receipt_sha256"] = _sha256(
        json.dumps(receipt, sort_keys=True, ensure_ascii=False))
    return receipt


def _model_used(report, findings) -> str:
    routes = getattr(report, "routes", {}) or {}
    if routes.get("judge"):
        return str(routes["judge"])
    for f in findings:
        m = getattr(f, "origin_route", "")
        if m:
            return str(m)
    return "unknown"


def _map_verdict(report) -> str:
    accepted = getattr(report, "accepted", []) or []
    blocking = [f for f in accepted
                if getattr(f, "verdict", "") == "real_now"
                and getattr(f, "severity", "") in ("high", "critical")]
    if getattr(report, "model_calls", 0) <= 0:
        return "blocked"
    if blocking:
        return "fix"
    if accepted:
        return "required_review"
    return "ship"


def write_receipt(art_dir, receipt: dict) -> Path:
    art_dir = Path(art_dir)
    art_dir.mkdir(parents=True, exist_ok=True)
    path = art_dir / RECEIPT_NAME
    path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return path


def _load(path) -> tuple[dict | None, str]:
    p = Path(path)
    if p.is_dir():
        p = p / RECEIPT_NAME
    if not p.is_file():
        return None, "receipt file not found"
    try:
        return json.loads(p.read_text(encoding="utf-8")), ""
    except (OSError, ValueError) as e:
        return None, f"receipt unreadable/malformed: {e}"


def validate_receipt(path, *, expect_task_id: str = "",
                     expect_artifact_hash: str = "") -> tuple[bool, str]:
    """Validate a receipt for the in-loop PANEL check (Level 1).

    Returns (ok, message). ok=True -> 'PANEL valid.'. On failure the message is
    one of the exact pipeline strings.
    """
    receipt, _ = _load(path)
    if receipt is None:
        return False, ERR_REPORT_MISSING
    # receipt_sha256 is MANDATORY: a receipt without a valid integrity checksum
    # is invalid, so a hand-authored JSON file cannot forge a passing panel.
    required = ("panel_id", "task_id", "timestamp", "model_used",
                "artifact_hash", "lens_count_executed", "lenses",
                "final_verdict", "receipt_sha256")
    for key in required:
        if key not in receipt:
            return False, ERR_REPORT_MISSING
    try:
        executed = int(receipt.get("lens_count_executed", 0))
    except (TypeError, ValueError):
        return False, ERR_REPORT_MISSING
    if executed <= 0 or not receipt.get("lenses"):
        return False, ERR_ZERO_AGENTS
    if not receipt.get("artifact_hash") or not receipt.get("final_verdict"):
        return False, ERR_REPORT_MISSING
    ok_lenses = [ln for ln in receipt["lenses"] if ln.get("status") == "ok"]
    if not ok_lenses or any(not ln.get("output_hash") for ln in ok_lenses):
        return False, ERR_ZERO_AGENTS
    if expect_task_id and receipt.get("task_id") != expect_task_id:
        return False, ERR_REPORT_MISSING
    if expect_artifact_hash and receipt.get("artifact_hash") != expect_artifact_hash:
        return False, ERR_REPORT_MISSING
    body = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    if _sha256(json.dumps(body, sort_keys=True, ensure_ascii=False)) \
            != receipt["receipt_sha256"]:
        return False, ERR_REPORT_MISSING
    return True, "PANEL valid."


def gate_report(run_dir, *, expect_task_id: str = "",
                expect_artifact_hash: str = "") -> tuple[bool, str]:
    """REPORT-phase gate (Level 2). Returns (allowed, message)."""
    receipt, _ = _load(run_dir)
    if receipt is None:
        return False, ERR_REPORT_MISSING
    try:
        executed = int(receipt.get("lens_count_executed", 0))
    except (TypeError, ValueError):
        return False, ERR_REPORT_MISSING
    if executed <= 0 or not receipt.get("lenses"):
        return False, ERR_REPORT_ZERO
    ok, msg = validate_receipt(run_dir, expect_task_id=expect_task_id,
                               expect_artifact_hash=expect_artifact_hash)
    if not ok:
        return False, (ERR_REPORT_ZERO if msg == ERR_ZERO_AGENTS
                       else ERR_REPORT_MISSING)
    return True, "REPORT allowed to start."
