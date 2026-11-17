"""HTTP wiring for the local self-coding prototype."""

from contextlib import asynccontextmanager
from hmac import compare_digest
from pathlib import Path
import logging
import os
import time
from tempfile import TemporaryDirectory
from threading import Event as StopEvent, Lock, Thread
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .api_catalog import OPERATIONS, demo_fixtures
from .chat_tools import ChatTools
from .coding_agent import CodingAgent
from .coding_worker import CodingWorker
from .contracts import Event, JSON, RunRequest, RunResult
from .demo_social import DemoSocial
from .projects import Projects
from .runner import Runner
from .runs import Runs
from .service_api import ServiceAPI
from .test_environment import TestEnvironment

DEMO_USER = "demo"
HERE = Path(__file__).parent
LOG = logging.getLogger(__name__)


class EventInput(BaseModel):
    id: str = Field(min_length=1)
    kind: Literal["comment", "dm"]
    conversation_id: str = Field(min_length=1)
    payload: JSON

    def event(self) -> Event:
        return Event(**self.model_dump())


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class CommitInput(BaseModel):
    commit: str = Field(min_length=1)


class TestInput(CommitInput):
    fixtures: dict[str, JSON] | None = None


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


class FixtureInput(BaseModel):
    operation: str
    response: JSON


def create_app(root: Path | None = None) -> FastAPI:
    root = (root or Path(os.environ.get("SELF_CODING_DATA", ".data"))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    projects = Projects(root / "projects")
    social = DemoSocial(root / "social.sqlite")
    failures: dict[tuple[str, int], JSON | None] = {}
    failures_lock = Lock()

    def call_api(run: RunRequest, operation: str, body: JSON) -> JSON:
        result = service.call(run, operation, body, fence=runs.mutation)
        if operation.startswith("social.") and result.get("ok") is False:
            key = (run.id, run.attempt)
            with failures_lock:
                if key in failures:
                    failures[key] = result
        return result

    def execute(run: RunRequest) -> RunResult:
        key = (run.id, run.attempt)
        with failures_lock:
            failures[key] = None
        try:
            result = runner.execute(run)
        finally:
            with failures_lock:
                failure = failures.pop(key)
        if failure and result.status == "failed":
            return RunResult(
                "uncertain" if failure.get("uncertain") else "failed",
                result.logs,
                failure.get("error") or result.error,
                bool(failure.get("retryable")) and not failure.get("uncertain"),
            )
        return result

    runs = Runs(root / "runs.sqlite", projects.get, execute)
    testing = TestEnvironment(root / "tests.sqlite", runs.enqueue)
    service = ServiceAPI(root / "service.sqlite", testing.call, social, runs.schedule)
    runner = Runner(projects.snapshot, call_api)
    def move_main(project_id, commit):
        import ast
        with TemporaryDirectory(prefix="self-coding-main-") as work:
            snapshot = Path(work) / "snapshot"
            projects.snapshot(project_id, commit, snapshot)
            source = snapshot / "automation.py"
            if source.is_symlink() or not source.is_file():
                raise ValueError("The commit needs a regular automation.py file")
            try:
                ast.parse(source.read_text())
            except (SyntaxError, UnicodeError) as error:
                raise ValueError("automation.py must contain valid Python") from error
        runs.sync_subscriptions(project_id, ["comment", "dm"])
        return projects.move_main(project_id, commit)

    tools = ChatTools(root / "chat-tools.sqlite", projects, runs, testing, social, move_main)
    worker_image = os.environ.get("SELF_CODING_CODEX_IMAGE")
    auth_file = os.environ.get("SELF_CODING_CODEX_AUTH")
    control_key = os.environ.get("SELF_CODING_CONTROL_KEY")
    if worker_image and not control_key:
        raise RuntimeError("Set SELF_CODING_CONTROL_KEY when enabling the coding worker")
    coding_worker = (CodingWorker(root / "codex-workers", worker_image,
                                  Path(auth_file).resolve() if auth_file else None)
                     if worker_image else None)
    def save_commit(project_id, checkout, message):
        commit = projects.save(project_id, checkout, message)
        if projects.get(project_id)["main_commit"] == commit:
            runs.sync_subscriptions(project_id, ["comment", "dm"])
        return commit

    agent = CodingAgent(root / "agent", projects.checkout, save_commit,
                        backend=coding_worker, tools=tools)

    @asynccontextmanager
    async def lifespan(_app):
        for project in projects.list(DEMO_USER):
            if project["main_commit"]:
                runs.sync_subscriptions(project["id"], ["comment", "dm"])
        stop = StopEvent()

        def work():
            while not stop.is_set():
                try:
                    if not runs.work_once():
                        stop.wait(0.1)
                except Exception:
                    LOG.exception("Run worker failed")
                    stop.wait(1)

        workers = [Thread(target=work, name=f"run-worker-{i}", daemon=True) for i in range(2)]
        for worker in workers:
            worker.start()
        try:
            yield
        finally:
            stop.set()
            for worker in workers:
                worker.join(timeout=2)
            if coding_worker:
                coding_worker.close()
            agent.close()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def protect_browser_control(request: Request, call_next):
        path = request.url.path
        if control_key and path.startswith("/api/") and not path.startswith("/api/coding-tests/"):
            key = request.cookies.get("self_coding_control", "")
            if not compare_digest(key.encode(), control_key.encode()):
                return JSONResponse({"detail": "Open the local URL with the browser key"}, status_code=403)
        response = await call_next(request)
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(ValueError)
    async def invalid(_request: Request, error: ValueError):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(_request: Request, error: KeyError):
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.get("/api/projects")
    def list_projects():
        return projects.list(DEMO_USER)

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectInput):
        project = projects.create(DEMO_USER, body.name.strip())
        with TemporaryDirectory(prefix="self-coding-project-") as work:
            checkout = Path(work) / "checkout"
            projects.checkout(project["id"], checkout)
            projects.save(project["id"], checkout, "Initialize automation project")
        return projects.get(project["id"])

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        return projects.get(project_id)

    @app.get("/api/projects/{project_id}/history")
    def main_history(project_id: str):
        return projects.main_history(project_id)

    @app.get("/api/projects/{project_id}/commits/{commit}/files")
    def commit_files(project_id: str, commit: str):
        return projects.commit_files(project_id, commit)

    @app.get("/api/projects/{project_id}/commits/{commit}/file")
    def commit_file(project_id: str, commit: str, path: str):
        return projects.commit_file(project_id, commit, path)

    @app.get("/api/projects/{project_id}/commits/{commit}/diff")
    def commit_diff(project_id: str, commit: str):
        return projects.commit_diff(project_id, commit)

    @app.post("/api/projects/{project_id}/example")
    def add_example(project_id: str):
        with TemporaryDirectory(prefix="self-coding-example-") as work:
            checkout = Path(work) / "checkout"
            projects.checkout(project_id, checkout, branch="example")
            source = (HERE / "examples" / "instagram_pricing.py").read_text()
            target = checkout / "automation.py"
            target.unlink(missing_ok=True)
            target.write_text(source)
            commit = projects.save(project_id, checkout, "Add Instagram pricing automation")
        return {**projects.get(project_id), "commit": commit, "branch": "example"}

    @app.post("/api/projects/{project_id}/main")
    def update_main(project_id: str, body: CommitInput):
        return move_main(project_id, body.commit)

    @app.get("/api/projects/{project_id}/runs")
    def list_runs(project_id: str):
        projects.get(project_id)
        return runs.list(project_id)

    @app.post("/api/projects/{project_id}/tests", status_code=201)
    def create_test(project_id: str, body: TestInput):
        with TemporaryDirectory(prefix="self-coding-test-") as work:
            projects.snapshot(project_id, body.commit, Path(work) / "snapshot")
        return testing.start(project_id, body.commit, body.fixtures)

    @app.get("/api/tests/{session_id}")
    def get_test(session_id: str):
        return testing.get(session_id)

    @app.post("/api/tests/{session_id}/events", status_code=202)
    def inject_test(session_id: str, body: EventInput):
        return testing.inject(session_id, body.event())

    @app.post("/api/tests/{session_id}/prompts/{request_id}")
    def answer(session_id: str, request_id: str, body: JSON):
        if not isinstance(body.get("ok"), bool):
            raise ValueError("The response needs a boolean ok field")
        return testing.answer(session_id, request_id, body)

    @app.get("/api/projects/{project_id}/chat")
    def chat_history(project_id: str):
        projects.get(project_id)
        return agent.history(project_id)

    @app.get("/api/projects/{project_id}/chat/status")
    def chat_status(project_id: str):
        projects.get(project_id)
        return agent.progress(project_id)

    @app.post("/api/projects/{project_id}/chat")
    def chat(project_id: str, body: ChatInput):
        project = projects.get(project_id)
        test_url = None
        if project["main_commit"]:
            test = testing.start(project_id, project["main_commit"], demo_fixtures())
            origin = os.environ.get("SELF_CODING_CODING_TEST_ORIGIN", "http://host.docker.internal:18765")
            test_url = f"{origin.rstrip('/')}/api/coding-tests/{test['id']}"
        try:
            return agent.chat(project_id, body.message, test_url)
        except RuntimeError as error:
            LOG.warning("Coding agent failed: %s", type(error).__name__)
            if coding_worker is None:
                raise HTTPException(503, "Set SELF_CODING_CODEX_IMAGE to enable coding chat") from error
            raise HTTPException(503, "The coding worker could not complete this turn") from error

    @app.post("/api/coding-tests/{session_id}/{operation}")
    def coding_test(session_id: str, operation: str, body: JSON):
        if operation not in {*OPERATIONS, "state.get", "state.put"}:
            raise HTTPException(404, "Unknown mock operation")
        session = testing.get(session_id)
        if time.time() >= session["expires_at"]:
            raise HTTPException(410, "Coding test session expired")
        run = RunRequest(session_id, session_id, session["project_id"], session["commit"],
                         session_id, Event(session_id, "schedule", "coding-test", {}), 1)
        return service.call(run, operation, body)

    @app.get("/api/service")
    def service_catalog():
        return {"operations": OPERATIONS, "example_responses": demo_fixtures()}

    @app.post("/api/projects/{project_id}/service/{operation}")
    def public_service(project_id: str, operation: str, body: JSON):
        if operation not in OPERATIONS:
            raise HTTPException(404, "Unknown public service operation")
        project = projects.get(project_id)
        run = RunRequest("external", "external", project_id,
                         project["main_commit"],
                         None, Event("external", "comment", "external", {}), 1)
        return service.call(run, operation, body)

    @app.get("/api/projects/{project_id}/actions")
    def tool_history(project_id: str):
        projects.get(project_id)
        return tools.history(project_id)

    @app.post("/api/projects/{project_id}/debug/webhook", status_code=202)
    def webhook(project_id: str, body: EventInput):
        projects.get(project_id)
        if not runs.subscribed(project_id, body.kind):
            raise HTTPException(409, "Publish a project with this event subscription first")
        event = body.event()
        social.ingest(project_id, event)
        return runs.enqueue(project_id, event)

    @app.get("/api/projects/{project_id}/debug/social")
    def social_history(project_id: str):
        projects.get(project_id)
        return social.history(project_id)

    @app.post("/api/projects/{project_id}/debug/social")
    def social_fixture(project_id: str, body: FixtureInput):
        projects.get(project_id)
        return social.fixture(project_id, body.operation, body.response)

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def index(key: str | None = None):
        if control_key and key and compare_digest(key.encode(), control_key.encode()):
            response = RedirectResponse("/", status_code=303)
            response.set_cookie("self_coding_control", control_key, httponly=True, samesite="strict")
            return response
        return FileResponse(HERE / "static" / "index.html")

    return app
