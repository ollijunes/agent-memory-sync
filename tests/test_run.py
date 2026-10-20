import json
import shutil
from pathlib import Path

import pytest

from agent_memory_sync.cli import main
from agent_memory_sync.config import SyncError, dump
from agent_memory_sync.gitops import git as git_result
from agent_memory_sync.gitops import lock, redact, repository, run, tree_files
from tests.conftest import git
from tests.test_scan import canaries


def invoke(setup, *args):
    home, _, _, config = setup
    return main(["--home", str(home), "--config", str(config), *args])


def test_mirror_and_no_change(setup, capsys):
    home, repo, config, _ = setup
    assert invoke(setup, "run") == 0
    first = git(repo, "rev-parse", "HEAD")
    manifest = json.loads((repo / "laptop/MANIFEST.json").read_text())
    assert len(manifest) == 4
    assert not any("auth.json" in row["path"] or row["path"].endswith("jsonl") for row in manifest)
    assert all(not Path(row["path"]).is_absolute() for row in manifest)
    assert run(config) == 0
    assert "no changes" in capsys.readouterr().out
    assert git(repo, "rev-parse", "HEAD") == first
    (home / ".claude/CLAUDE.md").unlink()
    assert run(config) == 0
    assert not (repo / "laptop/claude/CLAUDE.md").exists()
    assert "1 removed" in git(repo, "log", "-1", "--format=%s")
    assert git(repo, "status", "--porcelain") == ""


def test_secret_blocks_without_changes(setup, capsys, monkeypatch, tmp_path):
    home, repo, config, _ = setup
    run(config)
    before = git(repo, "rev-parse", "HEAD")
    tree = {
        str(p.relative_to(repo)): p.read_bytes()
        for p in (repo / "laptop").rglob("*")
        if p.is_file()
    }
    text = canaries()["api-key"]
    (home / ".claude/CLAUDE.md").write_text(text)
    capsys.readouterr()
    import agent_memory_sync.gitops as module

    stage = tmp_path / "captured-stage"

    def mkdtemp(**kwargs):
        stage.mkdir(mode=0o700)
        return str(stage)

    monkeypatch.setattr(module.tempfile, "mkdtemp", mkdtemp)
    assert run(config) == 3
    assert not stage.exists()
    output = capsys.readouterr().out
    assert "claude/CLAUDE.md:1: api-key" in output
    assert text not in output
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo, "status", "--porcelain") == ""
    assert tree == {
        str(p.relative_to(repo)): p.read_bytes()
        for p in (repo / "laptop").rglob("*")
        if p.is_file()
    }


def test_push_and_retry_unchanged(setup, tmp_path, capsys):
    _, repo, config, _ = setup
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git(bare, "init", "--bare", "-b", "main")
    git(repo, "remote", "add", "origin", str(tmp_path / "missing.git"))
    assert invoke(setup, "run") == 6
    local = git(repo, "rev-parse", "HEAD")
    assert git(repo, "status", "--porcelain") == ""
    assert "Push failed" in capsys.readouterr().err
    git(repo, "remote", "set-url", "origin", str(bare))
    assert run(config) == 0
    assert git(bare, "rev-parse", "HEAD") == local
    assert git(repo, "rev-parse", "HEAD") == local
    git(repo, "remote", "rename", "origin", "backup")
    assert run(config) == 0


@pytest.mark.parametrize("kind", ["tracked", "untracked", "staged"])
def test_dirty_backup_rejected(setup, kind):
    _, repo, config, _ = setup
    run(config)
    path = repo / "laptop/claude/CLAUDE.md"
    if kind in ("untracked", "ignored"):
        path = repo / "laptop/unknown.txt"
    path.write_text("Person edited this file.")
    if kind == "ignored":
        (repo / ".git/info/exclude").write_text("unknown.txt\n")
    if kind == "staged":
        git(repo, "add", "--", "laptop")
    assert invoke(setup, "run") == 4
    assert path.read_text() == "Person edited this file."


def test_identity_and_lock(setup, capsys):
    _, repo, _, _ = setup
    git(repo, "config", "--unset", "user.email")
    assert invoke(setup, "run") == 5
    assert "git config user.email" in capsys.readouterr().err
    assert not (repo / "laptop").exists()
    with lock(repo) as acquired:
        assert acquired
        assert invoke(setup, "run") == 0
        assert "another run is in progress" in capsys.readouterr().out


def test_dry_run_and_plan_leave_tree_alone(setup, capsys):
    _, repo, _, _ = setup
    head = git(repo, "rev-parse", "HEAD")
    assert invoke(setup, "run", "--dry-run") == 0
    assert invoke(setup, "plan") == 0
    output = capsys.readouterr().out
    assert "include claude/CLAUDE.md" in output
    assert "skip codex/auth.json: excluded:" in output
    assert not (repo / "laptop").exists()
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain") == ""


def test_unrelated_staged_changes_preserved(setup):
    _, repo, config, _ = setup
    (repo / "unrelated.txt").write_text("Another task.")
    git(repo, "add", "unrelated.txt")
    run(config)
    assert git(repo, "diff", "--cached", "--name-only") == "unrelated.txt"
    assert "unrelated.txt" not in git(repo, "ls-tree", "-r", "--name-only", "HEAD")


def test_exclusion_change_and_disabled_source_mirrored(setup):
    _, repo, config, _ = setup
    run(config)
    config.sources["claude"].exclude = ["CLAUDE.md"]
    config.sources["codex"].enabled = False
    run(config)
    assert not (repo / "laptop/claude/CLAUDE.md").exists()
    assert not (repo / "laptop/codex").exists()


def test_file_directory_transitions(setup):
    home, repo, config, _ = setup
    config.sources["claude"].include = ["custom/**"]
    source = home / ".claude/custom"
    source.mkdir()
    (source / "item").write_text("first")
    run(config)
    (source / "item").unlink()
    (source / "item").mkdir()
    (source / "item/note.md").write_text("second")
    run(config)
    (source / "item/note.md").unlink()
    (source / "item").rmdir()
    (source / "item").write_text("third")
    run(config)
    assert (repo / "laptop/claude/custom/item").read_text() == "third"


def test_git_boundaries_and_redaction(setup, tmp_path):
    _, repo, _, _ = setup
    with pytest.raises(SyncError, match="work tree"):
        repository(tmp_path)
    with pytest.raises(SyncError, match="failed"):
        git_result(repo, "not-a-command")
    path = tmp_path / "link"
    path.symlink_to(repo, target_is_directory=True)
    with pytest.raises(SyncError, match="symlink"):
        tree_files(path)
    path.unlink()
    path.write_text("file")
    with pytest.raises(SyncError, match="directory"):
        tree_files(path)
    path.unlink()
    path.mkdir()
    (path / "link").symlink_to(repo)
    with pytest.raises(SyncError, match="symlink"):
        tree_files(path)
    url = "https://" + "example-user:example-value@" + "example.com/repo.git"
    assert redact("failed " + url) == "failed https://example.com/repo.git"


def test_scanner_error_leaves_repo_clean(setup, fake_gitleaks):
    _, repo, config, path = setup
    config.scanner = "gitleaks"
    path.write_bytes(dump(config))
    head = git(repo, "rev-parse", "HEAD")
    fake_gitleaks(2)
    assert invoke(setup, "run") == 1
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.integration
def test_real_gitleaks_end_to_end(setup, tmp_path, capsys):
    if not shutil.which("gitleaks"):
        pytest.skip("gitleaks not installed")
    home, repo, config, path = setup
    config.scanner = "gitleaks"
    path.write_bytes(dump(config))
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git(bare, "init", "--bare", "-b", "main")
    git(repo, "remote", "add", "origin", str(bare))
    assert invoke(setup, "plan") == 0
    assert invoke(setup, "run") == 0
    head = git(repo, "rev-parse", "HEAD")
    assert git(bare, "rev-parse", "HEAD") == head
    assert invoke(setup, "run") == 0
    assert git(repo, "rev-parse", "HEAD") == head
    text = canaries()["github-token"]
    (home / ".claude/CLAUDE.md").write_text(text)
    assert invoke(setup, "run") == 3
    assert text not in capsys.readouterr().out
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", "HEAD") == head
    (home / ".claude/CLAUDE.md").unlink()
    assert invoke(setup, "run") == 0
    assert not (repo / "laptop/claude/CLAUDE.md").exists()


def test_run_with_unborn_branch(setup):
    home, _, config, _ = setup
    repo = home / "new-backup"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Synthetic Example")
    git(repo, "config", "user.email", "synthetic@example.net")
    config.repo = repo
    assert run(config) == 0
    assert git(repo, "status", "--porcelain") == ""


def test_cli_missing_scanner_exit_seven(setup, monkeypatch):
    _, repo, config, path = setup
    config.scanner = "gitleaks"
    path.write_bytes(dump(config))
    monkeypatch.setattr("agent_memory_sync.scan.shutil.which", lambda name: None)
    assert invoke(setup, "run") == 7
    assert git(repo, "status", "--porcelain") == ""


def test_push_error_strips_url_userinfo(setup, monkeypatch, capsys):
    import subprocess

    import agent_memory_sync.gitops as module

    _, repo, _, _ = setup
    git(repo, "remote", "add", "origin", "https://example.com/demo.git")
    real_git = module.git
    credentials = "example-user:example-value"

    def fake_git(repo, *args, **kwargs):
        if args[0] == "push":
            return subprocess.CompletedProcess(
                args, 1, "", "failed https://" + credentials + "@example.com/demo.git"
            )
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(module, "git", fake_git)
    assert invoke(setup, "run") == 6
    output = capsys.readouterr().err
    assert "https://example.com/demo.git" in output
    assert credentials not in output
    assert git(repo, "status", "--porcelain") == ""
