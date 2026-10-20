import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_memory_sync.config import Config, Source, dump, write_private


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    for key in list(os.environ):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")) or key in {
            "CLAUDE_CONFIG_DIR",
            "CODEX_HOME",
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG",
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
            "EMAIL",
        }:
            monkeypatch.delenv(key)
    return home


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def setup(tmp_path, isolated_home):
    home = isolated_home
    shutil.copytree(Path(__file__).parents[1] / "examples/home", home, dirs_exist_ok=True)
    repo = tmp_path / "backup"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Synthetic Example")
    git(repo, "config", "user.email", "synthetic@example.com")
    git(repo, "commit", "--allow-empty", "-m", "Synthetic fixture")
    config = Config(
        repo,
        "laptop",
        {name: Source(home / f".{name}") for name in ("claude", "codex")},
        scanner="builtin",
    )
    path = tmp_path / "config.toml"
    write_private(path, dump(config))
    return home, repo, config, path


@pytest.fixture
def fake_gitleaks(tmp_path, monkeypatch):
    import sys

    directory = tmp_path / "bin"
    directory.mkdir()
    binary = directory / "gitleaks"
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ.get("PATH", ""))

    def make(code=0, report=None):
        binary.write_text(f"""#!{sys.executable}
import json
import pathlib
import sys
if sys.argv[1:] == ["version"]:
    print("8.24.3")
    sys.exit(0)
pathlib.Path({str(tmp_path / "scanner-argv.json")!r}).write_text(json.dumps(sys.argv[1:]))
report = {report!r}
if report is not None:
    pathlib.Path(sys.argv[sys.argv.index("-r") + 1]).write_text(json.dumps(report))
sys.exit({code})
""")
        binary.chmod(0o700)
        return binary

    return make
