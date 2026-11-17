"""Persistent, project-scoped stand-in for the assumed social service."""

import json
import sqlite3
import uuid

from .api_catalog import validate
from .contracts import JSON, RunRequest


class DemoSocial:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS fixtures (
                    project TEXT, operation TEXT, response TEXT,
                    PRIMARY KEY (project, operation)
                );
                CREATE TABLE IF NOT EXISTS calls (
                    id INTEGER PRIMARY KEY, project TEXT, workflow TEXT,
                    operation TEXT, effect_key TEXT, request TEXT, response TEXT,
                    UNIQUE (project, workflow, operation, effect_key)
                );
                CREATE TABLE IF NOT EXISTS resources (
                    project TEXT, kind TEXT, id TEXT, body TEXT NOT NULL,
                    PRIMARY KEY (project, kind, id)
                );
            """)

    def __call__(self, run: RunRequest, operation: str, body: JSON) -> JSON:
        spec = validate(operation, body)
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            self._seed(db, run.project_id)
            key = (run.project_id, run.workflow_id, operation, body.get("key", uuid.uuid4().hex))
            if spec["effect"]:
                previous = db.execute(
                    "SELECT response FROM calls WHERE project=? AND workflow=? AND operation=? AND effect_key=?",
                    key,
                ).fetchone()
                if previous:
                    return json.loads(previous[0])
            fixture = db.execute(
                "SELECT response FROM fixtures WHERE project=? AND operation=?",
                (run.project_id, operation),
            ).fetchone()
            result = (json.loads(fixture[0]) if fixture
                      else self._perform(db, run.project_id, operation, body))
            if result.get("ok") or result.get("uncertain") or not result.get("retryable"):
                db.execute(
                    """INSERT INTO calls (project,workflow,operation,effect_key,request,response)
                       VALUES (?,?,?,?,?,?)""",
                    (*key, json.dumps(body), json.dumps(result)),
                )
            return result

    @staticmethod
    def _seed(db, project):
        for kind, item in (
            ("account", {"id": "demo-account", "platform": "instagram", "name": "Demo shop"}),
            ("post", {"id": "demo-post", "account_id": "demo-account", "text": "Ask us about pricing"}),
            ("profile", {"id": "demo-user", "name": "Demo customer"}),
        ):
            # Seed once per project, so a deleted demo post stays deleted.
            if db.execute("SELECT 1 FROM resources WHERE project=? AND kind='seed'", (project,)).fetchone():
                return
            db.execute("INSERT OR IGNORE INTO resources VALUES (?,?,?,?)", (project, kind, item["id"], json.dumps(item)))
        db.execute("INSERT INTO resources VALUES (?, 'seed', 'seed', '{}')", (project,))

    @staticmethod
    def _items(db, project, kind):
        rows = db.execute("SELECT body FROM resources WHERE project=? AND kind=? ORDER BY rowid", (project, kind))
        return [json.loads(row[0]) for row in rows]

    def _perform(self, db, project, operation, body):
        def put(kind, item):
            db.execute("INSERT OR REPLACE INTO resources VALUES (?,?,?,?)", (project, kind, item["id"], json.dumps(item)))
            return item

        def get(kind, item_id):
            row = db.execute("SELECT body FROM resources WHERE project=? AND kind=? AND id=?", (project, kind, item_id)).fetchone()
            return json.loads(row[0]) if row else None

        if operation == "social.accounts.list":
            return {"ok": True, "accounts": self._items(db, project, "account")}
        if operation == "social.profiles.get":
            profile = get("profile", body["profile_id"])
            return {"ok": True, "profile": profile} if profile else {"ok": False, "error": "Profile not found"}
        if operation == "social.posts.get":
            post = get("post", body["post_id"])
            return {"ok": True, "post": post} if post else {"ok": False, "error": "Post not found"}
        if operation in {"social.posts.list", "social.comments.list", "social.messages.list"}:
            kind, field, response = {
                "social.posts.list": ("post", "account_id", "posts"),
                "social.comments.list": ("comment", "post_id", "comments"),
                "social.messages.list": ("message", "recipient_id", "messages"),
            }[operation]
            items = [item for item in self._items(db, project, kind) if item.get(field) == body[field]]
            return {"ok": True, response: items[:body.get("limit", 100)]}
        if operation == "social.posts.create":
            if not get("account", body["account_id"]):
                return {"ok": False, "error": "Account not found"}
            post = put("post", {"id": uuid.uuid4().hex, "account_id": body["account_id"], "text": body["text"]})
            return {"ok": True, "post": post}
        if operation in {"social.posts.update", "social.posts.delete"}:
            post = get("post", body["post_id"])
            if post is None:
                return {"ok": False, "error": "Post not found"}
            if operation.endswith("update"):
                post["text"] = body["text"]
                return {"ok": True, "post": put("post", post)}
            db.execute("DELETE FROM resources WHERE project=? AND kind='post' AND id=?", (project, body["post_id"]))
            return {"ok": True}
        kind = "message" if operation == "social.dm" else "comment"
        fields = {k: v for k, v in body.items() if k != "key"}
        if kind == "comment":
            parent = get("comment", body["comment_id"])
            if parent:
                fields["post_id"] = parent.get("post_id")
        else:
            fields["direction"] = "outgoing"
        item = put(kind, {"id": uuid.uuid4().hex, **fields})
        return {"ok": True, kind: item}

    def ingest(self, project, event):
        """Record simulated provider input for subsequent service reads."""
        payload = event.payload
        if event.kind == "comment":
            item_id = payload.get("comment_id", event.id)
            kind = "comment"
            item = {"id": item_id, **payload}
        elif event.kind == "dm":
            item_id, kind = event.id, "message"
            item = {"id": item_id, **payload, "recipient_id": payload.get("sender_id"),
                    "direction": "incoming"}
        else:
            return
        with sqlite3.connect(self.path) as db:
            self._seed(db, project)
            db.execute("INSERT OR IGNORE INTO resources VALUES (?,?,?,?)",
                       (project, kind, item_id, json.dumps(item)))

    def fixture(self, project: str, operation: str, response: JSON) -> JSON:
        from .api_catalog import OPERATIONS
        if operation not in OPERATIONS:
            raise ValueError("Unknown social operation")
        if not isinstance(response.get("ok"), bool):
            raise ValueError("The response needs a boolean ok field")
        encoded = json.dumps(response, allow_nan=False)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """INSERT INTO fixtures VALUES (?,?,?)
                   ON CONFLICT (project,operation) DO UPDATE SET response=excluded.response""",
                (project, operation, encoded),
            )
        return response

    def history(self, project: str) -> list[JSON]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT id,workflow,operation,request,response FROM calls WHERE project=? ORDER BY id", (project,),
            ).fetchall()
        return [
            {"id": row_id, "workflow_id": workflow, "operation": operation,
             "request": json.loads(request), "response": json.loads(response)}
            for row_id, workflow, operation, request, response in rows
        ]
