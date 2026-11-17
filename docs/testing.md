# Test environment

## Input

A project and commit, optional fixtures, simulated events, mock API requests, and user replies to mock prompts.

## Output

A test session, queued test events, and a timeline of API requests, responses, and pending prompts.

## Acceptance criteria

- A session lasts up to one hour and pins a commit. It can use an intermediate agent commit.
- Each session has its own state and fixtures. It never uses live social credentials or sends live messages.
- The timeline shows each mock API request. A fixture answers a known request. A request without a fixture can ask the user for a response.
- A user response applies to its pending request once. A timeout or session expiry gives pending requests a defined error.
- A simulated comment or DM enters the same run queue as a live event, with the test session as its scope.
