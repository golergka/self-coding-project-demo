# Triggers and runs

## Input

A webhook, schedule, or test event. A test event includes a test session and commit.

## Output

A recorded event, a run request pinned to a commit, and run history with status, logs, and errors.

## Acceptance criteria

- The backend registers the webhooks that projects with automation code on main require. It receives webhooks and owns schedules while project containers are idle.
- An accepted event survives a restart. Duplicate deliveries do not create a second logical run for the same project.
- Each workflow pins its first commit for every run, reply, follow-up, and retry. A new live workflow uses the tip of main. A test workflow uses the session commit.
- Events for the same project, environment, and conversation update conversation state in sequence.
- A reply can resume a waiting workflow after the original container exits. Run status records retries and terminal outcomes.

The prototype stores webhook subscriptions locally. The browser debug panel supplies provider events.

Run ownership also fences state writes and new effect reservations. Short database transactions serialize these mutations with lease takeover. Network waits hold no queue database lock. A late provider receipt can resolve an existing effect, but an expired attempt cannot change state or create more work.
