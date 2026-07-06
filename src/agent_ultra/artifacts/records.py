"""Artifact and ledger output — every run leaves a durable, uniform record.

One schema for panel runs, ULTRA loops, and anything else built on the kit:
question/task, routes, lenses, claims, steelmen, accepted/rejected findings,
proof gates, commands run, files touched, outputs, final decision.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from ..evidence.reader import redact_secrets


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit].rstrip("-") or "run"


@dataclass
class RunRecord:
    kind: str                    # panel | ultra | custom
    question: str                # the question or task
    run_id: str = ""
    started_utc: str = field(default_factory=_utc_now)
    duration_s: float = 0.0
    routes: dict = field(default_factory=dict)      # selection, health, degradation
    lenses: list = field(default_factory=list)
    claims: list = field(default_factory=list)      # raw critic claims
    steelmen: list = field(default_factory=list)
    accepted: list = field(default_factory=list)    # accepted findings
    rejected: list = field(default_factory=list)    # rejected/theoretical findings
    proof_gates: list = field(default_factory=list)
    destructive_gates: list = field(default_factory=list)
    commands: list = field(default_factory=list)    # broker results / commands run
    files_touched: list = field(default_factory=list)
    outputs: dict = field(default_factory=dict)     # synthesis, test output...
    decision: str = ""
    errors: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)


_ledger_lock = threading.Lock()


def append_jsonl(path: Path | str, obj) -> None:
    """Append one JSON object to a ledger file. Best-effort; never raises."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not isinstance(obj, (dict, list)):
            obj = asdict(obj)
        line = redact_secrets(json.dumps(obj, ensure_ascii=False)) + "\n"
        with _ledger_lock:
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(line)
    except (OSError, TypeError, ValueError):
        pass


def _md_section(title: str, body: str) -> str:
    return f"## {title}\n\n{body.strip()}\n" if body.strip() else ""


def _findings_md(items: list) -> str:
    lines = []
    for f in items:
        d = f if isinstance(f, dict) else asdict(f)
        lines.append(f"- **[{d.get('verdict', '?')}]** ({d.get('lens', '?')}"
                     f", {d.get('severity', '?')}) {d.get('claim', '')}")
        if d.get("reasoning"):
            lines.append(f"  - judge: {d['reasoning']}")
    return "\n".join(lines)


def render_markdown(rec: RunRecord) -> str:
    parts = [
        f"# {rec.kind.upper()} run — {rec.question[:120]}",
        "",
        f"- run id: `{rec.run_id}`",
        f"- started: {rec.started_utc}  duration: {rec.duration_s:.1f}s",
        f"- routes: `{json.dumps(rec.routes, ensure_ascii=False)[:400]}`",
        f"- lenses: {', '.join(rec.lenses) if rec.lenses else '(none)'}",
        "",
        _md_section("Decision", rec.decision),
        _md_section("Synthesis", str(rec.outputs.get("synthesis", ""))),
        _md_section(f"Accepted findings ({len(rec.accepted)})",
                    _findings_md(rec.accepted)),
        _md_section(f"Rejected / theoretical ({len(rec.rejected)})",
                    _findings_md(rec.rejected)),
        _md_section("Proof gates",
                    "\n".join(f"- `{g}`" for g in rec.proof_gates)),
        _md_section("DESTRUCTIVE gates (require a human — do not paste blindly)",
                    "\n".join(f"- `{g}`" for g in rec.destructive_gates)),
        _md_section("Commands run",
                    "\n".join(f"- [{c.get('risk_tier', '?')}/{c.get('status', '?')}] "
                              f"`{c.get('command', '')}`"
                              for c in rec.commands if isinstance(c, dict))),
        _md_section("Files touched",
                    "\n".join(f"- {f}" for f in rec.files_touched)),
        _md_section("Errors",
                    "\n".join(f"- {json.dumps(e, ensure_ascii=False)[:200]}"
                              for e in rec.errors)),
    ]
    return "\n".join(p for p in parts if p is not None)


def write_run(rec: RunRecord, out_dir: Path | str,
              ledger: Path | str | None = None) -> tuple[Path, Path]:
    """Write <stamp>-<slug>.json + .md into *out_dir*; optionally append a
    summary line to a JSONL ledger. Returns (json_path, md_path)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = f"{stamp}-{slug(rec.question)}"
    if not rec.run_id:
        rec.run_id = f"{rec.kind}-{base}"
    jp = out / f"{base}.json"
    mp = out / f"{base}.md"
    payload = redact_secrets(json.dumps(asdict(rec), indent=2, ensure_ascii=False))
    jp.write_text(payload, encoding="utf-8")
    mp.write_text(redact_secrets(render_markdown(rec)), encoding="utf-8")
    if ledger:
        append_jsonl(ledger, {
            "ts": rec.started_utc, "run_id": rec.run_id, "kind": rec.kind,
            "question": rec.question[:200], "decision": rec.decision[:300],
            "accepted": len(rec.accepted), "rejected": len(rec.rejected),
            "proof_gates": len(rec.proof_gates), "artifact": str(jp),
        })
    return jp, mp
