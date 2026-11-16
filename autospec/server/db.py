"""SQLite store (§11): workspaces, jobs, steps, events, approvals. stdlib sqlite3, one shared connection."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..core.events import Event, now_iso
from ..core.jobs import JobRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
  name TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL REFERENCES workspaces(name),
  pipeline TEXT NOT NULL,
  profile TEXT,
  inputs_json TEXT NOT NULL,
  options_json TEXT NOT NULL,
  state_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  current_step INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
  job_id TEXT NOT NULL REFERENCES jobs(id),
  idx INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT, ended_at TEXT,
  message TEXT,
  PRIMARY KEY (job_id, idx)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  type TEXT NOT NULL,
  step TEXT,
  level TEXT NOT NULL,
  message TEXT,
  data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
CREATE TABLE IF NOT EXISTS approvals (
  job_id TEXT NOT NULL,
  target TEXT NOT NULL,
  decision TEXT,
  comment TEXT,
  edited_content TEXT,
  requested_at TEXT NOT NULL,
  resolved_at TEXT,
  PRIMARY KEY (job_id, target)
);
"""

TERMINAL = {"succeeded", "failed", "cancelled"}


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._tx() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn

    def _one(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._tx() as c:
            row: sqlite3.Row | None = c.execute(sql, args).fetchone()
            return row

    def _all(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._tx() as c:
            return list(c.execute(sql, args).fetchall())

    # --- workspaces ---------------------------------------------------------------------------------
    def add_workspace(self, name: str, path: Path) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO workspaces(name, path, created_at) VALUES (?,?,?)", (name, str(path), now_iso()))

    def workspace(self, name: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM workspaces WHERE name=?", (name,))
        return dict(row) if row else None

    def workspaces(self) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT w.*, (SELECT status FROM jobs j WHERE j.workspace=w.name ORDER BY created_at DESC, id DESC"
            " LIMIT 1) AS last_status, (SELECT id FROM jobs j WHERE j.workspace=w.name"
            " ORDER BY created_at DESC, id DESC LIMIT 1) AS last_job FROM workspaces w ORDER BY w.created_at DESC")
        return [dict(r) for r in rows]

    # --- jobs -------------------------------------------------------------------------------------------
    def save_job(self, rec: JobRecord) -> None:
        """Upsert the job row and mirror ctx.state['_steps'] into the steps table."""
        rec.updated_at = now_iso()
        with self._tx() as c:
            c.execute(
                "INSERT INTO jobs(id, workspace, pipeline, profile, inputs_json, options_json, state_json, status,"
                " current_step, error, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json, status=excluded.status,"
                " current_step=excluded.current_step, error=excluded.error, updated_at=excluded.updated_at,"
                " options_json=excluded.options_json",
                (rec.id, rec.workspace, rec.pipeline, rec.profile, _dumps(rec.inputs), _dumps(rec.options),
                 _dumps(rec.state), rec.status, rec.current_step, rec.error, rec.created_at, rec.updated_at))
            steps = rec.state.get("_steps") or []
            if steps:
                c.executemany(
                    "INSERT INTO steps(job_id, idx, name, status, started_at, ended_at, message)"
                    " VALUES (?,?,?,?,?,?,?) ON CONFLICT(job_id, idx) DO UPDATE SET name=excluded.name,"
                    " status=excluded.status, started_at=excluded.started_at, ended_at=excluded.ended_at,"
                    " message=excluded.message",
                    [(rec.id, s["idx"], s["name"], s["status"], s.get("started_at"), s.get("ended_at"),
                      s.get("message")) for s in steps])

    def job(self, job_id: str) -> JobRecord | None:
        row = self._one("SELECT j.*, w.path AS ws_path FROM jobs j JOIN workspaces w ON w.name=j.workspace"
                        " WHERE j.id=?", (job_id,))
        return _record(row) if row else None

    def jobs(self, workspace: str | None = None, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT id, workspace, pipeline, profile, status, current_step, error, created_at, updated_at FROM jobs"
        cond, args = [], []
        if workspace:
            cond.append("workspace=?")
            args.append(workspace)
        if status:
            cond.append("status=?")
            args.append(status)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        return [dict(r) for r in self._all(sql, (*args, limit))]

    def steps(self, job_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT idx, name, status, started_at, ended_at, message FROM steps"
                                           " WHERE job_id=? ORDER BY idx", (job_id,))]

    def set_status(self, job_id: str, status: str, *, error: str | None = None,
                   current_step: int | None = None) -> None:
        with self._tx() as c:
            c.execute("UPDATE jobs SET status=?, error=COALESCE(?, error), current_step=COALESCE(?, current_step),"
                      " updated_at=? WHERE id=?", (status, error, current_step, now_iso(), job_id))

    def claim_next(self) -> str | None:
        """Atomically move the oldest queued job to running (the DB is the queue, §12)."""
        with self._tx() as c:
            row = c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1").fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (now_iso(), row["id"]))
            return str(row["id"])

    def mark_interrupted(self) -> list[str]:
        with self._tx() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM jobs WHERE status='running'").fetchall()]
            c.execute("UPDATE jobs SET status='interrupted', updated_at=? WHERE status='running'", (now_iso(),))
            c.execute("UPDATE steps SET status='interrupted' WHERE status='running'")
        return ids

    # --- events -----------------------------------------------------------------------------------------
    def add_event(self, ev: Event) -> int:
        with self._tx() as c:
            cur = c.execute("INSERT INTO events(job_id, ts, type, step, level, message, data_json)"
                            " VALUES (?,?,?,?,?,?,?)",
                            (ev.job_id, ev.ts, ev.type, ev.step, ev.level, ev.message, _dumps(ev.data)))
            return int(cur.lastrowid or 0)

    def events(self, job_id: str, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT ?", (job_id, after, limit))
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d.pop("data_json") or "{}")
            out.append(d)
        return out

    # --- approvals ----------------------------------------------------------------------------------------
    def request_approval(self, job_id: str, target: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO approvals(job_id, target, requested_at) VALUES (?,?,?)"
                      " ON CONFLICT(job_id, target) DO UPDATE SET decision=NULL, comment=NULL, edited_content=NULL,"
                      " requested_at=excluded.requested_at, resolved_at=NULL", (job_id, target, now_iso()))

    def resolve_approval(self, job_id: str, target: str, decision: str, comment: str,
                         edited: str | None) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO approvals(job_id, target, decision, comment, edited_content, requested_at,"
                      " resolved_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(job_id, target) DO UPDATE SET"
                      " decision=excluded.decision, comment=excluded.comment,"
                      " edited_content=excluded.edited_content, resolved_at=excluded.resolved_at",
                      (job_id, target, decision, comment, edited, now_iso(), now_iso()))

    def approvals(self, job_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT * FROM approvals WHERE job_id=? ORDER BY requested_at",
                                           (job_id,))]


class DbEventSink:
    """Event sink for the worker: every event goes to the events table and gets its DB id (used by SSE)."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def __call__(self, ev: Event) -> None:
        ev.id = self.store.add_event(ev)
        if ev.type == "approval.required" and ev.data.get("target"):
            self.store.request_approval(ev.job_id, str(ev.data["target"]))


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _record(row: sqlite3.Row) -> JobRecord:
    return JobRecord(id=row["id"], workspace=row["workspace"], workspace_root=row["ws_path"],
                     pipeline=row["pipeline"], profile=row["profile"], inputs=json.loads(row["inputs_json"]),
                     options=json.loads(row["options_json"]), state=json.loads(row["state_json"] or "{}"),
                     status=row["status"], current_step=row["current_step"], error=row["error"],
                     created_at=row["created_at"], updated_at=row["updated_at"])


def record_dict(rec: JobRecord) -> dict[str, Any]:
    return asdict(rec)
