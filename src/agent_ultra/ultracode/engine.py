"""Ultracode — deterministic multi-agent workflow engine.

An ultracode workflow is a plain Python module with a ``META`` dict and an
``async def run(wf)`` coroutine. The ``wf`` object fans work across many
bounded agents — each agent is ONE model call (optionally schema-validated) —
through ``parallel`` (barrier) and ``pipeline`` (no barrier) combinators, under
hard call/token budgets, with a resumable journal, broker-gated host checks,
and an anti-fraud receipt.

    META = {"name": "smoke", "description": "...", "phases": ["Ping"]}

    async def run(wf):
        wf.phase("Ping")
        a, b = await wf.parallel([
            lambda: wf.agent("Reply with exactly: pong-1", label="ping-1"),
            lambda: wf.agent("Reply with exactly: pong-2", label="ping-2"),
        ])
        return {"replies": [a, b]}

The pattern this proves: multi-agent fan-out -> journal -> resume -> receipt
-> status. It is stdlib-only at the core; the model endpoint is any
OpenAI-compatible URL, and the offline mock route needs no key.

The journal (``events.jsonl`` + ``journal.jsonl``) and the receipt
(``receipt.json``, checksummed) are the source of truth. A status card is a
pure display over the journal — a workflow's own text output never moves it.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path

from ..routes.pool import RoutePool
from ..routes.client import RouteError

logger = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# Home + config (env -> defaults; no committed paths)
# --------------------------------------------------------------------------

def ultracode_home() -> Path:
    env = os.environ.get("AGENT_ULTRA_HOME", "").strip()
    base = Path(env) if env else Path.home() / ".agent-ultra"
    return base / "ultracode"


DEFAULTS: dict = {
    "max_workers": 6,          # concurrent in-flight agents
    "max_calls": 200,          # model calls per run; exceeded -> HOLD
    "token_budget": 0,         # prompt+completion tokens per run; 0 = off
    "max_tokens_agent": 4000,  # per-call completion cap (agent may override)
    "schema_retries": 2,       # re-asks when JSON is invalid against a schema
    "evidence_max_files": 6,
    "evidence_max_chars": 8000,
    "check_timeout": 30,
    "report_agents_cap": 200,
    "report_text_cap": 2000,
}


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class WorkflowError(Exception):
    """A run could not start or the script is malformed."""


class BudgetExceeded(WorkflowError):
    """The run hit its hard call/token ceiling; everything so far is journaled
    so a resume continues from the paid-for prefix."""


class AgentCallError(Exception):
    """Every candidate route failed for one agent call (internal)."""


# --------------------------------------------------------------------------
# JSON extraction + minimal schema validation (models wrap JSON in prose)
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _scan_balanced(cand: str, start: int, opener: str, closer: str):
    depth, in_str, esc = 0, False, False
    for i in range(start, len(cand)):
        ch = cand[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cand[start:i + 1])
                except Exception:
                    return None
    return None


def extract_json(text: str):
    if not text:
        return None
    for cand in _FENCE_RE.findall(text) + [text]:
        cand = cand.strip()
        starts = sorted(
            (cand.find(o), o, c)
            for o, c in (("[", "]"), ("{", "}")) if cand.find(o) >= 0)
        for start, opener, closer in starts:
            parsed = _scan_balanced(cand, start, opener, closer)
            if parsed is not None:
                return parsed
    return None


_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate_schema(value, schema: dict, path: str = "$") -> list[str]:
    errs: list[str] = []
    if not isinstance(schema, dict):
        return errs
    typ = schema.get("type")
    if typ:
        types = typ if isinstance(typ, list) else [typ]
        if not any(_TYPE_CHECKS.get(t, lambda v: True)(value) for t in types):
            return [f"{path}: expected {typ}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum {schema['enum']}")
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}: missing required key {req!r}")
        for k, sub in (schema.get("properties") or {}).items():
            if k in value:
                errs.extend(validate_schema(value[k], sub, f"{path}.{k}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            errs.extend(validate_schema(item, schema["items"], f"{path}[{i}]"))
    return errs


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "run")).strip("-").lower()
    return s[:limit] or "run"


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...(truncated, {len(text)} chars total)"


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------
# Terminal rendering — DISPLAY ONLY. Plain ASCII unless the terminal provably
# supports better; any failure degrades and never raises into the run.
# --------------------------------------------------------------------------

_GLYPHS_RICH = {"completed": "●", "deployed": "○",
                "failed": "⚠", "blocked": "⚠"}
_GLYPHS_PLAIN = {"completed": "#", "deployed": ".", "failed": "!", "blocked": "!"}


def supports_rich(stream=None, env=None) -> bool:
    try:
        env = os.environ if env is None else env
        if str(env.get("AGENT_ULTRA_PLAIN", "")).strip() not in ("", "0"):
            return False
        if env.get("NO_COLOR") or env.get("CI"):
            return False
        stream = sys.stdout if stream is None else stream
        if not (hasattr(stream, "isatty") and stream.isatty()):
            return False
        term = str(env.get("TERM", "")).strip().lower()
        if term in ("dumb", "unknown"):
            return False
        if os.name == "nt" and not (env.get("WT_SESSION") or env.get("ANSICON")
                                    or env.get("TERM_PROGRAM") or term):
            return False
        enc = str(getattr(stream, "encoding", "") or "").lower()
        if enc and "utf" not in enc:
            return False
        return True
    except Exception:
        return False


def _ascii(text: str) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


def _card_text(s: dict, glyphs: dict, plain: bool) -> str:
    counts = s.get("counts") if isinstance(s.get("counts"), dict) else {}

    def n(key):
        try:
            return int(counts.get(key, 0) or 0)
        except Exception:
            return 0

    marks = ""
    for d in (s.get("dots") or []):
        st = str((d or {}).get("state", "")) if isinstance(d, dict) else ""
        marks += glyphs.get(st, glyphs["deployed"])
    status = str(s.get("final_state") or "") or "RUNNING"
    lines = ["ULTRACODE"]
    if s.get("run_id"):
        lines.append(f"Run: {s['run_id']}")
    if s.get("name"):
        lines.append(f"Name: {s['name']}")
    if s.get("phase"):
        lines.append(f"Phase: {s['phase']}")
    lines.append(f"Agents: {n('completed')}/{n('deployed')} complete")
    lines.append(f"Failed: {n('failed')}")
    lines.append(f"Blocked: {n('blocked')}")
    queued = n("planned") - n("deployed")
    if queued > 0:
        lines.append(f"Queued: {queued}")
    if marks:
        lines.append("Progress: [" + marks + "]" if plain
                     else "Progress: " + " ".join(marks))
    lines.append(f"Status: {status}")
    if s.get("error"):
        lines.append("Error: " + " ".join(str(s["error"]).split())[:200])
    if s.get("receipt_path"):
        lines.append(f"Receipt: {s['receipt_path']}")
    text = "\n".join(lines)
    return _ascii(text) if plain else text


def render_card(summary, rich: bool | None = None, stream=None) -> str:
    """Format a summarize_events() dict for a terminal. Never raises."""
    try:
        if rich is None:
            rich = supports_rich(stream)
        s = summary if isinstance(summary, dict) else {}
        if rich:
            try:
                return _card_text(s, _GLYPHS_RICH, plain=False)
            except Exception:
                pass
        return _card_text(s, _GLYPHS_PLAIN, plain=True)
    except Exception:
        return ("ULTRACODE\nStatus: UNKNOWN (render failed; the journal "
                "events.jsonl and receipt.json remain the source of truth)")


def print_card(summary, stream=None) -> None:
    try:
        out = stream or sys.stdout
        try:
            print(render_card(summary, stream=out), file=out)
        except Exception:
            print(render_card(summary, rich=False), file=out)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Event stream — typed runtime events (the card replays this)
# --------------------------------------------------------------------------

FINAL_STATES = {"completed": ("ultracode_completed", "COMPLETE"),
                "aborted_budget": ("ultracode_hold", "HOLD"),
                "failed": ("ultracode_failed", "FAILED"),
                "cancelled": ("ultracode_cancelled", "CANCELLED")}

_PHASE_ALIASES = (
    ("verification_started", re.compile(r"(?i)verif|skeptic|adversar")),
    ("synthesis_started", re.compile(r"(?i)synth|summar")),
    ("fan_in_started", re.compile(r"(?i)fan[-_ ]?in|merge|dedup|reconcil")),
)
_PHASE_KIND_ALIASES = {"verification": "verification_started",
                       "synthesis": "synthesis_started",
                       "fan_in": "fan_in_started"}


class EventLog:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self._seq = 0
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields) -> None:
        with self._lock:
            self._seq += 1
            entry = {"v": 1, "event": event, "ultracode_run_id": self.run_id,
                     "seq": self._seq, "ts": _utc(), **fields}
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False,
                                        default=str) + "\n")
            except OSError:
                logger.warning("event write failed: %s", self.path)


def summarize_events(events_path: str | Path) -> dict:
    """Replay events.jsonl into the card model. Single source of truth for
    rendering — what the card shows is what the journal proves."""
    order: list[str] = []
    state: dict[str, str] = {}
    roles: dict[str, str] = {}
    planned = 0
    phase = name = final_state = run_id = started = last_ts = ""
    error = receipt = artifact = ""
    try:
        lines = Path(events_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"ok": False, "error": f"no events at {events_path}"}
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        ev = e.get("event", "")
        last_ts = e.get("ts", last_ts)
        aid = e.get("agent_id", "")
        run_id = run_id or e.get("ultracode_run_id", "")
        if ev == "ultracode_started":
            name = e.get("name", "")
            started = e.get("ts", "")
        elif ev == "phase_started":
            phase = e.get("phase", phase)
        elif ev == "agent_planned":
            planned += 1
            roles[aid] = e.get("agent_role", "")
        elif ev in ("agent_deployed", "agent_completed", "agent_failed",
                    "agent_blocked"):
            if aid not in state:
                order.append(aid)
            state[aid] = ev.replace("agent_", "")
        elif ev in ("ultracode_completed", "ultracode_hold",
                    "ultracode_failed", "ultracode_cancelled"):
            final_state = e.get("final_state", "")
            error = e.get("error", "")
            receipt = e.get("receipt_path", "")
            artifact = e.get("artifact_json", "")
    counts = {"planned": planned, "deployed": len(order),
              "completed": sum(1 for s in state.values() if s == "completed"),
              "failed": sum(1 for s in state.values() if s == "failed"),
              "blocked": sum(1 for s in state.values() if s == "blocked")}
    return {"ok": True, "run_id": run_id, "name": name, "phase": phase,
            "final_state": final_state, "counts": counts,
            "dots": [{"agent_id": a, "role": roles.get(a, ""),
                      "state": state[a]} for a in order],
            "started_utc": started, "last_event_utc": last_ts,
            "error": error, "receipt_path": receipt, "artifact_json": artifact}


# --------------------------------------------------------------------------
# Journal (replay cache for resume) + Budget
# --------------------------------------------------------------------------

class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: dict) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False,
                                    default=str) + "\n")
        except OSError:
            logger.warning("journal write failed: %s", self.path)

    @staticmethod
    def load_replay_cache(path: Path) -> dict:
        cache: dict[str, list] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") == "agent" and e.get("status") == "ok":
                    cache.setdefault(e["key"], []).append(e)
        except OSError:
            pass
        return cache


class Budget:
    def __init__(self, max_calls: int, token_budget: int, usage_source=None):
        self.max_calls = int(max_calls)
        self.token_budget = int(token_budget)
        self.calls = 0
        self._src = usage_source
        self._baseline = self._usage_total()

    def _usage_total(self) -> int:
        snap = getattr(self._src, "usage_snapshot", None)
        if snap is None:
            return 0
        try:
            u = snap()
            return int(u.get("prompt_tokens", 0)) + int(u.get("completion_tokens", 0))
        except Exception:
            return 0

    def tokens_spent(self) -> int:
        return max(0, self._usage_total() - self._baseline)

    def remaining_calls(self) -> int:
        return max(0, self.max_calls - self.calls)

    def check_or_raise(self) -> None:
        if self.max_calls and self.calls >= self.max_calls:
            raise BudgetExceeded(
                f"model-call budget exhausted ({self.calls}/{self.max_calls})")
        if self.token_budget and self.tokens_spent() >= self.token_budget:
            raise BudgetExceeded(
                f"token budget exhausted ({self.tokens_spent()}/{self.token_budget})")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class AgentRecord:
    seq: int
    label: str
    phase: str
    prompt_sha: str
    status: str = "ok"       # ok | error | invalid_schema
    route: str = ""
    text: str = ""
    parsed: object = None
    error: str = ""
    calls: int = 0
    cached: bool = False
    duration_s: float = 0.0


@dataclass
class WorkflowReport:
    name: str
    run_id: str
    script_path: str
    script_sha: str
    args: object = None
    status: str = "completed"   # completed | failed | aborted_budget | cancelled
    final_state: str = ""       # COMPLETE | HOLD | FAILED | CANCELLED
    result: object = None
    error: str = ""
    counts: dict = field(default_factory=dict)
    events_path: str = ""
    phases: list = field(default_factory=list)
    model_calls: int = 0
    cached_hits: int = 0
    agents_run: int = 0
    tokens: dict = field(default_factory=dict)
    duration_s: float = 0.0
    started_utc: str = ""
    artifact_dir: str = ""
    artifact_json: str = ""
    journal_path: str = ""
    receipt_path: str = ""
    resumed_from: str = ""
    agents: list = field(default_factory=list)
    log: list = field(default_factory=list)


# --------------------------------------------------------------------------
# Receipt — structural proof a real run happened (checksummed)
# --------------------------------------------------------------------------

def build_receipt(report: WorkflowReport, records: list) -> dict:
    receipt = {
        "schema_version": 1,
        "run_id": report.run_id,
        "name": report.name,
        "script_sha256": report.script_sha,
        "started_utc": report.started_utc,
        "status": report.status,
        "model_calls": report.model_calls,
        "cached_hits": report.cached_hits,
        "tokens": report.tokens,
        "agents": [
            {"seq": r.seq, "label": r.label, "phase": r.phase, "route": r.route,
             "status": r.status, "cached": r.cached, "prompt_sha256": r.prompt_sha,
             "output_sha256": _sha(r.text or "")}
            for r in records],
    }
    receipt["receipt_sha256"] = _receipt_checksum(receipt)
    return receipt


def _receipt_checksum(receipt: dict) -> str:
    body = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    return _sha(json.dumps(body, sort_keys=True, ensure_ascii=False))


def verify_receipt(receipt: dict) -> bool:
    return (isinstance(receipt, dict)
            and receipt.get("receipt_sha256") == _receipt_checksum(receipt))


# --------------------------------------------------------------------------
# The `wf` surface
# --------------------------------------------------------------------------

class Workflow:
    def __init__(self, *, cfg: dict, pool: RoutePool, journal: Journal,
                 events: EventLog, replay: dict, budget: Budget,
                 broker_factory, args, run_dir: Path, status_path: Path,
                 run_id: str, name: str):
        self.args = args
        self.budget = budget
        self.phases: list[str] = []
        self.records: list[AgentRecord] = []
        self.log_lines: list[str] = []
        self.counts = {"planned": 0, "deployed": 0, "completed": 0,
                       "failed": 0, "blocked": 0}
        self._cfg = cfg
        self._pool = pool
        self._journal = journal
        self._events = events
        self._replay = replay
        self._broker_factory = broker_factory
        self._broker = None
        self._run_dir = run_dir
        self._status_path = status_path
        self._run_id = run_id
        self._name = name
        self._seq = 0
        self._cached_hits = 0
        self._current_phase = ""
        self._started = time.time()
        self._sem: asyncio.Semaphore | None = None

    # -- progress ----------------------------------------------------------

    def phase(self, title: str, kind: str = "") -> None:
        self._current_phase = str(title)
        self.phases.append(self._current_phase)
        self._events.emit("phase_started", phase=self._current_phase)
        alias = _PHASE_KIND_ALIASES.get((kind or "").strip().lower())
        if not alias:
            for name, rx in _PHASE_ALIASES:
                if rx.search(self._current_phase):
                    alias = name
                    break
        if alias:
            self._events.emit(alias, phase=self._current_phase)
        self.log(f"PHASE: {title}")

    def log(self, message: str) -> None:
        line = f"{_utc()} {message}"
        self.log_lines.append(line)
        logger.info("ultracode[%s]: %s", self._name, message)
        self._journal.append({"type": "log", "ts": _utc(), "message": str(message)})
        self._write_status(str(message))

    def _write_status(self, last: str = "") -> None:
        try:
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            self._status_path.write_text(json.dumps({
                "run_id": self._run_id, "name": self._name,
                "phase": self._current_phase, "phases": self.phases,
                "model_calls": self.budget.calls, "cached_hits": self._cached_hits,
                "agents_run": self._seq, "counts": dict(self.counts),
                "events_path": str(self._events.path),
                "elapsed_s": round(time.time() - self._started, 1),
                "started_utc": _utc(), "last_log": last[:200],
            }, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # -- evidence ----------------------------------------------------------

    def evidence(self, paths, max_files: int | None = None,
                 max_chars: int | None = None) -> str:
        max_files = max_files or self._cfg["evidence_max_files"]
        max_chars = max_chars or self._cfg["evidence_max_chars"]
        chunks = []
        for p in list(paths)[:max_files]:
            path = Path(p)
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
                chunks.append(f"--- {path} ---\n{_truncate(body, max_chars)}")
            except OSError as e:
                chunks.append(f"--- {path} --- (unreadable: {e})")
        return "\n\n".join(chunks)

    # -- broker-gated host checks -----------------------------------------

    def run_check(self, command: str, reason: str = "") -> dict:
        """Execute a host command through the risk-tiered broker in CRITIC
        mode: only SAFE (pure-read) commands auto-run; everything else is
        classified, ledgered, and returned as requires_approval."""
        if self._broker is None:
            self._broker = self._broker_factory()
        res = self._broker.run(command, reason=reason or f"ultracode {self._name}")
        entry = asdict(res) if hasattr(res, "__dataclass_fields__") else dict(res)
        self._journal.append({"type": "check", "ts": _utc(), **entry})
        self._events.emit("check_executed", command=entry.get("command", ""),
                          risk_tier=entry.get("risk_tier", ""),
                          status=entry.get("status", ""))
        return entry

    # -- the agent primitive ----------------------------------------------

    async def agent(self, prompt: str, *, schema: dict | None = None,
                    route: str = "", label: str = "", phase: str = "",
                    system: str = "", evidence=None,
                    max_tokens: int | None = None):
        if self._sem is None:
            self._sem = asyncio.Semaphore(int(self._cfg["max_workers"]))
        self._seq += 1
        seq = self._seq
        label = label or f"agent-{seq}"
        phase = phase or self._current_phase
        max_tokens = int(max_tokens or self._cfg["max_tokens_agent"])

        parts = []
        if system:
            parts.append(system.strip())
        if evidence:
            parts.append("EVIDENCE (read-only excerpts):\n" + self.evidence(evidence))
        parts.append(str(prompt))
        if schema:
            parts.append("Respond with ONLY a JSON value matching this schema "
                         "-- no prose, no markdown fences:\n" + json.dumps(schema))
        full_prompt = "\n\n".join(parts)
        key = _sha(f"{full_prompt}\x00{route}\x00{max_tokens}")

        rec = AgentRecord(seq=seq, label=label, phase=phase,
                          prompt_sha=_sha(full_prompt))
        agent_id = self._aid(rec)
        task_summary = " ".join(str(prompt).split())[:140]
        self.counts["planned"] += 1
        self._events.emit("agent_planned", agent_id=agent_id, agent_role=label,
                          phase=phase, task_summary=task_summary)

        cached = self._replay_take(key)
        if cached is not None:
            rec.status, rec.cached = "ok", True
            rec.text = cached.get("text", "")
            rec.parsed = cached.get("parsed")
            rec.route = cached.get("route", "")
            self._cached_hits += 1
            self.counts["deployed"] += 1
            self.counts["completed"] += 1
            artifact = self._write_agent_artifact(rec)
            self._events.emit("agent_deployed", agent_id=agent_id,
                              agent_role=label, phase=phase, cached=True)
            self._events.emit("agent_completed", agent_id=agent_id,
                              agent_role=label, phase=phase, status="completed",
                              route=rec.route, cached=True,
                              output_artifact_path=artifact)
            self.records.append(rec)
            self._journal.append(self._entry(key, rec))
            self._write_status(f"replayed {label}")
            return rec.parsed if schema else rec.text

        start = time.time()
        async with self._sem:
            self.counts["deployed"] += 1
            self._events.emit("agent_deployed", agent_id=agent_id,
                              agent_role=label, phase=phase, cached=False)
            try:
                await asyncio.to_thread(self._attempts_sync, rec, full_prompt,
                                        schema, route, max_tokens, seq)
            except BudgetExceeded as e:
                rec.status, rec.error = "error", "budget exceeded"
                rec.duration_s = round(time.time() - start, 1)
                self.counts["blocked"] += 1
                self._events.emit("agent_blocked", agent_id=agent_id,
                                  agent_role=label, phase=phase,
                                  status="blocked", error=str(e))
                self.records.append(rec)
                self._journal.append(self._entry(key, rec))
                raise
        rec.duration_s = round(time.time() - start, 1)
        self.records.append(rec)
        self._journal.append(self._entry(key, rec))
        artifact = self._write_agent_artifact(rec)
        if rec.status == "ok":
            self.counts["completed"] += 1
            self._events.emit("agent_completed", agent_id=agent_id,
                              agent_role=label, phase=phase, status="completed",
                              route=rec.route, calls=rec.calls,
                              duration_s=rec.duration_s, cached=False,
                              output_artifact_path=artifact)
        else:
            self.counts["failed"] += 1
            self._events.emit("agent_failed", agent_id=agent_id,
                              agent_role=label, phase=phase, status="failed",
                              error=rec.error, output_artifact_path=artifact)
        self._write_status(f"{rec.status}: {label}")
        if rec.status != "ok":
            self.log(f"agent {label} {rec.status}: {rec.error[:200]}")
            return None
        return rec.parsed if schema else rec.text

    @staticmethod
    def _aid(rec: AgentRecord) -> str:
        return f"a{rec.seq:03d}"

    def _write_agent_artifact(self, rec: AgentRecord) -> str:
        try:
            d = self._run_dir / "agents"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{self._aid(rec)}.json"
            p.write_text(json.dumps(asdict(rec), ensure_ascii=False, indent=1,
                                    default=str), encoding="utf-8")
            return str(p)
        except OSError:
            return ""

    def _attempts_sync(self, rec, full_prompt, schema, route, max_tokens, seq):
        attempts = (int(self._cfg["schema_retries"]) + 1) if schema else 1
        prompt = full_prompt
        last_errs: list[str] = []
        for attempt in range(attempts):
            try:
                text, used = self._chat_with_routes(prompt, route, max_tokens,
                                                    seq, rec)
            except AgentCallError as e:
                rec.status, rec.error = "error", str(e)
                return
            rec.text, rec.route = text, used
            if not schema:
                rec.status = "ok"
                return
            parsed = extract_json(text)
            last_errs = (["no JSON found in response"] if parsed is None
                         else validate_schema(parsed, schema))
            if not last_errs:
                rec.parsed, rec.status = parsed, "ok"
                return
            if attempt < attempts - 1:
                self._events.emit("agent_heartbeat", agent_id=self._aid(rec),
                                  agent_role=rec.label, phase=rec.phase,
                                  status="retrying_schema",
                                  detail="; ".join(last_errs[:3]))
            prompt = (full_prompt
                      + "\n\nYour previous response was INVALID: "
                      + "; ".join(last_errs[:5])
                      + "\nReturn ONLY corrected JSON matching the schema.")
        rec.status = "invalid_schema"
        rec.error = "; ".join(last_errs[:5])

    def _chat_with_routes(self, prompt, route, max_tokens, seq, rec):
        candidates = [route] if route else self._pool.assign(seq)
        if not candidates:
            raise AgentCallError("no routes available")
        last = None
        for cand in candidates:
            self.budget.check_or_raise()
            self.budget.calls += 1
            rec.calls += 1
            try:
                return self._pool.chat(cand, prompt, max_tokens), cand
            except RouteError as e:
                last = e
                if not route:
                    self._pool.mark_dead(cand)
                self._events.emit("agent_heartbeat", agent_id=self._aid(rec),
                                  agent_role=rec.label, phase=rec.phase,
                                  status="route_fallthrough",
                                  detail=f"{cand}: {str(e)[:160]}")
        raise AgentCallError(f"all routes failed (last: {last})")

    def _replay_take(self, key: str):
        entries = self._replay.get(key)
        return entries.pop(0) if entries else None

    def _entry(self, key: str, rec: AgentRecord) -> dict:
        return {"type": "agent", "ts": _utc(), "key": key, **asdict(rec)}

    # -- combinators -------------------------------------------------------

    async def parallel(self, thunks) -> list:
        """Run zero-arg async thunks concurrently and WAIT FOR ALL (barrier).
        A thunk that raises resolves to None; only BudgetExceeded propagates."""
        async def one(t):
            try:
                return await t()
            except BudgetExceeded:
                raise
            except Exception as e:
                self.log(f"parallel task failed: {e.__class__.__name__}: {e}")
                return None
        return list(await asyncio.gather(*(one(t) for t in thunks)))

    async def pipeline(self, items, *stages) -> list:
        """Run each item through all stages independently -- NO barrier
        between stages. Stage callables receive (prev[, item[, index]]) and
        may be sync or async. A stage that raises drops its item to None."""
        items = list(items)

        async def chain(item, idx):
            val = item
            for si, stage in enumerate(stages):
                try:
                    val = await self._apply_stage(stage, val, item, idx)
                except BudgetExceeded:
                    raise
                except Exception as e:
                    self.log(f"pipeline item {idx} dropped at stage {si}: "
                             f"{e.__class__.__name__}: {e}")
                    return None
            return val
        return list(await asyncio.gather(
            *(chain(it, i) for i, it in enumerate(items))))

    async def _apply_stage(self, stage, prev, item, idx):
        try:
            n_params = len([
                p for p in inspect.signature(stage).parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                and p.default is p.empty])
        except (TypeError, ValueError):
            n_params = 1
        out = stage(*(prev, item, idx)[:max(1, min(3, n_params))])
        if inspect.isawaitable(out):
            out = await out
        return out


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class UltracodeEngine:
    """Loads and runs one ultracode workflow. Construct with an injected
    RoutePool (mock or live) and optional broker/home for offline tests."""

    def __init__(self, pool: RoutePool, config: dict | None = None,
                 broker=None, home: Path | None = None, usage_source=None):
        self.pool = pool
        self.home = Path(home) if home else ultracode_home()
        self.cfg = dict(DEFAULTS)
        if config:
            self.cfg.update({k: v for k, v in config.items() if k in DEFAULTS})
        self._broker = broker
        self._usage_source = usage_source

    # -- script resolution ------------------------------------------------

    def scripts_dir(self) -> Path:
        return self.home / "scripts"

    def examples_dir(self) -> Path:
        return _PKG_DIR / "examples"

    def resolve(self, name_or_path: str) -> Path:
        cand = Path(name_or_path)
        if cand.is_file():
            return cand
        stem = name_or_path if name_or_path.endswith(".py") else name_or_path + ".py"
        for base in (self.scripts_dir(), self.examples_dir()):
            p = base / stem
            if p.is_file():
                return p
        raise WorkflowError(
            f"no workflow named {name_or_path!r} -- looked in "
            f"{self.scripts_dir()} and the bundled examples. "
            "Run `agent-ultra ultracode list` to see what exists.")

    def list_scripts(self) -> dict:
        out = {"saved": [], "examples": []}
        for kind, base in (("saved", self.scripts_dir()),
                           ("examples", self.examples_dir())):
            if base.is_dir():
                for p in sorted(base.glob("*.py")):
                    if p.name.startswith("_"):
                        continue
                    meta = self._peek_meta(p)
                    out[kind].append({"name": p.stem, "path": str(p),
                                      "description": meta.get("description", "")[:160]})
        return out

    @staticmethod
    def _peek_meta(path: Path) -> dict:
        import ast
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if (isinstance(node, ast.Assign)
                        and any(getattr(t, "id", "") == "META" for t in node.targets)):
                    val = ast.literal_eval(node.value)
                    return val if isinstance(val, dict) else {}
        except Exception:
            pass
        return {}

    # -- run --------------------------------------------------------------

    def run_script(self, script_path, args=None, resume_run_id: str = "") -> WorkflowReport:
        path = Path(script_path).resolve()
        source = path.read_text(encoding="utf-8")
        script_sha = _sha(source)
        module = self._load_module(path, script_sha)

        meta = getattr(module, "META", None)
        if not isinstance(meta, dict) or not str(meta.get("name", "")).strip():
            raise WorkflowError(f"{path.name}: META must be a dict with a 'name'")
        run_fn = getattr(module, "run", None)
        if not inspect.iscoroutinefunction(run_fn):
            raise WorkflowError(f"{path.name}: must define `async def run(wf)`")

        name = _slug(str(meta["name"]))
        run_id = (time.strftime("%Y%m%dT%H%M%S")
                  + f"-{name}-{_sha(os.urandom(8).hex())[:6]}")
        run_dir = self.home / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        status_path = self.home / "runs" / f".status-{run_id}.json"

        replay: dict = {}
        if resume_run_id:
            old = self.home / "runs" / resume_run_id / "journal.jsonl"
            replay = Journal.load_replay_cache(old)
            if not replay:
                logger.warning("resume: no replayable journal at %s", old)

        budget = Budget(self.cfg["max_calls"], self.cfg["token_budget"],
                        usage_source=self._usage_source)
        journal = Journal(run_dir / "journal.jsonl")
        events = EventLog(run_dir / "events.jsonl", run_id)
        broker_factory = ((lambda: self._broker) if self._broker is not None
                          else (lambda: self._default_broker()))

        wf = Workflow(cfg=self.cfg, pool=self.pool, journal=journal,
                      events=events, replay=replay, budget=budget,
                      broker_factory=broker_factory, args=args, run_dir=run_dir,
                      status_path=status_path, run_id=run_id, name=name)

        report = WorkflowReport(
            name=name, run_id=run_id, script_path=str(path),
            script_sha=script_sha, args=args, started_utc=_utc(),
            resumed_from=resume_run_id or "", artifact_dir=str(run_dir),
            journal_path=str(journal.path), events_path=str(events.path))
        journal.append({"type": "run_start", "ts": report.started_utc,
                        "run_id": run_id, "name": name, "script": str(path),
                        "script_sha": script_sha, "args": args,
                        "resumed_from": resume_run_id or ""})
        events.emit("ultracode_started", name=name, script_sha=script_sha,
                    task_summary=" ".join(str(meta.get("description", "")).split())[:140],
                    resumed_from=resume_run_id or "")

        start = time.time()
        try:
            report.result = asyncio.run(run_fn(wf))
            report.status = "completed"
        except BudgetExceeded as e:
            report.status, report.error = "aborted_budget", str(e)
        except WorkflowError as e:
            report.status, report.error = "failed", str(e)
        except (KeyboardInterrupt, asyncio.CancelledError) as e:
            report.status = "cancelled"
            report.error = f"cancelled ({e.__class__.__name__})"
        except Exception:
            report.status = "failed"
            report.error = _truncate(traceback.format_exc(), 4000)
        finally:
            try:
                status_path.unlink()
            except OSError:
                pass

        report.duration_s = round(time.time() - start, 1)
        report.model_calls = budget.calls
        report.cached_hits = wf._cached_hits
        report.agents_run = len(wf.records)
        report.tokens = {"total": budget.tokens_spent()}
        report.phases = wf.phases
        report.counts = dict(wf.counts)
        report.log = wf.log_lines[-200:]
        cap = int(self.cfg["report_agents_cap"])
        tcap = int(self.cfg["report_text_cap"])
        report.agents = [{**asdict(r), "text": _truncate(r.text, tcap)}
                         for r in wf.records[:cap]]

        event_name, final_state = FINAL_STATES.get(
            report.status, ("ultracode_failed", "FAILED"))
        report.final_state = final_state
        self._finalize(report, wf, journal, run_dir)
        events.emit(event_name, final_state=final_state, counts=dict(wf.counts),
                    error=report.error[:500], receipt_path=report.receipt_path,
                    artifact_json=report.artifact_json)
        return report

    def _finalize(self, report, wf, journal, run_dir) -> None:
        receipt = build_receipt(report, wf.records)
        try:
            (run_dir / "receipt.json").write_text(
                json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
            report.receipt_path = str(run_dir / "receipt.json")
        except OSError:
            logger.warning("could not write receipt")
        try:
            (run_dir / "report.json").write_text(
                json.dumps(asdict(report), ensure_ascii=False, indent=1,
                           default=str), encoding="utf-8")
            report.artifact_json = str(run_dir / "report.json")
        except OSError:
            logger.warning("could not write report artifact")
        journal.append({"type": "run_end", "ts": _utc(), "status": report.status,
                        "model_calls": report.model_calls, "error": report.error[:500]})
        self._append_log(report)

    def _append_log(self, report) -> None:
        log = self.home / "ultracode-log.jsonl"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": _utc(), "run_id": report.run_id, "name": report.name,
                    "status": report.status, "final_state": report.final_state,
                    "counts": report.counts, "events_path": report.events_path,
                    "model_calls": report.model_calls,
                    "cached_hits": report.cached_hits, "tokens": report.tokens,
                    "duration_s": report.duration_s,
                    "artifact_json": report.artifact_json,
                    "receipt": report.receipt_path,
                    "result_head": _truncate(
                        json.dumps(report.result, default=str, ensure_ascii=False)
                        if report.result is not None else "", 300),
                    "error": report.error[:300],
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _default_broker(self):
        from ..broker.broker import CommandBroker, CRITIC_TIERS
        return CommandBroker(ledger_path=self.home / "broker-ledger.jsonl",
                             auto_run_tiers=CRITIC_TIERS,
                             timeout=self.cfg["check_timeout"])

    @staticmethod
    def _load_module(path: Path, script_sha: str):
        from importlib.util import spec_from_file_location, module_from_spec
        mod_name = f"agent_ultra_workflow_{path.stem}_{script_sha[:12]}"
        sys.modules.pop(mod_name, None)
        spec = spec_from_file_location(mod_name, str(path))
        if spec is None or spec.loader is None:
            raise WorkflowError(f"cannot load workflow script: {path}")
        mod = module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            sys.modules.pop(mod_name, None)
            raise WorkflowError(
                f"{path.name} failed to import: {e.__class__.__name__}: {e}")
        return mod
