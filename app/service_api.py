"""Scoped, durable operations exposed to automation runs."""

from contextlib import nullcontext
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Callable

from app.contracts import JSON, RunRequest
from app.api_catalog import validate


class ServiceAPI:
    def __init__(self, db: Path, mock: Callable, live: Callable, schedule: Callable):
        self.db, self.mock, self.live, self.schedule = db, mock, live, schedule
        db.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS service_state (
                    project TEXT, environment TEXT, workflow TEXT, value TEXT NOT NULL,
                    PRIMARY KEY (project, environment, workflow));
                CREATE TABLE IF NOT EXISTS service_effects (
                    project TEXT, environment TEXT, workflow TEXT, key TEXT,
                    operation TEXT NOT NULL, request TEXT NOT NULL, result TEXT,
                    PRIMARY KEY (project, environment, workflow, key));
            """)

    def _connect(self):
        return sqlite3.connect(self.db, timeout=30)

    @staticmethod
    def _scope(run):
        # Prefixes make live scope distinct even from a session named "live".
        return (run.project_id, "test:" + run.test_session_id if run.test_session_id is not None else "live:", run.workflow_id)

    def call(self, run: RunRequest, operation: str, body: JSON, *, fence=None) -> JSON:
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        scope = self._scope(run)
        if operation == "state.get":
            with self._connect() as con:
                row = con.execute("SELECT value FROM service_state WHERE project=? AND environment=? AND workflow=?", scope).fetchone()
            return {"value": json.loads(row[0]) if row else None}
        if operation == "state.put":
            if not isinstance(body.get("value"), dict):
                raise ValueError("state value must be an object")
            try:
                value = json.dumps(body["value"], allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("state value must be JSON-compatible") from exc
            with (fence(run) if fence else nullcontext()), self._connect() as con:
                con.execute("INSERT OR REPLACE INTO service_state VALUES (?, ?, ?, ?)", (*scope, value))
            return {"ok": True}
        if operation == "schedule":
            delay, key = body.get("delay_seconds"), body.get("key")
            if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay < float("inf") or not isinstance(key, str) or not key or "payload" not in body:
                raise ValueError("invalid schedule")
            try:
                json.dumps(body["payload"], allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("payload must be JSON-compatible") from exc
            return self.schedule(run, delay, body["payload"], key)
        spec = validate(operation, body)
        if not spec["effect"]:
            return self._adapter(run, operation, body, effect=False)
        request = json.dumps(body, sort_keys=True, separators=(",", ":"))
        effect_key = body["key"]
        upstream_key = hashlib.sha256(json.dumps([*scope, effect_key], separators=(",", ":")).encode()).hexdigest()
        with (fence(run) if fence else nullcontext()), self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT operation, request, result FROM service_effects WHERE project=? AND environment=? AND workflow=? AND key=?", (*scope, effect_key)).fetchone()
            if row:
                if row[0] != operation or row[1] != request:
                    raise ValueError("effect key already used for a different request")
                if row[2] is None:
                    return {"ok": False, "uncertain": True, "retryable": False, "error": "send outcome unknown"}
                result = json.loads(row[2])
                if result["ok"] or not result.get("retryable") or result.get("uncertain"):
                    return result
                con.execute("UPDATE service_effects SET result=NULL WHERE project=? AND environment=? AND workflow=? AND key=?", (*scope, effect_key))
            else:
                con.execute("INSERT INTO service_effects VALUES (?, ?, ?, ?, ?, ?, NULL)", (*scope, effect_key, operation, request))
        outgoing = {**body, "key": upstream_key}
        result = self._adapter(run, operation, outgoing, effect=True)
        with self._connect() as con:
            con.execute("UPDATE service_effects SET result=? WHERE project=? AND environment=? AND workflow=? AND key=?", (json.dumps(result), *scope, effect_key))
        return result


    def _adapter(self, run, operation, body, *, effect):
        try:
            result = (self.mock(run.test_session_id, operation, body)
                      if run.test_session_id is not None else self.live(run, operation, body))
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                raise ValueError("invalid adapter result")
            json.dumps(result, allow_nan=False)
            return result
        except Exception:
            if effect:
                return {"ok": False, "uncertain": True, "retryable": False, "error": "send outcome unknown"}
            return {"ok": False, "retryable": True, "error": "read failed"}
