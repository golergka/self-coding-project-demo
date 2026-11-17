"""Persistent fixtures and interactive prompts for isolated test runs."""

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable

from app.contracts import Event, JSON


class TestEnvironment:
    SESSION_SECONDS = 3600
    PROMPT_TIMEOUT_SECONDS = 30
    POLL_SECONDS = 0.05

    def __init__(self, db: Path, enqueue: Callable[..., dict]):
        self.db = Path(db)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.enqueue = enqueue
        with self._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS test_sessions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, commit_hash TEXT NOT NULL,
                    expires_at REAL NOT NULL, fixtures TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS test_requests (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, operation TEXT NOT NULL,
                    request TEXT NOT NULL, response TEXT, status TEXT NOT NULL,
                    deadline REAL NOT NULL, created_at REAL NOT NULL
                );
            """)

    def _connect(self):
        return sqlite3.connect(self.db, timeout=30)

    @staticmethod
    def _object(value, name):
        if not isinstance(value, dict):
            raise ValueError(f"{name} must be an object")
        try:
            return json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be JSON-compatible") from exc

    @staticmethod
    def _session(con, session_id):
        row = con.execute(
            "SELECT id, project_id, commit_hash, expires_at, fixtures FROM test_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return row

    @staticmethod
    def _expire(con, session_id, now):
        error = json.dumps({"ok": False, "error": "prompt expired"})
        con.execute(
            "UPDATE test_requests SET status = 'expired', response = ? "
            "WHERE session_id = ? AND status = 'pending' AND deadline <= ?",
            (error, session_id, now),
        )

    def start(self, project_id: str, commit: str, fixtures: dict | None = None) -> dict:
        if not isinstance(project_id, str) or not project_id or not isinstance(commit, str) or not commit:
            raise ValueError("project_id and commit are required")
        fixtures = {} if fixtures is None else fixtures
        self._object(fixtures, "fixtures")
        if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in fixtures.items()):
            raise ValueError("fixtures must map operation names to response objects")
        session_id = str(uuid.uuid4())
        with self._connect() as con:
            con.execute("INSERT INTO test_sessions VALUES (?, ?, ?, ?, ?)",
                        (session_id, project_id, commit, time.time() + self.SESSION_SECONDS, json.dumps(fixtures)))
        return self.get(session_id)

    def get(self, session_id: str) -> dict:
        with self._connect() as con:
            session = self._session(con, session_id)
            self._expire(con, session_id, time.time())
            rows = con.execute(
                "SELECT id, operation, request, response, status FROM test_requests "
                "WHERE session_id = ? ORDER BY created_at, rowid", (session_id,)
            ).fetchall()
        return {"id": session[0], "project_id": session[1], "commit": session[2],
                "expires_at": session[3], "timeline": [
                    {"id": row[0], "operation": row[1], "request": json.loads(row[2]),
                     "response": json.loads(row[3]) if row[3] is not None else None, "status": row[4]}
                    for row in rows]}

    def inject(self, session_id: str, event: Event) -> dict:
        if not isinstance(event, Event) or event.kind not in ("comment", "dm"):
            raise ValueError("test events must be comments or DMs")
        with self._connect() as con:
            session = self._session(con, session_id)
            if time.time() >= session[3]:
                raise ValueError("test session expired")
        return self.enqueue(session[1], event, test_session_id=session_id, commit=session[2])

    def call(self, session_id: str, operation: str, body: JSON) -> JSON:
        if not isinstance(operation, str) or not operation:
            raise ValueError("operation is required")
        request = self._object(body, "request")
        request_id = str(uuid.uuid4())
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            session = self._session(con, session_id)
            now = time.time()
            if now >= session[3]:
                return {"ok": False, "error": "test session expired"}
            fixtures = json.loads(session[4])
            response = fixtures.get(operation)
            status = "answered" if operation in fixtures else "pending"
            deadline = min(session[3], now + self.PROMPT_TIMEOUT_SECONDS)
            con.execute("INSERT INTO test_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (request_id, session_id, operation, request,
                         json.dumps(response) if status == "answered" else None, status, deadline, now))
        if status == "answered":
            return response
        while True:
            with self._connect() as con:
                self._expire(con, session_id, time.time())
                row = con.execute("SELECT response, status FROM test_requests WHERE id = ?", (request_id,)).fetchone()
            if row[1] != "pending":
                return json.loads(row[0])
            time.sleep(min(self.POLL_SECONDS, max(0, deadline - time.time())))

    def answer(self, session_id: str, request_id: str, response: JSON) -> dict:
        encoded = self._object(response, "response")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._session(con, session_id)
            self._expire(con, session_id, time.time())
            row = con.execute("SELECT status FROM test_requests WHERE id = ? AND session_id = ?",
                              (request_id, session_id)).fetchone()
            if row is None:
                raise KeyError(request_id)
            if row[0] != "pending":
                raise ValueError("request is no longer pending")
            con.execute("UPDATE test_requests SET response = ?, status = 'answered' WHERE id = ?",
                        (encoded, request_id))
        return self.get(session_id)
