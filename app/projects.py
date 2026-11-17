"""Project metadata and durable Git revisions for automation projects."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path


class Projects:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "repos").mkdir(exist_ok=True)
        self.db = self.root / "projects.sqlite3"
        with sqlite3.connect(self.db) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES profiles(id),
                    name TEXT NOT NULL
                );
            """)

    @staticmethod
    def _git_env() -> dict:
        return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

    def _git(self, *args: str, cwd: Path | None = None, input: bytes | None = None) -> bytes:
        result = subprocess.run(["git", *map(str, args)], cwd=cwd, input=input,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._git_env())
        if result.returncode:
            raise ValueError(result.stderr.decode(errors="replace").strip())
        return result.stdout

    def _repo(self, project_id: str) -> Path:
        # Look up first, so identifiers never become unchecked filesystem paths.
        with sqlite3.connect(self.db) as db:
            row = db.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        return self.root / "repos" / (row[0] + ".git")

    @contextmanager
    def _lock(self, repo: Path):
        with (repo / "project.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _ref(self, repo: Path, ref: str) -> str | None:
        result = subprocess.run(["git", "--git-dir", str(repo), "rev-parse", "--verify", "--quiet", ref],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._git_env())
        return result.stdout.decode().strip() if result.returncode == 0 else None

    def _commit(self, repo: Path, commit: str) -> str:
        if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
            raise ValueError("commit must be a full Git commit ID")
        resolved = self._ref(repo, commit + "^{commit}")
        if resolved != commit:
            raise ValueError("commit does not exist in this project")
        return resolved

    def create(self, user_id: str, name: str) -> dict:
        if not isinstance(user_id, str) or not user_id.strip() or not isinstance(name, str) or not name.strip():
            raise ValueError("user_id and name are required")
        project_id = uuid.uuid4().hex
        repo = self.root / "repos" / (project_id + ".git")
        self._git("init", "--bare", "--initial-branch=main", str(repo))
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT OR IGNORE INTO profiles VALUES (?)", (user_id,))
            db.execute("INSERT INTO projects VALUES (?, ?, ?)", (project_id, user_id, name))
        return self.get(project_id)

    def get(self, project_id: str) -> dict:
        repo = self._repo(project_id)
        with sqlite3.connect(self.db) as db:
            row = db.execute("SELECT id, user_id, name FROM projects WHERE id = ?", (project_id,)).fetchone()
        refs = self._git("--git-dir", str(repo), "for-each-ref",
                         "--format=%(refname:strip=2) %(objectname)", "refs/heads")
        branches = dict(line.split(" ", 1) for line in refs.decode().splitlines())
        return {"id": row[0], "user_id": row[1], "name": row[2],
                "main_commit": branches.get("main"), "branches": branches}

    def list(self, user_id: str) -> list[dict]:
        with sqlite3.connect(self.db) as db:
            ids = [row[0] for row in db.execute("SELECT id FROM projects WHERE user_id = ? ORDER BY rowid", (user_id,))]
        return [self.get(project_id) for project_id in ids]

    def main_history(self, project_id: str) -> list[dict]:
        repo = self._repo(project_id)
        with self._lock(repo):
            if not self._ref(repo, "refs/heads/main"):
                return []
            output = self._git("--git-dir", str(repo), "log", "--first-parent", "--format=%H%x00%ct%x00%s%x00",
                               "refs/heads/main")
        records = []
        for line in output.splitlines():
            commit, timestamp, message, _ = line.split(b"\0", 3)
            records.append({"commit": commit.decode(), "timestamp": int(timestamp),
                            "message": message.decode(errors="replace")})
        return records

    def commit_files(self, project_id: str, commit: str) -> dict:
        repo = self._repo(project_id)
        self._commit(repo, commit)
        output = self._git("--git-dir", str(repo), "ls-tree", "-rlz", "--full-tree", commit)
        files = []
        for entry in output.split(b"\0"):
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            mode, kind, object_id, size = metadata.decode().split()
            files.append({"path": path.decode(errors="replace"),
                          "kind": "symlink" if mode == "120000" else "submodule" if kind == "commit" else "file",
                          "size": None if size == "-" else int(size)})
        return {"commit": commit, "files": files}

    def commit_file(self, project_id: str, commit: str, path: str) -> dict:
        repo = self._repo(project_id)
        self._commit(repo, commit)
        if not isinstance(path, str) or not path or "\0" in path:
            raise ValueError("A repository file path is required")
        # Match exact tree entries rather than accepting revision/path expressions.
        output = self._git("--git-dir", str(repo), "ls-tree", "-rz", "--full-tree", commit)
        for entry in output.split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            if name != path.encode():
                continue
            mode, kind, object_id = metadata.decode().split()
            if kind != "blob":
                raise ValueError("This entry is not a file")
            size = int(self._git("--git-dir", str(repo), "cat-file", "-s", object_id))
            if size > 262144:
                return {"commit": commit, "path": path, "content": None,
                        "binary": False, "truncated": True, "size": size}
            content = self._git("--git-dir", str(repo), "cat-file", "blob", object_id)
            try:
                text = content.decode("utf-8")
                binary = "\0" in text
            except UnicodeDecodeError:
                text, binary = None, True
            return {"commit": commit, "path": path, "content": None if binary else text,
                    "binary": binary, "truncated": False, "size": size}
        raise KeyError("File not found in this commit")

    def commit_diff(self, project_id: str, commit: str) -> dict:
        repo = self._repo(project_id)
        self._commit(repo, commit)
        ancestry = self._git("--git-dir", str(repo), "rev-list", "--parents", "-n", "1", commit).decode().split()
        parent = ancestry[1] if len(ancestry) > 1 else None
        arguments = (["diff", parent, commit] if parent else
                     ["diff-tree", "--root", "--no-commit-id", "-r", "-p", commit])
        output = self._git("--git-dir", str(repo), *arguments, "--no-ext-diff",
                           "--no-textconv", "--no-color", "--")
        limit = 1048576
        return {"commit": commit, "parent": parent,
                "diff": output[:limit].decode(errors="replace"), "truncated": len(output) > limit}

    def _branch(self, name):
        if not isinstance(name, str) or not name:
            raise ValueError("A Git branch name is required")
        self._git("check-ref-format", "refs/heads/" + name)
        self._git("check-ref-format", "--branch", name)
        return name

    def checkout(self, project_id: str, dest: Path, commit: str | None = None,
                 *, branch: str = "main") -> None:
        repo = self._repo(project_id)
        self._branch(branch)
        with self._lock(repo):
            selected = (self._commit(repo, commit) if commit is not None
                        else self._ref(repo, "refs/heads/" + branch)
                        or self._ref(repo, "refs/heads/main"))
            self._git("clone", "--no-checkout", str(repo), str(dest))
            if selected:
                self._git("checkout", "--detach", selected, cwd=dest)
                self._git("update-ref", "refs/heads/" + branch, selected, cwd=dest)
                self._git("switch", branch, cwd=dest)
            else:
                self._git("symbolic-ref", "HEAD", "refs/heads/" + branch, cwd=dest)

    def _check_checkout(self, repo, checkout):
        origin = self._git("remote", "get-url", "origin", cwd=checkout).decode().strip()
        if Path(origin).resolve() != repo:
            raise ValueError("checkout does not belong to this project")

    def branch(self, project_id: str, checkout: Path) -> str:
        self._check_checkout(self._repo(project_id), checkout)
        return self._git("symbolic-ref", "--short", "HEAD", cwd=checkout).decode().strip()

    def switch(self, project_id: str, checkout: Path, branch: str,
               commit: str | None = None) -> dict:
        repo = self._repo(project_id)
        self._branch(branch)
        with self._lock(repo):
            self._check_checkout(repo, checkout)
            self._git("fetch", "origin", cwd=checkout)
            selected = (self._commit(repo, commit) if commit is not None
                        else self._ref(repo, "refs/heads/" + branch)
                        or self._ref(checkout / ".git", "HEAD"))
            if selected:
                self._git("checkout", "--detach", selected, cwd=checkout)
                self._git("update-ref", "refs/heads/" + branch, selected, cwd=checkout)
                self._git("switch", branch, cwd=checkout)
            else:
                self._git("symbolic-ref", "HEAD", "refs/heads/" + branch, cwd=checkout)
        return {"branch": branch, "commit": selected}

    def save(self, project_id: str, checkout: Path, message: str) -> str:
        repo = self._repo(project_id)
        checkout = Path(checkout).resolve()
        if not isinstance(message, str) or not message.strip():
            raise ValueError("commit message is required")
        with self._lock(repo):
            self._check_checkout(repo, checkout)
            branch = self._git("symbolic-ref", "--short", "HEAD", cwd=checkout).decode().strip()
            self._branch(branch)
            head = self._ref(checkout / ".git", "HEAD")
            remote = self._ref(repo, "refs/heads/" + branch)
            if remote is not None and remote != head:
                raise ValueError("checkout is not based on the current branch tip")
            self._git("add", "-A", cwd=checkout)
            if head and not self._git("diff", "--cached", "--name-only", cwd=checkout).strip():
                commit = head
            else:
                self._git("-c", "user.name=Self-coding demo", "-c", "user.email=demo@localhost",
                          "commit", "--allow-empty", "-m", message, cwd=checkout)
                commit = self._git("rev-parse", "HEAD", cwd=checkout).decode().strip()
            self._git("push", "--atomic", "origin", f"{commit}:refs/heads/{branch}",
                      f"{commit}:refs/archive/{commit}", cwd=checkout)
            return commit

    def move_main(self, project_id: str, commit: str) -> dict:
        repo = self._repo(project_id)
        with self._lock(repo):
            self._commit(repo, commit)
            self._git("--git-dir", str(repo), "update-ref", "refs/heads/main", commit)
        return self.get(project_id)

    def snapshot(self, project_id: str, commit: str, dest: Path) -> None:
        repo = self._repo(project_id)
        self._commit(repo, commit)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        if any(dest.iterdir()):
            raise ValueError("snapshot destination must be empty")
        entries = self._git("--git-dir", str(repo), "ls-tree", "-rz", "--full-tree", commit)
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode, kind, object_id = metadata.decode().split()
            if kind != "blob":
                raise ValueError("submodules are not supported in snapshots")
            target = dest / os.fsdecode(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            content = self._git("--git-dir", str(repo), "cat-file", "blob", object_id)
            if mode == "120000":
                target.symlink_to(os.fsdecode(content))
            else:
                target.write_bytes(content)
                target.chmod(0o755 if mode == "100755" else 0o644)
