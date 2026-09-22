"""SQLite persistence: runs, steps, audit (append-only), approval requests, SSE events.

Concurrency model: a SINGLE shared sqlite connection guarded by a re-entrant lock.
All DB access is serialized — this is safe (ops are short) and avoids per-thread
connection deadlocks under the asyncio workflow + request-handler threads.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

from .config import settings

# Canonical 10 workflow steps (video order).
STEP_NAMES = [
    "Identity validated",
    "Case metadata resolved",
    "CRM profile fetched",
    "Credit exposure fetched",
    "Transaction behavior analyzed",
    "Compliance status checked",
    "Prior memo reviewed",
    "Draft memo created",
    "Submission attempted",
    "Approval decision",
]

RUN_ACTIVE = ("PENDING", "RUNNING", "AWAITING_APPROVAL")
RUN_TERMINAL = ("COMPLETED", "REJECTED", "FAILED")

_db: sqlite3.Connection | None = None
_db_lock = threading.RLock()  # serializes ALL sqlite access

_sse: dict[str, list[dict]] = {}
_subscribers: dict[str, list] = {}
_sse_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _conn() -> sqlite3.Connection:
    global _db
    with _db_lock:
        if _db is None:
            os.makedirs(settings.data_dir, exist_ok=True)
            _db = sqlite3.connect(settings.sqlite_path, check_same_thread=False, timeout=30)
            _db.row_factory = sqlite3.Row
            _db.execute("PRAGMA journal_mode=WAL")
        return _db


def init_db() -> None:
    c = _conn()
    with _db_lock:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                requested_emp_id TEXT NOT NULL,
                amount_usd INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                request_id TEXT,
                memo_draft_path TEXT,
                memo_official_path TEXT,
                approved_by TEXT,
                approval_role TEXT,
                reject_reason TEXT,
                fail_reason TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS steps (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                name TEXT NOT NULL,
                state TEXT NOT NULL,
                ts TEXT,
                PRIMARY KEY (run_id, seq)
            );
            CREATE TABLE IF NOT EXISTS audit (
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT,
                reason TEXT,
                meta TEXT
            );
            CREATE TABLE IF NOT EXISTS approval_requests (
                request_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                decision TEXT,
                ttl_hours INTEGER,
                decided_by TEXT,
                decided_at TEXT,
                via TEXT,
                reason TEXT
            );
            """
        )
        c.commit()
        _ensure_audit_columns(c)


def _ensure_audit_columns(c: sqlite3.Connection) -> None:
    """Idempotent migration for pre-existing databases (append-only columns)."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(audit)")}
    if "reason" not in cols:
        c.execute("ALTER TABLE audit ADD COLUMN reason TEXT")
    if "meta" not in cols:
        c.execute("ALTER TABLE audit ADD COLUMN meta TEXT")
    acols = {r[1] for r in c.execute("PRAGMA table_info(approval_requests)")}
    if "via" not in acols:
        c.execute("ALTER TABLE approval_requests ADD COLUMN via TEXT")
    if "reason" not in acols:
        c.execute("ALTER TABLE approval_requests ADD COLUMN reason TEXT")
    c.commit()


def reset_for_tests() -> None:
    """Drop the shared connection (tests start a fresh DB)."""
    global _db
    with _db_lock:
        if _db is not None:
            _db.close()
            _db = None
    _sse.clear()
    _subscribers.clear()


# ---------- runs ----------

def create_run(case_id: str, requested_by: str, emp_id: str, amount_usd: int) -> str:
    c = _conn()
    with _db_lock:
        # Monotonic, collision-safe id: derive the next number from the max existing
        # suffix. (The previous count+wallclock formula produced duplicate ids once the
        # time offset rolled over on a persistent data dir -> UNIQUE constraint errors.)
        row = c.execute("SELECT MAX(CAST(SUBSTR(run_id, 5) AS INTEGER)) AS m FROM runs").fetchone()
        base = (row["m"] or 0) + 1
        run_id = None
        for _ in range(10000):
            cand = f"RUN-{base:05d}"
            if c.execute("SELECT 1 FROM runs WHERE run_id=?", (cand,)).fetchone() is None:
                run_id = cand
                break
            base += 1
        if run_id is None:  # effectively impossible; fall back to a unique suffix
            run_id = "RUN-" + uuid.uuid4().hex[:5].upper()
        c.execute(
            "INSERT INTO runs (run_id, case_id, status, requested_by, requested_emp_id,"
            " amount_usd, started_at) VALUES (?,?,?,?,?,?,?)",
            (run_id, case_id, "PENDING", requested_by, emp_id, amount_usd, _now()),
        )
        for i, name in enumerate(STEP_NAMES, start=1):
            c.execute("INSERT INTO steps (run_id, seq, name, state) VALUES (?,?,?,?)",
                      (run_id, i, name, "pending"))
        c.commit()
    _sse[run_id] = []
    _subscribers[run_id] = []
    return run_id


def get_run(run_id: str) -> dict | None:
    c = _conn()
    with _db_lock:
        row = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def set_run_status(run_id: str, status: str, **fields) -> None:
    c = _conn()
    sets = ["status=?"]
    vals: list = [status]
    for k, v in fields.items():
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(run_id)
    with _db_lock:
        c.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id=?", vals)
        c.commit()


def active_run_for_case(case_id: str) -> dict | None:
    c = _conn()
    with _db_lock:
        rows = c.execute(
            "SELECT * FROM runs WHERE case_id=? AND status IN (?,?,?) ORDER BY started_at DESC",
            (case_id, "PENDING", "RUNNING", "AWAITING_APPROVAL")).fetchall()
    return dict(rows[0]) if rows else None


def list_runs(limit: int = 50) -> list[dict]:
    c = _conn()
    with _db_lock:
        rows = c.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------- steps ----------

def get_steps(run_id: str) -> list[dict]:
    c = _conn()
    with _db_lock:
        rows = c.execute(
            "SELECT seq, name, state, ts FROM steps WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
    return [dict(r) for r in rows]


def set_step(run_id: str, seq: int, state: str) -> None:
    c = _conn()
    with _db_lock:
        c.execute("UPDATE steps SET state=?, ts=? WHERE run_id=? AND seq=?", (state, _now(), run_id, seq))
        _advance_states_locked(c, run_id)
        c.commit()
    publish(run_id, {"type": "step", "seq": seq, "state": state})


def _advance_states_locked(c: sqlite3.Connection, run_id: str) -> None:
    """Caller holds _db_lock. Keep a coherent visual state: done up to N, one active."""
    done = c.execute("SELECT MAX(seq) AS m FROM steps WHERE run_id=? AND state='done'",
                     (run_id,)).fetchone()["m"] or 0
    failed = c.execute("SELECT MIN(seq) AS m FROM steps WHERE run_id=? AND state='failed'",
                       (run_id,)).fetchone()["m"]
    active = failed if failed else (done + 1 if done < len(STEP_NAMES) else None)
    for r in c.execute("SELECT seq FROM steps WHERE run_id=?", (run_id,)):
        s = r["seq"]
        if s <= done:
            cur = "done"
        elif s == active:
            cur = "failed" if failed == s else "active"
        else:
            cur = "pending"
        c.execute("UPDATE steps SET state=? WHERE run_id=? AND seq=? AND state<>?",
                  (cur, run_id, s, cur))


# ---------- audit (append-only) ----------

def add_audit(run_id: str, actor: str, action: str, detail: str = "",
              reason: str = "", meta: dict | None = None) -> None:
    c = _conn()
    with _db_lock:
        c.execute("INSERT INTO audit (run_id, ts, actor, action, detail, reason, meta) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (run_id, _now(), actor, action, detail, reason or None,
                   json.dumps(meta) if meta else None))
        c.commit()
    publish(run_id, {"type": "audit", "actor": actor, "action": action,
                     "detail": detail, "reason": reason,
                     "meta": meta or {}})


def get_audit(run_id: str) -> list[dict]:
    c = _conn()
    with _db_lock:
        rows = c.execute(
            "SELECT ts, actor, action, detail, reason, meta FROM audit "
            "WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
        except (TypeError, ValueError):
            d["meta"] = {}
        out.append(d)
    return out


# ---------- approval requests ----------

def create_approval_request(run_id: str, ttl_hours: int) -> str:
    request_id = str(uuid.uuid4())
    now = time.time()
    c = _conn()
    with _db_lock:
        c.execute(
            "INSERT INTO approval_requests (request_id, run_id, created_at, expires_at,"
            " status, ttl_hours) VALUES (:rid, :run, :created, :expires, 'pending', :ttl)",
            {"rid": request_id, "run": run_id, "created": _now(),
             "expires": now + ttl_hours * 3600, "ttl": ttl_hours})
        c.commit()
    return request_id


def get_approval_request(request_id: str) -> dict | None:
    c = _conn()
    with _db_lock:
        row = c.execute("SELECT * FROM approval_requests WHERE request_id=?", (request_id,)).fetchone()
    return dict(row) if row else None


def request_for_run(run_id: str) -> dict | None:
    c = _conn()
    with _db_lock:
        rows = c.execute(
            "SELECT * FROM approval_requests WHERE run_id=? ORDER BY created_at DESC", (run_id,)).fetchall()
    return dict(rows[0]) if rows else None


def record_decision(request_id: str, decision: str, ttl_hours: int, decided_by: str,
                    via: str = "signed email link", reason: str = "") -> str:
    """Single-use: only the first decision is recorded; repeats return the existing."""
    status = "approved" if decision == "approve" else "rejected"
    c = _conn()
    with _db_lock:
        row = c.execute("SELECT status, decision, decided_at FROM approval_requests WHERE request_id=?",
                        (request_id,)).fetchone()
        if row is None:
            return "not_found"
        if row["status"] in ("approved", "rejected"):
            return "already_recorded"
        c.execute(
            "UPDATE approval_requests SET status=?, decision=?, ttl_hours=?, decided_by=?,"
            " decided_at=?, expires_at=?, via=?, reason=? WHERE request_id=?",
            (status, decision, ttl_hours, decided_by, _now(), time.time() + ttl_hours * 3600,
             via, reason or None, request_id))
        c.commit()
    return "recorded"


# ---------- SSE ----------

def publish(run_id: str, event: dict) -> None:
    with _sse_lock:
        log = _sse.setdefault(run_id, [])
        event = {"id": len(log) + 1, "ts": _now(), **event}
        log.append(event)
        queues = list(_subscribers.get(run_id, []))
    for q in queues:
        try:
            q.put_nowait(event)
        except Exception:
            pass


def snapshot(run_id: str) -> dict:
    run = get_run(run_id)
    steps = get_steps(run_id)
    out = {"run": run, "steps": steps, "audit": get_audit(run_id)}
    if run:
        out["approval_request"] = request_for_run(run_id)
    return out


def subscribe(run_id: str, last_event_id: int = 0) -> tuple[list[dict], object]:
    import asyncio
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    with _sse_lock:
        _subscribers.setdefault(run_id, []).append(q)
        replay = list(_sse.get(run_id, []))
    return [e for e in replay if e["id"] > last_event_id], q


def unsubscribe(run_id: str, q) -> None:
    with _sse_lock:
        subs = _subscribers.get(run_id, [])
        if q in subs:
            subs.remove(q)
