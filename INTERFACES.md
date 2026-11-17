# Prototype interfaces

Each component owns its module and matching test file. The coordinator owns shared types, HTTP wiring, the Instagram example, and browser integration. Python APIs below are synchronous. Constructors must create their own tables and paths. Use JSON-compatible values at HTTP boundaries. Use ValueError for invalid requests and KeyError for missing entities.

## Projects — app/projects.py

- `Projects(root: Path)`.
- `create(user_id: str, name: str) -> dict`, `list(user_id: str) -> list[dict]`, `get(project_id: str) -> dict`. Project keys are id, user_id, name, main_commit, branches. main_commit is the tip of main. branches maps Git branch names to commit IDs.
- `checkout(project_id: str, dest: Path, commit: str | None = None, *, branch: str = "main") -> None` creates a Git checkout on a selected Git branch or at an exact requested commit.
- `save(project_id: str, checkout: Path, message: str) -> str` commits changes to the checked-out Git branch.
- `switch(project_id: str, checkout: Path, branch: str, commit: str | None = None) -> dict` selects a Git branch, optionally starting at a chosen commit.
- `move_main(project_id: str, commit: str) -> dict` moves main to any selected repository commit.
- `snapshot(project_id: str, commit: str, dest: Path) -> None` exports tracked files without Git metadata. Retain previous revisions even while workflows wait.

## Triggers and runs — app/runs.py

- `Runs(db: Path, get_project: Callable[[str], dict], execute: Callable[[RunRequest], RunResult])`.
- `enqueue(project_id: str, event: Event, *, test_session_id: str | None = None, commit: str | None = None) -> dict`. Returns run record with id, workflow_id, project_id, commit, test_session_id, event, status, attempt, logs, error. Test events require both session and commit.
- `schedule(run: RunRequest, delay_seconds: float, payload: JSON, key: str) -> dict`. Persist a delayed event bound to the workflow and deduplicate by key.
- `work_once() -> bool` claims and runs one due event. One lease owner can mutate a conversation in each project and environment. Persist retry, recovery and outcomes. `list(project_id: str) -> list[dict]`.
- `sync_subscriptions(project_id: str, kinds: list[str]) -> None` and `subscribed(project_id: str, kind: str) -> bool`. The coordinator calls these when main updates and on webhook ingress.
- A comment can start a workflow. A DM for an active conversation continues that workflow. A schedule always continues its source workflow. Reserve the workflow before dispatch, even if a DM arrives before the first run ends. Queue within the conversation. A DM without a workflow can finish as a no-op. The coordinator supplies the polling worker.

## Container runner — app/runner.py

- `Runner(snapshot: Callable[[str, str, Path], None], call_api: Callable[[RunRequest, str, JSON], JSON])`. `execute(run: RunRequest) -> RunResult`.
- Run Python in Docker with a read-only snapshot, no network, no host credentials, and hard resource and time limits. Container entry point is `automation.py`, function `handle(event: dict, api: callable) -> str`. Return waiting or completed. The coordinator supplies this automation, the runner owns the bootstrap.
- The bootstrap runs each API call as host RPC through stdio. The event is the dataclass converted to a dictionary. The runner binds scope to the request that launched the container. Report logs and exceptions. An API error includes a structured error result or exception. Keep stdin/stdout framing bounded and treat container output as untrusted.

## Service API — app/service_api.py

- `ServiceAPI(db: Path, mock: Callable[[str, str, JSON], JSON], live: Callable[[RunRequest, str, JSON], JSON], schedule: Callable[[RunRequest, float, JSON, str], JSON])`. `call(run: RunRequest, operation: str, body: JSON) -> JSON`.
- The catalog in `app/api_catalog.py` defines accounts, posts, comments, profiles, messages, and post mutations. Operations `social.reply` with comment_id/text/key and `social.dm` with recipient_id/text/key. Return a dict with ok and optional error, uncertain and retryable. No raw network URLs. Keep an effect record scoped to project, test session and workflow. Pass a stable upstream key to the adapter. Classify ambiguous sends conservatively. Only use the live adapter for live scope.
- `state.get` returns {"value": object or null}. `state.put` accepts {"value": object} and returns {"ok": true}. State is workflow scoped. `schedule` takes delay_seconds, payload and key. Only route social calls to the test fixture adapter.

## Test environment — app/test_environment.py

- `TestEnvironment(db: Path, enqueue: Callable[..., dict])`. `start(project_id: str, commit: str, fixtures: dict | None = None) -> dict`, `get(session_id: str) -> dict`, `inject(session_id: str, event: Event) -> dict`.
- Session keys id, project_id, commit, expires_at and timeline. Timeline entries have id, operation, request, response, status (pending, answered, expired). Fixtures map operation names to response objects. The UI can start with success fixtures.
- `call(session_id: str, operation: str, body: JSON) -> JSON` records each request and returns a fixture or waits for a response with a bounded timeout. `answer(session_id: str, request_id: str, response: JSON) -> dict` resolves a prompt once.

## Coding agent — app/coding_agent.py

- `CodingAgent(root: Path, checkout: Callable[..., None], save_commit: Callable[..., str])`. `chat(project_id: str, message: str, test_api_url: str | None = None) -> dict`. Return reply, commit, and session_id. `history(project_id: str) -> list[dict]` of role/text entries.
- Integrate `coloph_agent_runtime.CodexClient` using the v0.3.0 public GitHub release artifact as a dependency if PyPI is unavailable. Own agent dependency in `requirements-agent.txt` to avoid shared build conflicts. Document actual prerequisites. Keep a configurable backend seam for component tests.
- Backend tools in `app/chat_tools.py` execute project-scoped structured requests for tests and deployment outside the coding container.
- Serialize chat per project and persist continuation. Use the test scope only. The coordinator wires the test API endpoint and credentials. No changes outside the component files.

## Frontend — app/static/

- Plain browser UI, served at /. Own index.html, app.js, and styles.css.
- GET/POST /api/projects (create body name), GET /api/projects/{project}, POST /api/projects/{project}/main (commit).
- GET/POST /api/projects/{project}/chat (message), GET /api/projects/{project}/runs, POST /api/projects/{project}/example (adds an example commit on an ordinary Git branch).
- POST /api/projects/{project}/tests (commit and optional fixtures), GET /api/tests/{session}, POST /api/tests/{session}/events (Event body), POST /api/tests/{session}/prompts/{request} (response object).
- POST /api/projects/{project}/debug/webhook (Event body) and GET/POST /api/projects/{project}/debug/social (POST operation and response fixture). Debug live events use the demo social adapter, which records outgoing effects. Support live and test mode in the browser.
- An Instagram comment payload has text, comment_id, sender_id, post_id. A DM payload has text and sender_id. For the demo, use the same conversation_id for the comment and DM.
