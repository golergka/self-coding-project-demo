# Coding agent

## Input

A project checkout, saved Codex session, and English user request.

## Output

A plain-language reply, Git commit, and recorded results from backend tools.

## Acceptance criteria

- Each project resumes its own chat through `coloph-agent-runtime`. Idle workers release compute and retain the session.
- The agent explains its work to a nontechnical user.
- The agent can create code, run isolated tests, inspect results, and move main through chat.
- Backend tools bind every action to the current project. The coding container receives only its mock service URL.
- Tests run the saved commit in the normal runner. Reports include actual statuses, API calls, and assertion results.
- Deployment moves main to the selected Git commit and registers its webhooks. Existing workflows keep their original commit.
- The backend persists queued, running, completed, failed, and interrupted chat turn states. Refresh retains progress, and restart identifies abandoned turns.
- The backend records completed actions before it asks the model to explain them. An interrupted chat preserves that evidence.

## Execution

Codex returns a structured tool request or a final reply. The backend commits to the checked-out branch, runs the requested tool, and returns its actual result to Codex.

Test reports also show pending scheduled runs. Their assertions cover the supplied events, and do not claim coverage of future follow-ups.

The tools are `service.describe`, `project.get`, `project.checkout`, `test.run`, `project.deploy`, `runs.list`, and `demo.events`. Deployment follows a user request. Demo events emulate provider input against the deployed code.

The app keeps a trusted Docker coding worker warm for 90 seconds after a turn. A separate browser key protects project controls.

Setup and login requirements are in the [README](../README.md).

The worker image includes a CLI compatibility shim. It preserves explicit sandbox configuration when the runtime resumes a saved chat.
