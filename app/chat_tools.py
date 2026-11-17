"""Project-scoped tools executed by the backend after an agent turn."""

import json
from pathlib import Path
import sqlite3
import time
import uuid

from .api_catalog import OPERATIONS, demo_fixtures
from .contracts import Event


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "operation": {"type": "string", "enum": [
            "respond", "service.describe", "project.get", "project.checkout", "test.run",
            "project.deploy", "runs.list", "demo.events",
        ]},
        "arguments_json": {"type": "string"},
    },
    "required": ["reply", "operation", "arguments_json"],
    "additionalProperties": False,
}

TOOL_INSTRUCTIONS = """
You can use backend tools by returning the required JSON object. For a tool
call, set operation to its name and arguments_json to a JSON-encoded object.
After the backend returns the real result, continue with the next tool call
or use operation="respond" and put your final plain-language answer in reply.
operation="respond" ends the entire customer request. Use it only after you
finish the requested work or need an actual customer decision. Never use it
for a progress update, a plan, or a promise to work later. Use your native
file-editing tools to create or change source files before asking the backend
to test them. Do not ask for approval to perform work the customer requested.
Never invent test or deployment results. The backend commits your files on
the checked-out Git branch before each tool call. Tools bind the current project.
Git refs are the version model. The tip of main is always live.
Any commit can be selected for a test run. Choose an ordinary Git branch before
editing when you want to test changes without moving main.

service.describe {} returns the full social API contract and example responses.
project.get {} returns main_commit, the Git branches, and recent tool results.
project.checkout {"branch":"feature-name", "commit":"optional-full-sha"} checks
out an existing branch or creates it from the selected commit or current HEAD.
test.run {"events":[{"id":"unique","kind":"comment","conversation_id":"c",
  "payload":{"text":"pricing","comment_id":"c1","sender_id":"demo-user",
  "post_id":"demo-post"}}], "fixtures":{}, "expected_statuses":["waiting"],
  "expected_effects":[{"operation":"social.reply","request":{"text":"Sent you a DM!"}},
                      {"operation":"social.dm"}]}
runs the saved code in real isolated run containers, with a separate test
session. Fixtures override the default mock social responses. Provide explicit
expected statuses and expected effects for meaningful behavioral checks.
Effects include every social API call, including reads, in order.
Idempotency keys in recorded effects are opaque upstream keys. Match the
operation, target, and text instead of the original logical key. The tool
returns passed, checks, actual runs, and the mock request timeline.
project.deploy {"commit":"optional-full-sha"} moves main to that commit
(default: the commit just saved on the current branch) and registers webhooks.
Any commit in the repository can be selected, including a tested older commit.
Use it only when the customer asks to deploy or publish. Existing workflows
keep their original commit. Moving main is immediate in this local prototype.
runs.list {} returns the real run history and demo social effects.
demo.events {"events":[...], "expected_statuses":[...]} injects simulated
provider events into the deployed workflow. Use it only when the customer
asks to exercise the deployed demo. This tool uses the demo social adapter.
No real social accounts receive messages in this prototype.

Do not emulate these tools with shell scripts or HTTP calls. These control
tools execute in the backend, outside your container. Test project code through
test.run. Write source files, and avoid saving generated caches.
"""


class ChatTools:
    def __init__(self, db: Path, projects, runs, testing, social, move_main):
        self.db = db
        self.projects = projects
        self.runs = runs
        self.testing = testing
        self.social = social
        self.move_main = move_main
        with sqlite3.connect(db) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS chat_actions (
                id INTEGER PRIMARY KEY, project TEXT, operation TEXT,
                commit_id TEXT, status TEXT, result TEXT, arguments TEXT)""")
            columns = {row[1] for row in con.execute("PRAGMA table_info(chat_actions)")}
            if "arguments" not in columns:
                con.execute("ALTER TABLE chat_actions ADD COLUMN arguments TEXT")

    def history(self, project_id):
        with sqlite3.connect(self.db) as con:
            rows = con.execute(
                """SELECT id,operation,commit_id,status,result,arguments FROM chat_actions
                   WHERE project=? ORDER BY id DESC LIMIT 12""", (project_id,),
            ).fetchall()
        return [{"id": row[0], "operation": row[1], "commit": row[2],
                 "status": row[3], "result": json.loads(row[4]) if row[4] else None,
                 "arguments": json.loads(row[5]) if row[5] else None}
                for row in reversed(rows)]

    def call(self, project_id, operation, body, commit, *, checkout=None):
        self.projects.get(project_id)
        with sqlite3.connect(self.db) as con:
            action_id = con.execute(
                "INSERT INTO chat_actions(project,operation,commit_id,status,arguments) VALUES (?,?,?,'running',?)",
                (project_id, operation, commit, json.dumps(body)),
            ).lastrowid
        try:
            if not isinstance(body, dict):
                raise ValueError("Tool arguments must be an object")
            result = self._execute(project_id, operation, body, commit, checkout)
            status = "completed"
        except (ValueError, KeyError) as error:
            result = {"ok": False, "error": str(error)}
            status = "failed"
        except Exception:
            with sqlite3.connect(self.db) as con:
                con.execute("UPDATE chat_actions SET status='uncertain' WHERE id=?", (action_id,))
            raise
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE chat_actions SET status=?,result=? WHERE id=?",
                        (status, json.dumps(result), action_id))
        return result

    def _execute(self, project, operation, body, commit, checkout):
        if operation == "service.describe":
            return {"operations": OPERATIONS, "example_responses": demo_fixtures(),
                    "internal_operations": ["state.get", "state.put", "schedule"]}
        if operation == "project.get":
            return {**self.projects.get(project), "actions": [
                {key: value for key, value in action.items() if key not in {"result", "arguments"}}
                for action in self.history(project)
            ]}
        if operation == "project.checkout":
            if checkout is None:
                raise ValueError("A coding checkout is required")
            return self.projects.switch(project, checkout, body.get("branch"), body.get("commit"))
        if operation == "runs.list":
            return {"runs": self.runs.list(project), "effects": self.social.history(project)}
        if operation == "project.deploy":
            selected = body.get("commit", commit)
            result = {"ok": True, "deployment": self.move_main(project, selected)}
            if checkout is not None and self.projects.branch(project, checkout) == "main":
                result["checkout"] = self.projects.switch(project, checkout, "main", selected)
            return result
        if operation not in {"test.run", "demo.events"}:
            raise ValueError("Unknown backend tool")
        events = body.get("events")
        if not isinstance(events, list) or not 1 <= len(events) <= 20:
            raise ValueError("Supply between 1 and 20 events")
        parsed = []
        for event in events:
            if (not isinstance(event, dict) or event.get("kind") not in {"comment", "dm"}
                    or not isinstance(event.get("payload"), dict)
                    or not isinstance(event.get("conversation_id"), str) or not event["conversation_id"]
                    or not isinstance(event.get("id"), str) or not event["id"]):
                raise ValueError("Each event needs id, kind, conversation_id and payload")
            parsed.append(Event(event["id"], event["kind"], event["conversation_id"], event["payload"]))
        statuses = body.get("expected_statuses")
        expected = body.get("expected_effects")
        if statuses is not None and (not isinstance(statuses, list) or len(statuses) != len(parsed)):
            raise ValueError("Expected statuses must match the number of events")
        if expected is not None and (not isinstance(expected, list) or any(not isinstance(x, dict) for x in expected)):
            raise ValueError("Expected effects must be objects")
        if operation == "test.run":
            selected = body.get("commit", commit)
            fixtures = body.get("fixtures", {})
            if not isinstance(fixtures, dict):
                raise ValueError("Fixtures must be an object")
            # Validate the revision through the repository owner.
            from tempfile import TemporaryDirectory
            with TemporaryDirectory() as directory:
                self.projects.snapshot(project, selected, Path(directory) / "snapshot")
            session = self.testing.start(project, selected, {**demo_fixtures(), **fixtures})
            before = 0
        else:
            session = None
            if not self.projects.get(project)["main_commit"]:
                raise ValueError("Deploy the project before simulating live events")
            if any(not self.runs.subscribed(project, event.kind) for event in parsed):
                raise ValueError("The deployed project does not subscribe to this event")
            before = len(self.social.history(project))
        completed = []
        deadline = time.monotonic() + 90
        for event in parsed:
            if session is None:
                self.social.ingest(project, event)
            item = (self.testing.inject(session["id"], event) if session
                    else self.runs.enqueue(project, event))
            while item["status"] in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.05)
                item = next(run for run in self.runs.list(project) if run["id"] == item["id"])
            completed.append(item)
            if item["status"] in {"queued", "running", "failed", "uncertain"}:
                break
        effects = (self.testing.get(session["id"])["timeline"] if session
                   else self.social.history(project)[before:])
        checks = [{"name": "runs finish without errors",
                   "passed": len(completed) == len(parsed) and all(
                       run["status"] in {"waiting", "completed"} for run in completed)}]
        if statuses is not None:
            checks.append({"name": "expected run statuses",
                           "passed": [run["status"] for run in completed] == statuses})
        if expected is not None:
            matched = len(effects) == len(expected)
            for actual, wanted in zip(effects, expected):
                matched = matched and actual["operation"] == wanted.get("operation")
                subset = wanted.get("request", {})
                if not isinstance(subset, dict):
                    raise ValueError("Expected request must be an object")
                matched = matched and all(actual["request"].get(k) == v for k, v in subset.items())
            checks.append({"name": "expected social API calls", "passed": bool(matched)})
        commits = sorted({run["commit"] for run in completed if run["commit"]})
        workflow_ids = {run["workflow_id"] for run in completed}
        pending = [run for run in self.runs.list(project)
                   if run["workflow_id"] in workflow_ids and run["status"] in {"queued", "running"}]
        return {"ok": True, "passed": all(check["passed"] for check in checks),
                "commit": commits[0] if len(commits) == 1 else None,
                "commits": commits,
                "pending_runs": pending,
                "coverage": "Only the supplied events and expectations are checked. Pending scheduled runs are not verified.",
                "session_id": session["id"] if session else None,
                "expectations": {"statuses": statuses, "effects": expected},
                "checks": checks, "runs": completed, "effects": effects}
