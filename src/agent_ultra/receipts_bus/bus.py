"""ReceiptsBus — the SQLite-backed store, resolver, and gate audit chain.

Store
-----
SQLite in WAL mode with the DEFAULT auto-checkpoint left in place (we never
set ``wal_autocheckpoint=0``, which would grow the WAL without bound). Writes
take a ``busy_timeout``; reads get one bounded retry. A read that still fails
raises :class:`BusUnavailable` — it never degrades to an empty list, because a
caller cannot tell "no receipts" from "the store was unreachable" and would
wrongly conclude a claim is unsupported.

Resolve + binding
-----------------
:meth:`resolve` returns candidate receipts scoped to the claim's session /
workspace / window, optionally federating other in-place sources through
injected reader callables (so the kit stays store-agnostic). :meth:`binds`
applies the binding rule that decides which candidates actually back a claim.

Gate audit
----------
:meth:`append_audit` writes a hash-chained row; :meth:`verify_audit` walks the
chain and reports the first broken link.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .envelope import (
    build_envelope,
    canonical_body,
    receipt_sha256,
    verify_hashes,
    ACCEPTABLE_VERDICTS,
    COMPLETION_KINDS,
)


class BusUnavailable(RuntimeError):
    """The store could not be read. Distinct from 'no receipts found' — a
    caller MUST NOT treat this as an empty result."""


# A reader callable federates an external, in-place source into resolve().
# It takes a claim context dict and returns a list of envelope dicts.
FederatedReader = Callable[[dict], list]


@dataclass
class Candidate:
    """A receipt that could bind to a claim, annotated with its origin."""
    receipt: dict
    origin: str = "bus"   # "bus" or the name of a federated reader


@dataclass
class ReceiptsBus:
    """Authenticated receipt index over a SQLite store.

    All host-specific values are parameters:

      db_path            store location (default: in-memory shared cache).
      key_path           per-install HMAC key file. Read once at construction.
                         The HOST is responsible for fencing this path from
                         untrusted writers (see the honest residual below).
      ttl_seconds        receipts older than this are evicted on write.
      per_session_cap    keep at most this many receipts per session.
      busy_timeout_ms    SQLite busy timeout for writes.
      read_retries       bounded read retries before BusUnavailable.
      freshness_seconds  a verify-command candidate is only fresh within this.

    Honest residual: a same-user process that can READ ``key_path`` can forge
    an authentic receipt. Closing that needs an out-of-process signer, which
    is out of scope for v1.
    """

    db_path: str | Path = ""
    key_path: str | Path = ""
    ttl_seconds: int = 30 * 24 * 3600
    per_session_cap: int = 500
    busy_timeout_ms: int = 5000
    read_retries: int = 1
    freshness_seconds: int = 24 * 3600
    federated_readers: dict[str, FederatedReader] = field(default_factory=dict)

    _key: bytes = field(default=b"", init=False, repr=False)

    def __post_init__(self) -> None:
        self._key = self._load_key()
        self._init_db()

    # -- key custody -------------------------------------------------------
    def _load_key(self) -> bytes:
        if not self.key_path:
            # No key configured: the bus can still store/verify integrity, but
            # nothing it writes is authentic. Enforce-mode consumers reject.
            return b""
        p = Path(self.key_path)
        try:
            raw = p.read_bytes()
        except OSError:
            return b""
        return raw.strip() or b""

    @property
    def has_key(self) -> bool:
        return bool(self._key)

    # -- store -------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        target = str(self.db_path) if self.db_path else \
            "file:receipts_bus?mode=memory&cache=shared"
        uri = bool(self.db_path) is False
        conn = sqlite3.connect(target, uri=uri, timeout=self.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")          # default autocheckpoint
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        return conn

    # keep one connection alive for the in-memory shared-cache case, so the
    # schema/data survive between calls within a process.
    _keepalive: sqlite3.Connection | None = field(default=None, init=False,
                                                  repr=False)

    def _init_db(self) -> None:
        conn = self._connect()
        if not self.db_path:
            self._keepalive = conn
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS receipts (
                receipt_id   TEXT PRIMARY KEY,
                kind         TEXT NOT NULL,
                run_id       TEXT,
                session_id   TEXT,
                workspace    TEXT,
                ts           TEXT,
                created_at   REAL,
                claim_sha256 TEXT,
                verdict      TEXT,
                body         TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_receipts_session
                ON receipts(session_id);
            CREATE INDEX IF NOT EXISTS ix_receipts_claim
                ON receipts(claim_sha256);
            CREATE TABLE IF NOT EXISTS gate_audit (
                event_id    TEXT PRIMARY KEY,
                seq         INTEGER,
                gate        TEXT NOT NULL,
                mode        TEXT,
                decision    TEXT,
                source      TEXT,
                reason      TEXT,
                ts          TEXT,
                prev_sha256 TEXT,
                row_sha256  TEXT
            );
            """
        )
        conn.commit()
        if self.db_path:
            conn.close()

    def _read_conn(self) -> sqlite3.Connection:
        """A read connection with a bounded retry; BusUnavailable on failure."""
        last: Exception | None = None
        for attempt in range(self.read_retries + 1):
            try:
                if not self.db_path and self._keepalive is not None:
                    return self._keepalive
                return self._connect()
            except sqlite3.Error as e:   # pragma: no cover - timing dependent
                last = e
                time.sleep(0.02 * (attempt + 1))
        raise BusUnavailable(f"receipts store unreadable: {last}")

    # -- write -------------------------------------------------------------
    def append(
        self, *, kind: str, actor: str, verdict: str, **fields
    ) -> dict:
        """Build, sign (if a key is configured), and store a receipt. Returns
        the stored envelope. Enum validation happens in ``build_envelope``."""
        env = build_envelope(kind=kind, actor=actor, verdict=verdict,
                             key=self._key or None, **fields)
        self.put(env)
        return env

    def put(self, env: dict) -> None:
        """Store an already-built envelope (used by federation/import)."""
        conn = self._connect() if self.db_path else self._keepalive
        assert conn is not None
        conn.execute(
            "INSERT OR REPLACE INTO receipts (receipt_id, kind, run_id, "
            "session_id, workspace, ts, created_at, claim_sha256, verdict, body)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (env["receipt_id"], env["kind"], env.get("run_id", ""),
             env.get("session_id", ""), env.get("workspace", ""),
             env.get("ts", ""), time.time(), env.get("claim_sha256", ""),
             env["verdict"], json.dumps(env, sort_keys=True)),
        )
        conn.commit()
        self._evict(conn, env.get("session_id", ""))
        if self.db_path:
            conn.close()

    def _evict(self, conn: sqlite3.Connection, session_id: str) -> None:
        cutoff = time.time() - self.ttl_seconds
        conn.execute("DELETE FROM receipts WHERE created_at < ?", (cutoff,))
        if session_id:
            conn.execute(
                "DELETE FROM receipts WHERE session_id = ? AND receipt_id NOT IN "
                "(SELECT receipt_id FROM receipts WHERE session_id = ? "
                " ORDER BY created_at DESC LIMIT ?)",
                (session_id, session_id, self.per_session_cap),
            )
        conn.commit()

    # -- read --------------------------------------------------------------
    def get(self, receipt_id: str) -> dict | None:
        conn = self._read_conn()
        try:
            row = conn.execute(
                "SELECT body FROM receipts WHERE receipt_id = ?",
                (receipt_id,)).fetchone()
        except sqlite3.Error as e:
            raise BusUnavailable(f"receipts store unreadable: {e}")
        finally:
            if self.db_path:
                conn.close()
        return json.loads(row["body"]) if row else None

    def list(self, *, session_id: str = "", workspace: str = "",
             kind: str = "", limit: int = 200) -> list[dict]:
        """List stored receipts, newest first. Raises BusUnavailable on a
        store error — never returns [] to hide an unreadable store."""
        clauses, params = [], []
        if session_id:
            clauses.append("session_id = ?"); params.append(session_id)
        if workspace:
            clauses.append("workspace = ?"); params.append(workspace)
        if kind:
            clauses.append("kind = ?"); params.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        conn = self._read_conn()
        try:
            rows = conn.execute(
                f"SELECT body FROM receipts{where} ORDER BY created_at DESC "
                f"LIMIT ?", (*params, limit)).fetchall()
        except sqlite3.Error as e:
            raise BusUnavailable(f"receipts store unreadable: {e}")
        finally:
            if self.db_path:
                conn.close()
        return [json.loads(r["body"]) for r in rows]

    # -- verify ------------------------------------------------------------
    def verify(self, receipt_id: str) -> dict:
        """Return ``{ok, authentic, integrity, refs_ok, receipt}``.

        ``authentic`` is separate from ``integrity`` (see envelope). ``ok`` is
        the enforce-mode answer: integrity AND authentic AND refs resolve.
        """
        receipt = self.get(receipt_id)
        if receipt is None:
            return {"ok": False, "authentic": False, "integrity": False,
                    "refs_ok": False, "receipt": None}
        integrity, authentic = verify_hashes(receipt, self._key or None)
        refs_ok = self._refs_ok(receipt)
        return {"ok": bool(integrity and authentic and refs_ok),
                "authentic": authentic, "integrity": integrity,
                "refs_ok": refs_ok, "receipt": receipt}

    def _refs_ok(self, receipt: dict) -> bool:
        """Every referenced artifact path that looks local must exist."""
        for p in receipt.get("artifact_paths", []):
            try:
                if p and not Path(p).exists():
                    return False
            except OSError:
                return False
        return True

    # -- resolve + binding -------------------------------------------------
    def resolve(self, claim_ctx: dict) -> list[Candidate]:
        """Candidates scoped by session/workspace/window, plus any federated
        readers. Origin-annotated. Raises BusUnavailable on a store error."""
        rows = self.list(session_id=claim_ctx.get("session_id", ""),
                         workspace=claim_ctx.get("workspace", ""),
                         limit=claim_ctx.get("limit", 200))
        out = [Candidate(receipt=r, origin="bus") for r in rows]
        for name, reader in self.federated_readers.items():
            try:
                for env in reader(claim_ctx) or []:
                    out.append(Candidate(receipt=env, origin=name))
            except Exception:
                # A misbehaving federated reader must not sink the resolve;
                # it simply contributes no candidates.
                continue
        return out

    def binds(self, claim_ctx: dict, candidate: Candidate) -> tuple[bool, str]:
        """The binding rule. Returns ``(binds, clause)``.

        A candidate binds a COMPLETION claim only by:
          * an explicitly-cited receipt id (``task_ref.cited`` / ``cited``), or
          * a canonical verify-command match with scope coverage + freshness, or
          * a ``task_ref`` match.

        Never binds:
          * a command-kind candidate to a completion claim alone,
          * a verifier-kind candidate unless its claim_sha256 == the claim's,
          * manual / repair / route_health candidates to completion.

        Weak-but-present evidence (uncovered / stale / ad-hoc-equivalent)
        returns the distinct ``"ledger-weak"`` clause, NOT a pass.
        """
        r = candidate.receipt
        kind = r.get("kind", "")

        # authenticity/integrity is a precondition to binding anything
        integrity, authentic = verify_hashes(r, self._key or None)
        if not (integrity and authentic):
            return False, "unauthenticated"

        # a non-acceptable verdict can never satisfy a gate
        if r.get("verdict") not in ACCEPTABLE_VERDICTS:
            return False, "non-acceptable-verdict"

        # 1. explicit citation always wins (still must be authentic + acceptable)
        cited = claim_ctx.get("cited") or claim_ctx.get("task_ref", {}).get("cited")
        if cited and cited == r.get("receipt_id"):
            return True, "cited"

        # kinds that never satisfy completion
        if kind in ("command", "manual", "repair", "route_health"):
            return False, "kind-cannot-complete" if kind != "command" \
                else "command-not-completion"

        # 2. verifier candidate: must match the claim hash exactly
        if kind == "verifier":
            if claim_ctx.get("claim_sha256") and \
                    r.get("claim_sha256") == claim_ctx["claim_sha256"]:
                return True, "verifier-claim-match"
            return False, "verifier-claim-mismatch"

        # 3. canonical verify-command match with scope coverage + freshness
        want_cmd = claim_ctx.get("verify_command", "")
        if want_cmd and r.get("canonical_command") == want_cmd:
            if not self._scope_covers(claim_ctx, r):
                return False, "ledger-weak"     # uncovered scope
            if not self._fresh(r):
                return False, "ledger-weak"     # stale
            return True, "verify-command"

        # 4. task_ref match (issue id / external ref)
        want_ref = claim_ctx.get("task_ref", {})
        got_ref = r.get("task_ref", {})
        common = {k: want_ref[k] for k in want_ref
                  if k in got_ref and want_ref[k] and got_ref[k] == want_ref[k]}
        if common:
            if kind not in COMPLETION_KINDS:
                return False, "kind-cannot-complete"
            return True, "task-ref"

        # present but not a clean bind
        if want_cmd or want_ref:
            return False, "ledger-weak"
        return False, "no-match"

    def _scope_covers(self, claim_ctx: dict, receipt: dict) -> bool:
        cs, rs = claim_ctx.get("session_id", ""), receipt.get("session_id", "")
        cw, rw = claim_ctx.get("workspace", ""), receipt.get("workspace", "")
        if cs and rs and cs != rs:
            return False
        if cw and rw and cw != rw:
            return False
        return True

    def _fresh(self, receipt: dict) -> bool:
        ts = receipt.get("ts", "")
        if not ts:
            return False
        try:
            t = time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            age = time.time() - calendar.timegm(t)   # ts is UTC (gmtime)
        except (ValueError, OverflowError):
            return False
        return age <= self.freshness_seconds

    def why(self, claim_ctx: dict) -> dict:
        """Explain resolution: every candidate with its binding clause."""
        results = []
        binding = False
        for cand in self.resolve(claim_ctx):
            ok, clause = self.binds(claim_ctx, cand)
            binding = binding or ok
            results.append({"receipt_id": cand.receipt.get("receipt_id"),
                            "kind": cand.receipt.get("kind"),
                            "origin": cand.origin, "binds": ok,
                            "clause": clause})
        return {"binds": binding, "candidates": results}

    # -- gate_audit (hash chain) ------------------------------------------
    def _last_audit(self, conn: sqlite3.Connection) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT seq, row_sha256 FROM gate_audit ORDER BY seq DESC LIMIT 1"
        ).fetchone()

    @staticmethod
    def _audit_row_sha(seq, gate, mode, decision, source, reason, ts,
                       prev) -> str:
        body = json.dumps({"seq": seq, "gate": gate, "mode": mode,
                           "decision": decision, "source": source,
                           "reason": reason, "ts": ts, "prev_sha256": prev},
                          sort_keys=True)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def append_audit(self, gate: str, mode: str, decision: str,
                     source: str, reason: str) -> str:
        """Append a hash-chained audit row; returns the event_id."""
        conn = self._connect() if self.db_path else self._keepalive
        assert conn is not None
        last = self._last_audit(conn)
        seq = (last["seq"] + 1) if last else 0
        prev = last["row_sha256"] if last else ""
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        event_id = uuid.uuid4().hex
        row_sha = self._audit_row_sha(seq, gate, mode, decision, source,
                                      reason, ts, prev)
        conn.execute(
            "INSERT INTO gate_audit (event_id, seq, gate, mode, decision, "
            "source, reason, ts, prev_sha256, row_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (event_id, seq, gate, mode, decision, source, reason, ts, prev,
             row_sha))
        conn.commit()
        if self.db_path:
            conn.close()
        return event_id

    def verify_audit(self, gate: str = "") -> dict:
        """Recompute the chain. Returns ``{ok, first_broken}`` (first_broken is
        the event_id of the first row whose hash/link does not recompute)."""
        conn = self._read_conn()
        try:
            where = " WHERE gate = ?" if gate else ""
            params = (gate,) if gate else ()
            rows = conn.execute(
                f"SELECT * FROM gate_audit{where} ORDER BY seq ASC",
                params).fetchall()
        except sqlite3.Error as e:
            raise BusUnavailable(f"gate_audit unreadable: {e}")
        finally:
            if self.db_path:
                conn.close()
        prev = ""
        for row in rows:
            expect = self._audit_row_sha(
                row["seq"], row["gate"], row["mode"], row["decision"],
                row["source"], row["reason"], row["ts"], row["prev_sha256"])
            if row["prev_sha256"] != prev or row["row_sha256"] != expect:
                return {"ok": False, "first_broken": row["event_id"]}
            prev = row["row_sha256"]
        return {"ok": True, "first_broken": None}

    def get_audit_event(self, event_id: str) -> dict | None:
        conn = self._read_conn()
        try:
            row = conn.execute(
                "SELECT * FROM gate_audit WHERE event_id = ?",
                (event_id,)).fetchone()
        except sqlite3.Error as e:
            raise BusUnavailable(f"gate_audit unreadable: {e}")
        finally:
            if self.db_path:
                conn.close()
        return dict(row) if row else None
