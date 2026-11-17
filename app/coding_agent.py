"""Project-scoped chat and Git persistence for an isolated coding worker."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

import fcntl

PROMPT = """You help a nontechnical customer build a social media automation.
Explain your work in plain language and ask when requirements are unclear.
Edit the project files to implement their request. The service saves your edits
as Git commits. Main is live. Test any commit you choose. Use the backend
deployment tool to move main when the customer asks to deploy. Use only the supplied test API
for testing; never invoke live social actions. The test API URL, when supplied,
is available in SELF_CODING_TEST_API_URL. Do not put credentials in project files.

Customer request:\n"""


class CodexBackend:
    """Adapter for a provisioned, isolated coloph Codex client.

    The caller owns worker isolation, scoped proxy access, credentials and
    persistent Codex session storage. A fresh process is used for each turn.
    """

    def __init__(self, client):
        self.client = client

    def run(self, message, *, cwd, session_id, test_api_url):
        from coloph_agent_runtime import CodexRunOptions

        return self.client.run(
            PROMPT + message,
            cwd=cwd,
            options=CodexRunOptions(sandbox="workspace-write", timeout_seconds=300),
            session_id=session_id,
            resume=session_id is not None,
            env_overrides={"SELF_CODING_TEST_API_URL": test_api_url or ""},
        )


class CodingAgent:
    def __init__(self, root: Path, checkout: Callable[..., None],
                 save_commit: Callable[..., str], *, backend=None, tools=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.checkout = checkout
        self.save_commit = save_commit
        self.backend = backend
        self.tools = tools
        self.db = self.root / "coding-agent.sqlite3"
        self.owner = uuid.uuid4().hex
        (self.root / ".owners").mkdir(exist_ok=True)
        self._owner_lock = (self.root / ".owners" / self.owner).open("a")
        fcntl.flock(self._owner_lock, fcntl.LOCK_EX)
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (project TEXT PRIMARY KEY, session TEXT, branch TEXT NOT NULL DEFAULT 'main')")
            if "branch" not in {row[1] for row in db.execute("PRAGMA table_info(sessions)")}:
                db.execute("ALTER TABLE sessions ADD COLUMN branch TEXT NOT NULL DEFAULT 'main'")
            db.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, project TEXT, role TEXT, text TEXT)")
            db.execute("""CREATE TABLE IF NOT EXISTS chat_turns (
                id TEXT PRIMARY KEY, project TEXT, owner TEXT, status TEXT,
                message TEXT, updated_at REAL, error TEXT)""")
        self._recover_turns()
        self._locks_guard = threading.Lock()
        self._locks = {}

    def _lock(self, project_id):
        with self._locks_guard:
            return self._locks.setdefault(project_id, threading.Lock())

    def chat(self, project_id: str, message: str, test_api_url: str | None = None) -> dict:
        if not isinstance(project_id, str) or not project_id or not isinstance(message, str) or not message.strip():
            raise ValueError("project and message are required")
        if self.backend is None:
            raise RuntimeError("An isolated coding backend with a scoped test proxy is required")
        key = hashlib.sha256(project_id.encode()).hexdigest()
        turn_id = uuid.uuid4().hex
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO chat_turns VALUES (?,?,?,'queued',?,?,NULL)",
                       (turn_id, project_id, self.owner, message, time.time()))
        try:
            with self._lock(project_id), open(self.root / (key + ".lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self._turn_status(turn_id, "running")
                result = self._chat_locked(project_id, message, test_api_url, key)
            self._turn_status(turn_id, "completed")
            return result
        except BaseException as error:
            self._turn_status(turn_id, "failed" if isinstance(error, Exception) else "interrupted",
                              str(error)[:512])
            raise

    def _chat_locked(self, project_id, message, test_api_url, key):
        work = self.root / key
        if work.exists():
            shutil.rmtree(work)
        with sqlite3.connect(self.db) as db:
            row = db.execute("SELECT session,branch FROM sessions WHERE project=?", (project_id,)).fetchone()
        session, branch = (row[0], row[1]) if row else (None, "main")
        if branch == "main":
            self.checkout(project_id, work)
        else:
            self.checkout(project_id, work, branch=branch)
        if self.tools is not None:
            return self._tool_chat(project_id, message, work, session, test_api_url, branch)
        result = self.backend.run(message, cwd=work, session_id=session, test_api_url=test_api_url)
        if not result.session_id or not isinstance(result.response_text, str):
            raise ValueError("coding backend returned an invalid result")
        commit = self.save_commit(project_id, work, "Save automation commit")
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO sessions(project,session) VALUES (?, ?) ON CONFLICT(project) DO UPDATE SET session=excluded.session", (project_id, result.session_id))
            db.executemany("INSERT INTO history(project, role, text) VALUES (?, ?, ?)", [(project_id, "user", message), (project_id, "assistant", result.response_text)])
        return {"reply": result.response_text, "commit": commit, "session_id": result.session_id}

    def _turn_status(self, turn_id, status, error=None):
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE chat_turns SET status=?,updated_at=?,error=? WHERE id=?",
                       (status, time.time(), error, turn_id))

    def _recover_turns(self):
        with sqlite3.connect(self.db) as db:
            owners = [row[0] for row in db.execute(
                "SELECT DISTINCT owner FROM chat_turns WHERE status IN ('queued','running')")]
        for owner in owners:
            with (self.root / ".owners" / owner).open("a") as owner_lock:
                try:
                    fcntl.flock(owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                with sqlite3.connect(self.db) as db:
                    db.execute("""UPDATE chat_turns SET status='interrupted',updated_at=?,error=?
                                  WHERE owner=? AND status IN ('queued','running')""",
                               (time.time(), "The service stopped before this request finished.", owner))

    def progress(self, project_id):
        with sqlite3.connect(self.db) as db:
            db.row_factory = sqlite3.Row
            active = db.execute("""SELECT id,status,message,updated_at,error FROM chat_turns
                                   WHERE project=? AND status IN ('queued','running')
                                   ORDER BY rowid""", (project_id,)).fetchall()
            latest = db.execute("""SELECT id,status,updated_at,error FROM chat_turns
                                   WHERE project=? ORDER BY rowid DESC LIMIT 1""", (project_id,)).fetchone()
        return {"active": [dict(row) for row in active], "latest": dict(latest) if latest else None}

    def close(self):
        self._owner_lock.close()

    def history(self, project_id: str) -> list[dict]:
        with sqlite3.connect(self.db) as db:
            rows = db.execute("SELECT role, text FROM history WHERE project = ? ORDER BY id", (project_id,)).fetchall()
        return [{"role": role, "text": text} for role, text in rows]

    def _tool_chat(self, project, message, work, session, test_api_url, branch):
        from .chat_tools import TOOL_INSTRUCTIONS
        self._append_history(project, "user", message)
        prompt = TOOL_INSTRUCTIONS + "\nChecked-out Git branch: " + branch + "\nCustomer request:\n" + message
        try:
            for step in range(12):
                result = self.backend.run(prompt, cwd=work, session_id=session, test_api_url=test_api_url)
                if not result.session_id or not isinstance(result.response_text, str):
                    raise ValueError("coding backend returned an invalid result")
                session = result.session_id
                with sqlite3.connect(self.db) as db:
                    db.execute("INSERT INTO sessions(project,session,branch) VALUES (?,?,?) ON CONFLICT(project) DO UPDATE SET session=excluded.session,branch=excluded.branch",
                               (project, session, branch))
                response = json.loads(result.response_text)
                if not isinstance(response, dict) or set(response) != {"reply", "operation", "arguments_json"}:
                    raise ValueError("coding backend returned an invalid tool response")
                commit = self.save_commit(project, work, "Save automation commit")
                if response["operation"] == "respond":
                    reply = response["reply"]
                    if not isinstance(reply, str):
                        raise ValueError("coding backend returned an invalid reply")
                    self._append_history(project, "assistant", reply)
                    return {"reply": reply, "commit": commit, "session_id": session}
                try:
                    arguments = json.loads(response["arguments_json"])
                except (ValueError, TypeError):
                    arguments = response["arguments_json"]
                tool_result = self.tools.call(project, response["operation"], arguments, commit, checkout=work)
                selection = None
                if response["operation"] == "project.checkout":
                    selection = tool_result
                elif response["operation"] == "project.deploy":
                    selection = tool_result.get("checkout")
                if selection and selection.get("branch"):
                    branch = selection["branch"]
                    commit = selection["commit"]
                    with sqlite3.connect(self.db) as db:
                        db.execute("UPDATE sessions SET branch=? WHERE project=?", (branch, project))
                self._append_history(project, "tool", json.dumps({
                    "operation": response["operation"], "result": tool_result,
                }))
                prompt = ("Backend tool result for " + response["operation"] + ":\n"
                          + json.dumps(tool_result) + "\nContinue the customer request. "
                          "Use respond when finished. Checked-out Git branch: " + branch
                          + ". Current commit: " + str(commit))
            raise RuntimeError("The coding agent exceeded the tool-call limit")
        except Exception:
            self._append_history(project, "assistant",
                "This turn did not finish. Completed backend actions remain in the tool history.")
            raise

    def _append_history(self, project, role, text):
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO history(project,role,text) VALUES (?,?,?)", (project, role, text))
