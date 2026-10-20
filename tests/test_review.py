"""Regression tests for review findings, using synthetic homes and runtime canaries."""

import os
import plistlib
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from agent_memory_sync.cli import main
from agent_memory_sync.collect import collect
from agent_memory_sync.config import SyncError, safe_relative, write_private
from agent_memory_sync.gitops import clean_stale_staging, run, visibility
from agent_memory_sync.restore import restore
from agent_memory_sync.rules import reason
from agent_memory_sync.scan import builtin, scan
from agent_memory_sync.schedule import scheduler_path
from tests.conftest import git
from tests.test_rules import EXCLUDED
from tests.test_scan import canaries


@pytest.mark.parametrize("path", EXCLUDED)
@pytest.mark.parametrize("prefix", ["", "skills/x/"])
def test_builtin_exclusions_ignore_case_at_any_depth(path, prefix):
    assert reason("claude", prefix + path.upper(), include=["**"]).startswith("excluded:")


def test_user_globs_remain_case_sensitive():
    assert reason("claude", "custom.MD", include=["custom.md"]) == "not-included"
    assert reason("claude", "CLAUDE.md", exclude=["claude.md"]) is None
    assert reason("claude", "skills/MyTOKEN/note.md").startswith("excluded:")


@pytest.mark.parametrize(
    "code,report",
    [(0, [{}]), (1, []), (0, None), (1, None), (0, {}), (1, {}), (0, "text"), (1, "text")],
)
def test_inconsistent_gitleaks_reports(setup, tmp_path, fake_gitleaks, code, report):
    fake_gitleaks(code, report)
    with pytest.raises(SyncError, match="Invalid gitleaks report; backup blocked"):
        scan(tmp_path / "stage", setup[1], "gitleaks")


@pytest.mark.parametrize("code", [0, 1])
def test_malformed_gitleaks_json(setup, tmp_path, fake_gitleaks, code):
    fake_gitleaks(code, raw="{invalid")
    with pytest.raises(SyncError, match="Invalid gitleaks report; backup blocked"):
        scan(tmp_path / "stage", setup[1], "gitleaks")


@pytest.mark.parametrize("dry_run", [True, False])
def test_path_secret_is_withheld_and_blocks(setup, capsys, dry_run):
    home, repo, config, _ = setup
    value = canaries()["api-key"]
    path = home / ".claude/skills" / value / "note.md"
    path.parent.mkdir(parents=True)
    path.write_text(value)
    before = git(repo, "rev-parse", "HEAD")
    assert run(config, dry_run=dry_run) == 3
    output = capsys.readouterr()
    assert "claude/<path withheld>:0: api-key" in output.out
    assert value not in output.out + output.err
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo, "status", "--porcelain") == ""
    assert not (repo / "laptop").exists()


@pytest.mark.parametrize(
    "name",
    [
        "bad:name.md",
        "back\\slash.md",
        "line\nfile.md",
        "del\x7f.md",
        "tab\tfile.md",
        "control\x85.md",
    ],
)
def test_collect_skips_unsafe_names(setup, tmp_path, name):
    home, _, config, _ = setup
    folder = home / ".claude/skills"
    folder.mkdir()
    (folder / name).write_text("Synthetic note")
    result = collect(config, tmp_path / "stage")
    assert ("claude/<path withheld>", "unsafe-name") in result.skipped
    assert not any(row["path"] == f"skills/{name}" for row in result.files)


@pytest.mark.parametrize("name", [".", "..", "a/./b", "a/../b", "/a", "a//b", "a/"])
def test_shared_path_validator_rejects_components(name):
    assert not safe_relative(name)


def test_collect_skips_hard_links(setup, tmp_path):
    home, _, config, _ = setup
    os.link(home / ".claude/CLAUDE.md", tmp_path / "other")
    result = collect(config, tmp_path / "stage")
    assert ("claude/CLAUDE.md", "hard-link") in result.skipped
    assert not (tmp_path / "stage/claude/CLAUDE.md").exists()


def test_atomic_write_preserves_other_inode_and_fsyncs(tmp_path, monkeypatch):
    import agent_memory_sync.config as module

    path, linked = tmp_path / "dest", tmp_path / "linked"
    path.write_bytes(b"before")
    os.link(path, linked)
    syncs = []
    actual_fsync = os.fsync

    def fsync(fd):
        syncs.append(os.fstat(fd).st_mode & 0o777)
        actual_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", fsync)
    write_private(path, b"after")
    assert path.read_bytes() == b"after" and linked.read_bytes() == b"before"
    assert path.stat().st_ino != linked.stat().st_ino
    assert syncs == [0o600]
    with pytest.raises(FileExistsError):
        write_private(path, b"collision", exclusive=True)
    assert path.read_bytes() == b"after"
    assert not list(tmp_path.glob(".agent-memory-sync-*"))


def test_backup_replaces_hardlinked_destination(setup, tmp_path):
    home, repo, config, _ = setup
    run(config)
    target = repo / "laptop/claude/CLAUDE.md"
    old = target.read_bytes()
    os.link(target, tmp_path / "linked")
    (home / ".claude/CLAUDE.md").write_text("Updated instructions")
    run(config)
    assert (tmp_path / "linked").read_bytes() == old
    assert target.read_text() == "Updated instructions"


def test_commit_uses_staging_bytes_despite_filter_and_worktree_change(setup, monkeypatch):
    import agent_memory_sync.gitops as module

    home, repo, config, _ = setup
    (repo / ".gitattributes").write_text("laptop/** filter=alter text\n")
    git(repo, "config", "filter.alter.clean", "printf FILTERED")
    git(repo, "config", "filter.alter.smudge", "cat")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-m", "Synthetic filter")
    git(repo, "config", "core.autocrlf", "true")
    git(repo, "config", "core.safecrlf", "true")
    content = b"Scanned instructions\r\nWith CRLF\r\n"
    (home / ".claude/CLAUDE.md").write_bytes(content)
    original = module.commit_staging

    def inject_worktree(config, before, after):
        write_private(repo / "laptop/claude/CLAUDE.md", b"Unscanned worktree bytes")
        return original(config, before, after)

    monkeypatch.setattr(module, "commit_staging", inject_worktree)
    assert run(config) == 0
    for path in (repo / "laptop").rglob("*"):
        if path.is_file():
            committed = subprocess.check_output(
                ["git", "-C", str(repo), "show", "HEAD:" + path.relative_to(repo).as_posix()]
            )
            assert committed == path.read_bytes()
    assert (repo / "laptop/claude/CLAUDE.md").read_bytes() == content
    assert git(repo, "log", "-1", "--format=%b") == ""


def test_commit_verification_blocks_worktree_update(setup, monkeypatch):
    import agent_memory_sync.gitops as module

    _, repo, config, _ = setup
    actual = module.committed_files
    calls = 0

    def corrupt(*args):
        nonlocal calls
        calls += 1
        return actual(*args) if calls == 1 else {}

    monkeypatch.setattr(module, "committed_files", corrupt)
    with pytest.raises(SyncError, match="does not match scanned"):
        run(config)
    assert not (repo / "laptop").exists()


@pytest.mark.parametrize("existing", [("README.md",), (".gitignore",), ("README.md", ".gitignore")])
def test_init_only_commits_created_files(isolated_home, tmp_path, existing):
    repo = tmp_path / "new"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Synthetic Example")
    git(repo, "config", "user.email", "synthetic@example.com")
    for name in existing:
        (repo / name).write_text("Existing user work\n")
        git(repo, "add", name)
    assert (
        main(
            [
                "init",
                "--home",
                str(isolated_home),
                "--repo",
                str(repo),
                "--machine",
                "demo",
                "--config",
                str(tmp_path / "config.toml"),
            ]
        )
        == 0
    )
    for name in existing:
        assert (repo / name).read_text() == "Existing user work\n"
    if len(existing) == 2:
        assert (
            subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"], capture_output=True
            ).returncode
            != 0
        )
    else:
        assert set(git(repo, "ls-tree", "--name-only", "HEAD").splitlines()) == {
            "README.md",
            ".gitignore",
        } - set(existing)
        assert set(git(repo, "diff", "--cached", "--name-only").splitlines()) == set(existing)


def test_init_existing_history_does_not_commit(setup):
    home, repo, _, path = setup
    before = git(repo, "rev-parse", "HEAD")
    assert (
        main(
            [
                "init",
                "--home",
                str(home),
                "--repo",
                str(repo),
                "--machine",
                "demo",
                "--config",
                str(path),
                "--force",
            ]
        )
        == 0
    )
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo, "diff", "--cached", "--name-only") == ""


@pytest.mark.parametrize(
    "assignment", ['"api_key": "{}"', "'token': '{}'", "password: {}", 'password = "{}"']
)
@pytest.mark.parametrize(
    "value",
    [
        "value",
        "${REF}x",
        "a" * 16,
        "${REF}actualvalue",
        "$REF" + "!" * 15,
        "op://vault/item/field?extra",
        "op://vault/item/field/extra/more",
        "op://vault/item/field@extra",
        "op://vault/item/field\\extra",
    ],
)
def test_complete_assignment_values(tmp_path, assignment, value):
    (tmp_path / "note.md").write_text(assignment.format(value))
    assert any(rule == "credential-assignment" for _, _, rule in builtin(tmp_path))


@pytest.mark.parametrize(
    "value",
    [
        "$NAME",
        "${NAME}",
        "env:NAME",
        "op://vault/item/field",
        "op://My vault/My item/My section/My field",
    ],
)
def test_only_entire_references_exempt(tmp_path, value):
    (tmp_path / "note.md").write_text(f"\"api_key\": \"{value}\"\n'token': '{value}'")
    assert builtin(tmp_path) == []


@pytest.mark.parametrize("apply", [False, True])
def test_restore_conflicts_exit_two(setup, tmp_path, apply):
    _, repo, config, _ = setup
    run(config)
    target = tmp_path / "restored"
    write_private(target / ".claude/CLAUDE.md", b"User work")
    assert restore(repo, "laptop", target, apply=apply) == 2
    assert (target / ".claude/CLAUDE.md").read_bytes() == b"User work"


@pytest.mark.parametrize("overwrite", [False, True])
def test_restore_changes_after_preview_are_conflicts(setup, tmp_path, monkeypatch, overwrite):
    import agent_memory_sync.restore as module

    _, repo, config, _ = setup
    run(config)
    home = tmp_path / "restored"
    destination = home / ".claude/CLAUDE.md"
    if overwrite:
        write_private(destination, b"Preview bytes")
    actual = module.write_private

    def raced(path, data, **kwargs):
        if path == destination:
            actual(path, b"Concurrent user edit")
        return actual(path, data, **kwargs)

    monkeypatch.setattr(module, "write_private", raced)
    assert restore(repo, "laptop", home, apply=True, overwrite=overwrite) == 2
    assert destination.read_bytes() == b"Concurrent user edit"
    assert not list(home.rglob("*.bak"))


def test_restore_hardlink_and_exclusive_backup(setup, tmp_path, monkeypatch):
    import agent_memory_sync.restore as module

    _, repo, config, _ = setup
    run(config)
    home = tmp_path / "restored"
    destination = home / ".claude/CLAUDE.md"
    write_private(destination, b"Preview bytes")
    os.link(destination, tmp_path / "linked")
    assert restore(repo, "laptop", home, apply=True, overwrite=True) == 0
    assert (tmp_path / "linked").read_bytes() == b"Preview bytes"
    assert next(home.rglob("*.bak")).read_bytes() == b"Preview bytes"
    write_private(destination, b"Another edit")
    actual = module.write_private

    def collide(path, data, **kwargs):
        if path.suffix == ".bak":
            actual(path, b"Existing backup")
            assert kwargs["exclusive"]
        return actual(path, data, **kwargs)

    monkeypatch.setattr(module, "write_private", collide)
    assert restore(repo, "laptop", home, apply=True, overwrite=True) == 2
    assert destination.read_bytes() == b"Another edit"
    assert b"Existing backup" in [path.read_bytes() for path in home.rglob("*.bak")]


def test_ignored_housekeeping_survives_backup_and_removal(setup):
    home, repo, config, _ = setup
    run(config)
    (repo / ".git/info/exclude").write_text(".DS_Store\n")
    house = repo / "laptop/claude/.DS_Store"
    house.write_bytes(b"Housekeeping")
    (home / ".claude/CLAUDE.md").unlink()
    assert run(config) == 0
    assert house.read_bytes() == b"Housekeeping"
    assert ".DS_Store" not in git(repo, "ls-tree", "-r", "--name-only", "HEAD")
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_signals_cleanup_and_restore_handlers(setup, monkeypatch, sig):
    import agent_memory_sync.gitops as module

    previous = signal.getsignal(sig)
    staging = []

    def interrupted(config, path):
        staging.append(path)
        (path / "sensitive-stage").write_text("Synthetic staging data")
        os.kill(os.getpid(), sig)

    monkeypatch.setattr(module, "collect", interrupted)
    with pytest.raises(SyncError, match="interrupted by signal"):
        run(setup[2])
    assert staging and not staging[0].exists()
    assert signal.getsignal(sig) == previous


def test_stale_staging_only_owned_old_directories(tmp_path, monkeypatch):
    root = Path(tempfile.gettempdir())
    old = root / "agent-memory-sync-old"
    fresh = root / "agent-memory-sync-fresh"
    foreign = root / "agent-memory-sync-foreign"
    unrelated = root / "unrelated-old"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("Keep")
    for path in (old, fresh, foreign, unrelated):
        path.mkdir()
        (path / "link").symlink_to(outside, target_is_directory=True)
        if path != fresh:
            os.utime(path, (time.time() - 7200,) * 2)
    link = root / "agent-memory-sync-link"
    link.symlink_to(outside, target_is_directory=True)
    os.utime(link, (time.time() - 7200,) * 2, follow_symlinks=False)
    actual = Path.lstat

    def lstat(path, *args, **kwargs):
        result = actual(path, *args, **kwargs)
        if path == foreign:
            values = list(result)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "lstat", lstat)
    clean_stale_staging()
    assert not old.exists()
    assert all(path.exists() for path in (fresh, foreign, unrelated, link, outside / "keep"))


def test_detached_head_rejected_before_any_write(setup, capsys):
    home, repo, _, config_path = setup
    git(repo, "checkout", "--detach", "HEAD")
    assert main(["--home", str(home), "--config", str(config_path), "run"]) == 1
    assert "Detached HEAD" in capsys.readouterr().err
    assert not (repo / ".git/agent-memory-sync.lock").exists()
    assert not (repo / "laptop").exists()


@pytest.mark.parametrize("remote", ["origin", "archive"])
def test_push_explicit_branch_refspec(setup, tmp_path, monkeypatch, remote):
    import agent_memory_sync.gitops as module

    _, repo, config, _ = setup
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git(bare, "init", "--bare")
    git(repo, "remote", "add", remote, str(bare))
    git(repo, "config", "push.default", "nothing")
    git(repo, "switch", "-c", "backup/nested")
    actual = module.git
    pushes = []

    def record(repo, *args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
        return actual(repo, *args, **kwargs)

    monkeypatch.setattr(module, "git", record)
    assert run(config) == 0
    assert pushes == [("push", remote, "HEAD:refs/heads/backup/nested")]
    assert git(bare, "rev-parse", "refs/heads/backup/nested") == git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
def test_scheduler_resolved_path_and_default_time(
    isolated_home, tmp_path, monkeypatch, capsys, platform
):
    binaries = tmp_path / "custom-tools"
    binaries.mkdir()
    tools = {name: binaries / name for name in ("git", "gitleaks")}
    for path in tools.values():
        path.touch()
    monkeypatch.setattr("agent_memory_sync.schedule.shutil.which", lambda name: str(tools[name]))
    path = scheduler_path(isolated_home, platform).split(":")
    assert path[:2] == [str(binaries), str(isolated_home / ".local/bin")]
    assert len(path) == len(set(path))
    assert "/usr/bin" in path
    assert ("/opt/homebrew/bin" in path) == (platform == "launchd")
    assert main(["schedule", "print", "--home", str(isolated_home), "--platform", platform]) == 0
    output = capsys.readouterr().out
    if platform == "launchd":
        data = plistlib.loads(output.split("\n", 1)[1].encode())
        assert data["StartCalendarInterval"] == {"Hour": 2, "Minute": 0}
        assert data["EnvironmentVariables"]["PATH"] == ":".join(path)
    else:
        assert "OnCalendar=*-*-* 02:00:00" in output
        assert ":".join(path) in output
    monkeypatch.setattr("agent_memory_sync.schedule.shutil.which", lambda name: None)
    assert scheduler_path(isolated_home, platform).startswith(str(isolated_home / ".local/bin"))


@pytest.mark.parametrize("code,output", [(1, ""), (0, "not json"), (0, "{}"), (0, "[]")])
def test_visibility_failures_warn(setup, monkeypatch, code, output):
    import agent_memory_sync.gitops as module

    repo = setup[1]
    git(repo, "remote", "add", "origin", "https://github.com/example/demo.git")
    actual = subprocess.run

    def fake(argv, **kwargs):
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, code, output, "private error")
        return actual(argv, **kwargs)

    monkeypatch.setattr(module.shutil, "which", lambda name: "/synthetic/bin/gh")
    monkeypatch.setattr(module.subprocess, "run", fake)
    assert visibility(repo) == "could not determine remote repository visibility"


@pytest.mark.integration
def test_real_gitleaks_only_canary(setup, tmp_path, capsys):
    if not shutil.which("gitleaks"):
        pytest.skip("gitleaks not installed")
    home, repo, config, _ = setup
    config.scanner = "gitleaks"
    assert run(config) == 0
    before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    value = "sk_" + "live_" + "Q7mK9pR2vN4xT8bL6cD3fH5j"
    (home / ".claude/CLAUDE.md").write_text(value)
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "note.md").write_text(value)
    assert builtin(probe) == []
    capsys.readouterr()
    assert run(config) == 3
    output = capsys.readouterr()
    assert value not in output.out + output.err
    assert "stripe" in output.out
    after = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    assert after == before
    assert all(value.encode() not in data for data in after.values())


@pytest.mark.parametrize("code", [0, 1])
def test_unreadable_gitleaks_report(setup, tmp_path, fake_gitleaks, monkeypatch, code):
    fake_gitleaks(
        code, [] if code == 0 else [{"File": "claude/CLAUDE.md", "StartLine": 1, "RuleID": "demo"}]
    )
    original = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path.name == "report.json":
            raise PermissionError
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(SyncError, match="Invalid gitleaks report; backup blocked"):
        scan(tmp_path / "stage", setup[1], "gitleaks")


def test_gitleaks_success_with_valid_findings_is_invalid(setup, tmp_path, fake_gitleaks):
    fake_gitleaks(0, [{"File": "claude/CLAUDE.md", "StartLine": 1, "RuleID": "demo"}])
    with pytest.raises(SyncError, match="Invalid gitleaks report; backup blocked"):
        scan(tmp_path / "stage", setup[1], "gitleaks")


def test_gitleaks_output_is_never_printed(setup, tmp_path, monkeypatch, capsys):
    import agent_memory_sync.scan as module

    value = canaries()["github-token"]

    def fail(argv, **kwargs):
        assert kwargs["capture_output"]
        return subprocess.CompletedProcess(argv, 2, value.encode(), value.encode())

    monkeypatch.setattr(module.shutil, "which", lambda name: "/synthetic/bin/gitleaks")
    monkeypatch.setattr(module.subprocess, "run", fail)
    with pytest.raises(SyncError, match="output withheld") as caught:
        scan(tmp_path / "stage", setup[1], "gitleaks")
    output = capsys.readouterr()
    assert value not in str(caught.value) + output.out + output.err


@pytest.mark.parametrize("overwrite", [False, True])
def test_restore_destination_becomes_symlink(setup, tmp_path, monkeypatch, overwrite):
    import agent_memory_sync.restore as module

    _, repo, config, _ = setup
    run(config)
    home = tmp_path / "restore"
    destination = home / ".claude/CLAUDE.md"
    destination.parent.mkdir(parents=True)
    if overwrite:
        destination.write_bytes(b"Preview")
    outside = tmp_path / "outside"
    outside.write_bytes(b"Keep")
    original = module.write_private

    def change(path, data, **kwargs):
        if path == destination:
            path.unlink(missing_ok=True)
            path.symlink_to(outside)
        return original(path, data, **kwargs)

    monkeypatch.setattr(module, "write_private", change)
    assert restore(repo, "laptop", home, apply=True, overwrite=overwrite) == 2
    assert destination.is_symlink()
    assert outside.read_bytes() == b"Keep"


def test_plan_withholds_secrets_in_skipped_paths(setup, capsys):
    home, _, config, _ = setup
    value = canaries()["api-key"]
    (home / ".claude" / (value + ".jsonl")).write_text("Excluded")
    assert run(config, dry_run=True) == 0
    output = capsys.readouterr()
    assert value not in output.out + output.err
    assert "skip claude/<path withheld>" in output.out
