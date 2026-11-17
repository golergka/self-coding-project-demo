"""Durable event queue and workflow routing for automation runs."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from app.contracts import Event, RunRequest, RunResult, JSON


class Runs:
    LEASE_SECONDS = 30
    MAX_ATTEMPTS = 3

    def __init__(self, db: Path, get_project: Callable[[str], dict], execute: Callable[[RunRequest], RunResult]):
        self.db, self.get_project, self.execute = db, get_project, execute
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, environment TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, commit_id TEXT NOT NULL, active INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_conversation ON workflows
                    (project_id, environment, conversation_id) WHERE active = 1;
                CREATE TABLE IF NOT EXISTS runs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    workflow_id TEXT, project_id TEXT NOT NULL, environment TEXT NOT NULL,
                    commit_id TEXT, test_session_id TEXT, event_id TEXT NOT NULL,
                    event TEXT NOT NULL, status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                    logs TEXT NOT NULL DEFAULT '[]', error TEXT, due REAL NOT NULL,
                    lease_token TEXT, lease_until REAL,
                    UNIQUE(project_id, environment, event_id)
                );
                CREATE TABLE IF NOT EXISTS schedules (
                    workflow_id TEXT NOT NULL, key TEXT NOT NULL, run_id TEXT NOT NULL,
                    PRIMARY KEY (workflow_id, key)
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    project_id TEXT NOT NULL, kind TEXT NOT NULL, PRIMARY KEY(project_id, kind)
                );
            """)

    def _connect(self):
        c = sqlite3.connect(self.db, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def _record(row):
        return {"id": row["id"], "workflow_id": row["workflow_id"],
                "project_id": row["project_id"], "commit": row["commit_id"],
                "test_session_id": row["test_session_id"], "event": json.loads(row["event"]),
                "status": row["status"], "attempt": row["attempt"],
                "logs": json.loads(row["logs"]), "error": row["error"]}

    def enqueue(self, project_id: str, event: Event, *, test_session_id: str | None = None, commit: str | None = None) -> dict:
        if not isinstance(event, Event) or event.kind not in ("comment", "dm") or not event.id or not event.conversation_id:
            raise ValueError("Invalid event")
        if (test_session_id is None) != (commit is None) or test_session_id == "" or commit == "":
            raise ValueError("Test events require a session and commit")
        environment = test_session_id or ""
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute("SELECT * FROM runs WHERE project_id=? AND environment=? AND event_id=?", (project_id, environment, event.id)).fetchone()
            if existing:
                return self._record(existing)
            project = self.get_project(project_id)
            workflow = c.execute("SELECT * FROM workflows WHERE project_id=? AND environment=? AND conversation_id=? AND active=1", (project_id, environment, event.conversation_id)).fetchone()
            if workflow is None and event.kind == "comment":
                revision = commit if test_session_id else project.get("main_commit")
                if not revision:
                    raise ValueError("Project main has no commit")
                workflow_id = uuid.uuid4().hex
                c.execute("INSERT INTO workflows VALUES (?, ?, ?, ?, ?, 1)", (workflow_id, project_id, environment, event.conversation_id, revision))
                workflow = c.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            run_id = uuid.uuid4().hex
            c.execute("""INSERT INTO runs (id,workflow_id,project_id,environment,commit_id,test_session_id,event_id,event,status,due)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (run_id, workflow["id"] if workflow else None, project_id, environment,
                workflow["commit_id"] if workflow else None, test_session_id, event.id, json.dumps(asdict(event)),
                "queued" if workflow else "completed", time.time()))
            return self._record(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    @staticmethod
    def _owns_attempt(row, run):
        return (row is not None and row["status"] == "running"
                and row["attempt"] == run.attempt and row["lease_until"] > time.time()
                and row["workflow_id"] == run.workflow_id and row["project_id"] == run.project_id
                and row["test_session_id"] == run.test_session_id and row["commit_id"] == run.commit)

    @contextmanager
    def mutation(self, run):
        """Serialize short mutations with lease takeover, never with network waits."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run.id,)).fetchone()
            if not self._owns_attempt(row, run):
                raise ValueError("Run attempt no longer owns its lease")
            yield connection

    def schedule(self, run: RunRequest, delay_seconds: float, payload: JSON, key: str) -> dict:
        if not key or not isinstance(delay_seconds, (int, float)) or not 0 <= delay_seconds < float("inf"):
            raise ValueError("Invalid schedule")
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            source = c.execute("SELECT * FROM runs WHERE id=?", (run.id,)).fetchone()
            if not self._owns_attempt(source, run):
                raise ValueError("Run attempt no longer owns its lease")
            existing = c.execute("SELECT run_id FROM schedules WHERE workflow_id=? AND key=?", (run.workflow_id, key)).fetchone()
            if existing:
                return self._record(c.execute("SELECT * FROM runs WHERE id=?", (existing["run_id"],)).fetchone())
            event = Event(uuid.uuid4().hex, "schedule", json.loads(source["event"])["conversation_id"], payload)
            run_id = uuid.uuid4().hex
            c.execute("""INSERT INTO runs (id,workflow_id,project_id,environment,commit_id,test_session_id,event_id,event,status,due)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)""", (run_id, run.workflow_id, run.project_id, source["environment"], run.commit, run.test_session_id, event.id, json.dumps(asdict(event)), time.time() + delay_seconds))
            c.execute("INSERT INTO schedules VALUES (?, ?, ?)", (run.workflow_id, key, run_id))
            return self._record(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def work_once(self) -> bool:
        now = time.time()
        token = uuid.uuid4().hex
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE runs SET status='queued', lease_token=NULL, lease_until=NULL WHERE status='running' AND lease_until < ?", (now,))
            row = c.execute("""SELECT r.* FROM runs r WHERE r.status='queued' AND r.due<=?
                AND NOT EXISTS (SELECT 1 FROM runs p WHERE p.project_id=r.project_id AND p.environment=r.environment
                    AND json_extract(p.event, '$.conversation_id')=json_extract(r.event, '$.conversation_id')
                    AND p.status IN ('queued','running') AND p.seq<r.seq AND (p.due<=? OR p.attempt>0))
                AND NOT EXISTS (SELECT 1 FROM runs p WHERE p.project_id=r.project_id AND p.environment=r.environment
                    AND json_extract(p.event, '$.conversation_id')=json_extract(r.event, '$.conversation_id') AND p.status='running')
                ORDER BY r.seq LIMIT 1""", (now, now)).fetchone()
            if not row:
                return False
            c.execute("UPDATE runs SET status='running', attempt=attempt+1, lease_token=?, lease_until=? WHERE id=?", (token, now + self.LEASE_SECONDS, row["id"]))
            row = c.execute("SELECT * FROM runs WHERE id=?", (row["id"],)).fetchone()
        request = RunRequest(row["id"], row["workflow_id"], row["project_id"], row["commit_id"], row["test_session_id"], Event(**json.loads(row["event"])), row["attempt"])
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(self.LEASE_SECONDS / 3):
                with self._connect() as c:
                    c.execute("UPDATE runs SET lease_until=? WHERE id=? AND lease_token=?", (time.time() + self.LEASE_SECONDS, request.id, token))
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            try:
                result = self.execute(request)
                if result.status not in ("waiting", "completed", "failed", "uncertain"):
                    raise ValueError("Invalid runner status")
            except Exception as exc:
                result = RunResult("failed", error=str(exc), retryable=True)
        finally:
            stop.set()
            thread.join()
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT * FROM runs WHERE id=? AND lease_token=?", (request.id, token)).fetchone()
            if current:
                retry = result.status == "failed" and result.retryable and request.attempt < self.MAX_ATTEMPTS
                status = "queued" if retry else result.status
                logs = json.loads(current["logs"]) + result.logs
                c.execute("UPDATE runs SET status=?, logs=?, error=?, due=?, lease_token=NULL, lease_until=NULL WHERE id=?", (status, json.dumps(logs), result.error, time.time() + (2 ** (request.attempt - 1) if retry else 0), request.id))
                if status in ("completed", "failed", "uncertain"):
                    c.execute("""UPDATE workflows SET active=0 WHERE id=? AND NOT EXISTS
                        (SELECT 1 FROM runs WHERE workflow_id=? AND status IN ('queued', 'running'))""",
                              (request.workflow_id, request.workflow_id))
        return True

    def list(self, project_id: str) -> list[dict]:
        with self._connect() as c:
            return [self._record(row) for row in c.execute("SELECT * FROM runs WHERE project_id=? ORDER BY seq", (project_id,))]

    def sync_subscriptions(self, project_id: str, kinds: list[str]) -> None:
        self.get_project(project_id)
        if any(not isinstance(kind, str) or not kind for kind in kinds):
            raise ValueError("Invalid subscription")
        with self._connect() as c:
            c.execute("DELETE FROM subscriptions WHERE project_id=?", (project_id,))
            c.executemany("INSERT OR IGNORE INTO subscriptions VALUES (?, ?)", ((project_id, kind) for kind in kinds))

    def subscribed(self, project_id: str, kind: str) -> bool:
        with self._connect() as c:
            return c.execute("SELECT 1 FROM subscriptions WHERE project_id=? AND kind=?", (project_id, kind)).fetchone() is not None
