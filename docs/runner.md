# Container runner

## Input

A run ID, project scope, immutable commit, event, and live or test API context.

## Output

A completion result and logs. If the run fails, the runner also returns an error.

## Acceptance criteria

- The runner uses the exact commit in the request. Moving main does not change active containers.
- Untrusted code receives only the API privileges for its run. The proxy holds upstream credentials outside the container.
- Each container has a resource limit and a deadline. The runner removes the container after completion or timeout.
- The Docker prototype runs a container per invocation. A wait for a future event uses durable state and releases the container.
