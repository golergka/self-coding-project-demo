# Frontend

## Input

User actions and backend records for projects, chats, test sessions, and runs.

## Output

Project and chat requests, test events and mock replies, requests to move main, and views of backend state.

## Acceptance criteria

- Each project opens on a dashboard with its own run totals and recent executions.
- Versions shows the first-parent history of main. Its tip is the live version.
- Coding agent shows the current turn status and one saved conversation per project. The user can request changes, tests, and deployment in English.
- Preview screens identify illustrative data and unavailable controls.
- Completed backend tool results remain available as expandable chat details.
- The user can start a test on a chosen commit, inject events, and answer pending mock prompts.
- The user can move main to a selected commit and see run status, logs, and errors.
- Chat, test, and live run status can change independently. The browser shows the status that each backend component reports.
