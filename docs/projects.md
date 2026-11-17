# Projects and repos

## Input

A user ID, project request, Git branch, or commit.

## Output

Project metadata, the tip of main, and the repository's branch refs.

## Acceptance criteria

- Each project belongs to one user and has one Git repository.
- Git is the version model. The tip of main is the live version.
- A save commits to the checked-out branch. A save without changes retains its commit.
- The agent can choose any repository commit for a test run, including an older commit on any branch.
- Moving main changes the version for new live workflows. Existing workflows retain their starting commit.
- Referenced commits remain available while workflows wait for replies.
- The local prototype uses SQLite and demo profiles without sign-in.
