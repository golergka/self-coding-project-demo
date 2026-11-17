import subprocess
from pathlib import Path

import pytest

from app.projects import Projects


def write_commit(projects, project_id, path, text, branch="main"):
    projects.checkout(project_id, path, branch=branch)
    (path / "automation.py").write_text(text)
    return projects.save(project_id, path, "Edit automation")


def test_git_branches_commits_and_main_are_the_version_model(tmp_path):
    projects = Projects(tmp_path / "data")
    project = projects.create("alice", "Automation")
    other = projects.create("bob", "Other")
    assert projects.main_history(project["id"]) == []
    assert project == {"id": project["id"], "user_id": "alice", "name": "Automation",
                       "main_commit": None, "branches": {}}
    first = write_commit(projects, project["id"], tmp_path / "first", "first")
    assert projects.get(project["id"])["main_commit"] == first
    second = write_commit(projects, project["id"], tmp_path / "second", "second", "feature/pricing")
    third = write_commit(projects, project["id"], tmp_path / "third", "third", "feature/pricing")
    info = projects.get(project["id"])
    assert info["main_commit"] == first
    assert info["branches"] == {"main": first, "feature/pricing": third}
    # An older commit remains selectable, even after its branch advances.
    assert projects.move_main(project["id"], second)["main_commit"] == second
    history = projects.main_history(project["id"])
    assert [entry["commit"] for entry in history] == [second, first]
    assert history[0]["message"] == "Edit automation"
    assert isinstance(history[0]["timestamp"], int)
    projects.snapshot(project["id"], first, tmp_path / "snapshot")
    assert (tmp_path / "snapshot" / "automation.py").read_text() == "first"
    assert not (tmp_path / "snapshot" / ".git").exists()
    projects.checkout(project["id"], tmp_path / "old", second, branch="experiment")
    assert (tmp_path / "old" / "automation.py").read_text() == "second"
    (tmp_path / "old" / "notes.txt").write_text("Independent branch")
    experiment = projects.save(project["id"], tmp_path / "old", "Save experiment")
    assert projects.get(project["id"])["branches"]["experiment"] == experiment
    assert projects.get(project["id"])["main_commit"] == second
    with pytest.raises(ValueError):
        projects.snapshot(other["id"], third, tmp_path / "wrong")
    fourth = write_commit(projects, project["id"], tmp_path / "fourth", "fourth")
    assert Projects(tmp_path / "data").get(project["id"])["main_commit"] == fourth
    repo = tmp_path / "data" / "repos" / (project["id"] + ".git")
    assert subprocess.check_output(["git", "--git-dir", str(repo), "rev-parse", "main"], text=True).strip() == fourth
    subprocess.run(["git", "--git-dir", str(repo), "update-ref", "refs/heads/main", second], check=True)
    assert projects.get(project["id"])["main_commit"] == second
    assert set(projects.get(project["id"])["branches"]) == {"main", "feature/pricing", "experiment"}


def test_rejects_stale_or_foreign_checkouts_and_invalid_requests(tmp_path):
    projects = Projects(tmp_path / "data")
    project = projects.create("alice", "A")
    other = projects.create("alice", "B")
    with pytest.raises(KeyError):
        projects.get("missing")
    with pytest.raises(ValueError):
        projects.create("", "A")
    projects.checkout(project["id"], tmp_path / "stale")
    write_commit(projects, project["id"], tmp_path / "current", "current")
    with pytest.raises(ValueError):
        projects.save(project["id"], tmp_path / "stale", "stale")
    with pytest.raises(ValueError):
        projects.save(other["id"], tmp_path / "current", "foreign")
    with pytest.raises(ValueError):
        projects.checkout(project["id"], tmp_path / "bad", "HEAD")


def test_snapshot_preserves_files_ignored_by_git_archive(tmp_path):
    projects = Projects(tmp_path / "data")
    project = projects.create("alice", "A")
    checkout = tmp_path / "checkout"
    projects.checkout(project["id"], checkout)
    (checkout / ".gitattributes").write_text("automation.py export-ignore\n")
    (checkout / "automation.py").write_text("tracked")
    commit = projects.save(project["id"], checkout, "Add files")
    projects.snapshot(project["id"], commit, tmp_path / "snapshot")
    assert (tmp_path / "snapshot" / "automation.py").read_text() == "tracked"


def test_git_calls_ignore_inherited_repository_environment(tmp_path, monkeypatch):
    projects = Projects(tmp_path / "data")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "unrelated"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "unrelated"))
    project = projects.create("alice", "A")
    commit = write_commit(projects, project["id"], tmp_path / "checkout", "content")
    assert projects.move_main(project["id"], commit)["main_commit"] == commit


def test_commit_browser_reads_exact_version_and_first_parent_diff(tmp_path):
    projects = Projects(tmp_path / "data")
    project = projects.create("alice", "A")["id"]
    other = projects.create("alice", "B")["id"]
    checkout = tmp_path / "checkout"
    projects.checkout(project, checkout)
    (checkout / "nested").mkdir()
    (checkout / "nested" / "hello world.py").write_text("first\n")
    (checkout / "binary").write_bytes(b"\0\xff")
    (checkout / "link").symlink_to("/etc/passwd")
    first = projects.save(project, checkout, "Initial files")
    tree = projects.commit_files(project, first)
    assert tree["commit"] == first
    assert {f["path"]: f["kind"] for f in tree["files"]} == {
        "nested/hello world.py": "file", "binary": "file", "link": "symlink"}
    assert projects.commit_file(project, first, "nested/hello world.py")["content"] == "first\n"
    assert projects.commit_file(project, first, "binary")["binary"] is True
    assert projects.commit_file(project, first, "link")["content"] == "/etc/passwd"
    initial = projects.commit_diff(project, first)
    assert initial["parent"] is None
    assert "+first" in initial["diff"]
    (checkout / "nested" / "hello world.py").write_text("second\n")
    (checkout / "large").write_bytes(b"x" * 262145)
    second = projects.save(project, checkout, "Update files")
    diff = projects.commit_diff(project, second)
    assert diff["parent"] == first
    assert "-first" in diff["diff"] and "+second" in diff["diff"]
    assert projects.commit_file(project, first, "nested/hello world.py")["content"] == "first\n"
    assert projects.commit_file(project, second, "large")["truncated"] is True
    assert projects.commit_file(project, second, "large")["content"] is None
    for invalid in ("../etc/passwd", "HEAD:binary", "--help"):
        with pytest.raises(KeyError):
            projects.commit_file(project, first, invalid)
    with pytest.raises(ValueError):
        projects.commit_files(other, first)
    with pytest.raises(ValueError):
        projects.commit_diff(project, "HEAD")
