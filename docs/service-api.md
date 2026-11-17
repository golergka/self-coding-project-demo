# Service API

## Input

A scoped social operation, durable state operation, or follow-up schedule.

## Output

A JSON result, state value, schedule receipt, or error.

## Acceptance criteria

- Public clients, live scripts, and test scripts use the same social operation contract.
- Social operations cover accounts, posts, comments, profiles, message history, replies, and DMs.
- Post creation, editing, and deletion use the same idempotency rules as sends.
- Test requests use the mock adapter. State and effects remain separate for each project, environment, and workflow.
- State survives a run. A schedule receipt means the backend recorded the schedule.
- Retries keep their effect key. An ambiguous write records uncertainty and prevents automatic duplication.
- Read requests can return fresh data on each call. Invalid fields fail before adapter dispatch.

## Social operations

| Operation | Required fields | Result field |
|---|---|---|
| social.accounts.list | None | accounts |
| social.posts.list | account_id | posts |
| social.posts.get | post_id | post |
| social.comments.list | post_id | comments |
| social.messages.list | recipient_id | messages |
| social.profiles.get | profile_id | profile |
| social.posts.create | account_id, text, key | post |
| social.posts.update | post_id, text, key | post |
| social.posts.delete | post_id, key | ok |
| social.reply | comment_id, text, key | comment |
| social.dm | recipient_id, text, key | message |

Every social response includes a boolean `ok`. List operations accept an optional `limit` from 1 to 100. The catalog exposes request fields and example responses.

The demo adapter retains posts, replies, and messages for each project. Test fixtures can replace responses for any social operation. Fixtures describe responses without changing live resources.

## Internal operations

`state.get` and `state.put` retain workflow state. `schedule` records a delayed event pinned to the same workflow commit.
