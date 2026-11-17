# Product concept

These are the user's initial requirements. Implementation details and unresolved decisions will evolve with the user.

## Projects and coding

- Each user has projects, each backed by a Git repository saved for that user.
- When a user asks to work on a project, restore its saved Codex chat and instantiate a Codex coding agent using `coloph-agent-runtime` (`CodexClient`).
- The agent works on the request and returns a response for the user to see.
- The agent's system prompt explains the system and how to interact with a non-technical user.
- A frontend lets users interact with projects and see results and progress.
- The backend tracks progress using explicit project states.

- Git is the version model. Main is always live. The coding agent can test any commit on any branch.

## Running project code

- A separate component runs the code written for a project in containers.
- The runner connects containers to the service API and handles authentication explicitly, without exposing authentication details to the code in the container.
- Project code is untrusted. It receives only the user's privileges.
- The service exposes the same API publicly and to project code.
- Coding containers can also access that API with the same authentication model, including for testing.
- The API supports webhooks; relevant subscriptions are created automatically.
- Each workflow keeps its starting Git commit. Replies and follow-ups use that commit after main changes.

## Example workflow

An Instagram pricing comment triggers a public reply and a private request for an email address. After the customer supplies an email address, the workflow sends a link.
