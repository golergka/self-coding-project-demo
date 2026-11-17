"""Docker provisioning for the trusted Codex coding worker."""

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from shutil import copytree, rmtree
from threading import Lock, Timer, current_thread
from urllib.parse import urlsplit, urlunsplit


AUTOMATION_CONTRACT = """Project runtime:
- Define handle(event, api) in automation.py. Return "waiting" to continue the
  same workflow on a future event or "completed" to finish it.
- The event is a dictionary with id, kind, conversation_id, and payload.
  Read event["kind"] and event["payload"], not Python attributes. Instagram comment
  payloads contain text, sender_id, comment_id, and post_id. DM payloads
  contain text and sender_id.
- Call api("state.get", {}) to read {"value": ...}. Save an object with
  api("state.put", {"value": ...}). State belongs to one workflow.
- Use api("social.reply", {"comment_id": ..., "text": ..., "key": ...}) and
  api("social.dm", {"recipient_id": ..., "text": ..., "key": ...}).
  Use a stable key for each intended send. Treat {"ok": false} as an error.
- Call the backend service.describe tool for the complete social API,
  including accounts, posts, comments, profiles, and message history.
- Use the backend test.run and project.deploy tools for testing and deployment.
  The optional mock URL also accepts social API operations for isolated checks.

"""


@dataclass
class Worker:
    container: object
    timer: Timer | None = None


class CodingWorker:
    IDLE_SECONDS = 90

    def __init__(self, root: Path, image: str, auth_file: Path | None = None):
        self.root = root
        self.image = image
        self.auth_file = auth_file
        self.workers: dict[str, Worker] = {}
        self.lock = Lock()

    def run(self, message, *, cwd, session_id, test_api_url):
        from coloph_agent_runtime import CodexRunOptions, CodexSubscriptionAuth
        from coloph_agent_runtime.docker import DockerBackend

        from .coding_agent import PROMPT
        from .chat_tools import RESPONSE_SCHEMA

        key = cwd.name
        directory = self.root / key
        workspace = directory / "workspace"
        session_store = directory / "sessions"
        workspace.mkdir(parents=True, exist_ok=True)
        session_store.mkdir(parents=True, exist_ok=True)
        with self.lock:
            worker = self.workers.get(key)
            if worker and worker.timer:
                worker.timer.cancel()
                worker.timer = None
            self._clear(workspace)
            copytree(cwd, workspace, symlinks=True, dirs_exist_ok=True,
                     ignore=self._ignored_files)
            try:
                if worker is None:
                    container = DockerBackend().start(
                        image=self.image,
                        mounts=(
                            f"type=bind,src={workspace},dst=/workspace",
                            f"type=bind,src={session_store},dst=/home/agent/.local/share/coloph-agent-runtime",
                        ),
                    )
                    worker = self.workers[key] = Worker(container)
                auth = (CodexSubscriptionAuth.from_file(self.auth_file)
                        if self.auth_file else CodexSubscriptionAuth.from_local())
                try:
                    result = worker.container.codex(auth=auth, executable="self-coding-codex").run(
                        AUTOMATION_CONTRACT + PROMPT + message,
                        cwd=Path("/workspace"),
                        options=CodexRunOptions(sandbox="danger-full-access", timeout_seconds=300,
                                                skip_git_repo_check=True, ignore_user_config=True,
                                                json_schema=RESPONSE_SCHEMA),
                        session_id=session_id,
                        resume=session_id is not None,
                        env_overrides={"SELF_CODING_TEST_API_URL": self._docker_url(test_api_url)},
                    )
                finally:
                    if self.auth_file:
                        auth.save(self.auth_file)
                self._clear(cwd, keep_git=True)
                copytree(workspace, cwd, symlinks=True, dirs_exist_ok=True,
                         ignore=self._ignored_files)
                return result
            except Exception:
                if worker is not None:
                    self._close(key, worker)
                    worker = None
                raise
            finally:
                self._remove_worker_credentials(session_store)
                if worker is not None:
                    timer = Timer(self.IDLE_SECONDS, self._expire, args=(key, worker))
                    timer.daemon = True
                    worker.timer = timer
                    timer.start()

    @staticmethod
    def _ignored_files(path, names):
        return {name for name in names if name in {".git", "__pycache__", ".pytest_cache"}
                or name.endswith((".pyc", ".pyo"))}

    @staticmethod
    def _remove_worker_credentials(session_store):
        codex = session_store / "codex"
        if codex.is_symlink() or not codex.is_dir():
            return
        for account in codex.iterdir():
            if account.is_dir() and not account.is_symlink():
                (account / "auth.json").unlink(missing_ok=True)

    @staticmethod
    def _clear(directory: Path, *, keep_git=False):
        for item in directory.iterdir():
            if keep_git and item.name == ".git":
                continue
            if item.is_dir() and not item.is_symlink():
                rmtree(item)
            else:
                item.unlink()

    @staticmethod
    def _docker_url(url):
        if not url:
            return ""
        parts = urlsplit(url)
        if parts.hostname in {"127.0.0.1", "localhost"}:
            host = "host.docker.internal"
            if parts.port:
                host += f":{parts.port}"
            return urlunsplit(parts._replace(netloc=host))
        return url

    def _expire(self, key, worker):
        with self.lock:
            if self.workers.get(key) is worker and worker.timer is current_thread():
                self._close(key, worker)

    def _close(self, key, worker):
        if worker.timer:
            worker.timer.cancel()
        self.workers.pop(key, None)
        with suppress(Exception):
            worker.container.close()

    def close(self):
        with self.lock:
            for key, worker in list(self.workers.items()):
                self._close(key, worker)
