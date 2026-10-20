import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_memory_sync.cli import doctor, main
from agent_memory_sync.config import SyncError, dump
from agent_memory_sync.gitops import visibility
from agent_memory_sync.schedule import LABEL, PATH, render, schedule, time_parts, unit_quote
from tests.conftest import git


def test_init_config_modes_force_and_root_rejection(setup, tmp_path):
    home, repo, _, _ = setup
    path = home / ".config/agent-memory-sync/config.toml"
    args = ["init", "--home", str(home), "--repo", str(repo), "--machine", "laptop", "--no-push"]
    assert main(args) == 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert main(args) == 1
    assert main([*args, "--force"]) == 0
    assert main([*args, "--force", "--repo", str(home / ".claude/backup")]) == 1
    assert main([*args, "--force", "--repo", str(home)]) == 1
    assert main([*args, "--machine", "bad label"]) == 1


def test_init_empty_and_missing_identity(isolated_home, tmp_path, monkeypatch):
    repo, path = tmp_path / "new", tmp_path / "config.toml"
    args = [
        "init",
        "--home",
        str(isolated_home),
        "--repo",
        str(repo),
        "--config",
        str(path),
        "--machine",
        "desktop",
    ]
    assert main(args) == 5
    git(repo, "config", "user.name", "Synthetic Example")
    git(repo, "config", "user.email", "synthetic@example.org")
    assert main([*args, "--remote", str(tmp_path / "bare.git")]) == 0
    assert git(repo, "ls-tree", "--name-only", "HEAD").splitlines() == [".gitignore", "README.md"]
    assert git(repo, "remote") == "origin"
    # Existing user files are preserved when attaching a config to a repository.
    (repo / "README.md").write_text("An existing synthetic README")
    assert main([*args, "--force"]) == 0
    assert (repo / "README.md").read_text() == "An existing synthetic README"


def test_visibility_fake_gh(setup, tmp_path, monkeypatch, capsys):
    home, repo, _, path = setup
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\nprint('{json.dumps({'visibility': 'PUBLIC'})}')\n")
    gh.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    git(repo, "remote", "add", "origin", "https://github.com/example/demo.git")
    assert "public" in visibility(repo)
    assert (
        main(
            [
                "init",
                "--home",
                str(home),
                "--config",
                str(path),
                "--repo",
                str(repo),
                "--machine",
                "laptop",
                "--force",
            ]
        )
        == 0
    )
    assert "warn:" in capsys.readouterr().out
    gh.write_text(f'#!{sys.executable}\nprint("invalid")\n')
    assert "could not determine" in visibility(repo)
    gh.write_text(f"#!{sys.executable}\nprint('{json.dumps({'visibility': 'PRIVATE'})}')\n")
    assert visibility(repo) is None
    git(repo, "remote", "set-url", "origin", "https://example.com/demo.git")
    assert visibility(repo) is None


def test_doctor_states(setup, fake_gitleaks, monkeypatch, capsys, tmp_path):
    home, repo, config, path = setup
    fake_gitleaks()
    assert doctor(path, home) == 0
    output = capsys.readouterr().out
    for name in ("git", "config", "machine", "repo", "git identity", "gitleaks"):
        assert f"ok: {name}" in output
    assert "warn: remote" in output
    git(repo, "config", "--unset", "user.email")
    assert doctor(path, home) == 1
    assert "fail: git identity" in capsys.readouterr().out
    config.repo = tmp_path / "missing"
    path.write_bytes(dump(config))
    assert doctor(path, home) == 1
    assert "fail: repo" in capsys.readouterr().out
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert doctor(path, home) == 1
    output = capsys.readouterr().out
    assert "fail: git" in output and "warn: gitleaks" in output
    config.scanner = "gitleaks"
    path.write_bytes(dump(config))
    assert doctor(path, home) == 1
    assert "fail: gitleaks" in capsys.readouterr().out
    path.unlink()
    assert doctor(path, home) == 1
    assert "fail: config" in capsys.readouterr().out


def test_cli_errors_and_version(isolated_home, capsys, monkeypatch):
    assert main([]) == 1
    assert main(["unknown"]) == 1
    assert main(["run"]) == 1
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert "0.1.0" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "7 gitleaks missing" in " ".join(capsys.readouterr().out.split())
    monkeypatch.setattr("agent_memory_sync.cli.load", lambda *a: (_ for _ in ()).throw(OSError()))
    assert main(["run"]) == 1
    assert "details withheld" in capsys.readouterr().err


def test_launchd_plist_and_systemd_units(isolated_home, tmp_path):
    path = tmp_path / "config with spaces.toml"
    files = render(isolated_home, path, "03:25", "launchd")
    data = plistlib.loads(next(iter(files.values())))
    assert data["Label"] == LABEL
    assert data["StartCalendarInterval"] == {"Hour": 3, "Minute": 25}
    assert data["ProcessType"] == "Background"
    assert data["LowPriorityIO"] is True
    assert data["Nice"] == 10 and data["RunAtLoad"] is False and "KeepAlive" not in data
    assert data["EnvironmentVariables"]["PATH"] == PATH
    assert data["ProgramArguments"][0] == sys.executable
    assert data["ProgramArguments"][-2:] == [str(path), "run"]
    if sys.platform == "darwin":
        file = tmp_path / "generated.plist"
        file.write_bytes(next(iter(files.values())))
        subprocess.run(["plutil", "-lint", str(file)], check=True)
    units = render(isolated_home, path, "03:25", "systemd")
    service, timer = [value.decode() for value in units.values()]
    assert "Type=oneshot" in service and "IOSchedulingClass=idle" in service
    assert "Nice=10" in service and f"Environment=PATH={PATH}" in service
    assert '"' + str(path) + '"' in service
    assert "OnCalendar=*-*-* 03:25:00" in timer
    assert "Persistent=true" in timer and "RandomizedDelaySec=5m" in timer
    assert "WantedBy=timers.target" in timer
    assert unit_quote('a"b%$\\') == '"a\\"b%%$$\\\\"'
    with pytest.raises(SyncError):
        unit_quote("bad\npath")


def test_systemd_analyze(isolated_home, tmp_path):
    if sys.platform != "linux" or not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze not available")
    units = render(isolated_home, tmp_path / "config.toml", "03:25", "systemd")
    paths = []
    for path, data in units.items():
        target = tmp_path / path.name
        target.write_bytes(data)
        paths.append(str(target))
    subprocess.run(["systemd-analyze", "calendar", "*-*-* 03:25:00"], check=True)
    subprocess.run(["systemd-analyze", "--user", "verify", *paths], check=True)


@pytest.mark.parametrize("at", ["25:00", "3:20", "00:60", "-1:00", "junk"])
def test_invalid_time(at):
    with pytest.raises(SyncError, match="HH:MM"):
        time_parts(at)


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
def test_scheduler_install_uninstall_mocked(isolated_home, tmp_path, monkeypatch, platform, capsys):
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if "bootout" in argv else 0)

    monkeypatch.setattr("agent_memory_sync.schedule.subprocess.run", fake)
    path = tmp_path / "config.toml"
    files = render(isolated_home, path, "04:15", platform)
    assert schedule("print", isolated_home, path, "04:15", platform) == 0
    assert schedule("install", isolated_home, path, "04:15", platform, True) == 0
    assert not calls and not any(p.exists() for p in files)
    assert schedule("install", isolated_home, path, "04:15", platform) == 0
    assert all(p.is_file() for p in files)
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in files)
    if platform == "launchd":
        assert [argv[0] for argv in calls] == ["plutil", "launchctl", "launchctl"]
        assert (isolated_home / "Library/Logs/agent-memory-sync").stat().st_mode & 0o777 == 0o700
    else:
        assert [argv[2] for argv in calls] == ["daemon-reload", "enable"]
    calls.clear()
    schedule("uninstall", isolated_home, path, platform=platform, dry_run=True)
    assert not calls
    assert all(p.exists() for p in files)
    schedule("uninstall", isolated_home, path, platform=platform)
    assert calls and not any(p.exists() for p in files)
    assert "remove" in capsys.readouterr().out


def test_schedule_cli_and_failure(isolated_home, tmp_path, monkeypatch):
    assert main(["schedule", "print", "--at", "12:30"]) == 0
    assert main(["schedule", "print", "--at", "invalid"]) == 1
    monkeypatch.setattr(
        "agent_memory_sync.schedule.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1),
    )
    with pytest.raises(SyncError, match="Scheduler command failed"):
        schedule("install", isolated_home, tmp_path / "config.toml", platform="systemd")


def test_init_cannot_write_config_into_source(setup):
    home, repo, _, _ = setup
    source = home / ".claude/CLAUDE.md"
    before = source.read_bytes()
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
                str(source),
                "--force",
            ]
        )
        == 1
    )
    assert source.read_bytes() == before


def test_shipped_templates_match_render(isolated_home, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", "/path/to/python")
    for platform in ("launchd", "systemd"):
        files = render(Path("/path/to/home"), Path("/path/to/config.toml"), "02:00", platform)
        for path, content in files.items():
            expected = content.replace(b"/path/to/python", b"@PYTHON@")
            expected = expected.replace(b"/path/to/home", b"@HOME@")
            expected = expected.replace(b"/path/to/config.toml", b"@CONFIG@")
            template = Path(__file__).parents[1] / "contrib" / platform / path.name
            assert template.read_bytes() == expected


def test_module_entrypoint(isolated_home):
    result = subprocess.run(
        [sys.executable, "-m", "agent_memory_sync", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "0.1.0"
