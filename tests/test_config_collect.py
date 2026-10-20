import os

import pytest

from agent_memory_sync.collect import collect
from agent_memory_sync.config import (
    SyncError,
    config_path,
    dump,
    expand,
    load,
    no_symlinks,
    read_relative,
    roots,
    separate,
)


def test_config_roundtrip_environment(setup, monkeypatch):
    home, _, config, path = setup
    assert load(path, home) == config
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "custom-claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / "custom-codex"))
    assert roots(home) == {"claude": home / "custom-claude", "codex": home / "custom-codex"}
    path.write_text('repo = "~/backup"\nmachine = "desktop"\n')
    loaded = load(path, home)
    assert loaded.sources["claude"].root == home / "custom-claude"
    assert loaded.sources["codex"].root == home / "custom-codex"
    assert loaded.repo == home / "backup"
    assert config_path(home) == home / ".config/agent-memory-sync/config.toml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert config_path(home) == home / ".config/agent-memory-sync/config.toml"
    config.repo = home / 'quote"and\\slash'
    path.write_bytes(dump(config))
    assert load(path, home).repo == config.repo


@pytest.mark.parametrize(
    "text,key",
    [
        ("wat = true", "wat"),
        ("push = 1", "push"),
        ("max_file_bytes = false", "max_file_bytes"),
        ("max_file_bytes = 0", "max_file_bytes"),
        ('scanner = "none"', "scanner"),
        ("sources = []", "sources"),
        ("[sources.other]", "sources.other"),
        ("[sources]\nclaude = []", "sources.claude"),
        ("[sources.claude]\nwat = 1", "sources.claude.wat"),
        ("[sources.claude]\nenabled = 1", "enabled"),
        ("[sources.claude]\nroot = 1", "root"),
        ("[sources.claude]\ninclude = [1]", "include"),
        ('[sources.claude]\nexclude = "a"', "exclude"),
    ],
)
def test_invalid_config(setup, text, key):
    home, _, _, path = setup
    path.write_text('repo = "~/backup"\nmachine = "demo"\n' + text)
    with pytest.raises(SyncError, match=key):
        load(path, home)


@pytest.mark.parametrize(
    "text", ["", "repo = 1", 'repo = ""', "[invalid", 'repo = "x"\nmachine = "../escape"']
)
def test_missing_or_malformed(setup, text):
    home, _, _, path = setup
    path.write_text(text)
    with pytest.raises(SyncError):
        load(path, home)
    path.unlink()
    with pytest.raises(SyncError):
        load(path, home)


def test_path_helpers(setup, tmp_path):
    home, _, config, _ = setup
    assert expand("~", home) == home
    with pytest.raises(SyncError):
        expand("~someone/path", home)
    with pytest.raises(SyncError):
        separate(home, config.sources)
    (home / "link").symlink_to(home / ".claude", target_is_directory=True)
    with pytest.raises(SyncError):
        no_symlinks(home / "link/new")
    for relative in ("../x", "/x", "a//b", "a\\b"):
        with pytest.raises(SyncError):
            read_relative(home, relative)
    if hasattr(os, "mkfifo"):
        os.mkfifo(home / "fifo")
        with pytest.raises(SyncError, match="regular"):
            read_relative(home, "fifo")


def test_collect_rules_and_permissions(setup, tmp_path):
    home, _, config, _ = setup
    root = home / ".claude"
    directory = root / "rules"
    directory.mkdir()
    (directory / "large.md").write_text("a" * 101)
    (directory / "binary.md").write_bytes(b"hello\0world")
    (directory / "invalid.md").write_bytes(b"\xff")
    (directory / "link.md").symlink_to(root / "CLAUDE.md")
    (directory / "linkdir").symlink_to(root / "projects", target_is_directory=True)
    (directory / "ok.md").write_text("Prefer tabs.\n")
    (root / "unlisted.txt").write_text("Synthetic note")
    if hasattr(os, "mkfifo"):
        os.mkfifo(directory / "pipe.md")
    config.max_file_bytes = 100
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    result = collect(config, staging)
    skipped = dict(result.skipped)
    for filename, reason in [
        ("large.md", "too-large"),
        ("binary.md", "binary"),
        ("invalid.md", "not-utf8"),
        ("link.md", "symlink"),
        ("linkdir", "symlink"),
    ]:
        assert skipped[f"claude/rules/{filename}"] == reason
    assert skipped["claude/unlisted.txt"] == "not-included"
    assert len(result.files) == 5
    for path in [staging, *staging.rglob("*")]:
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    assert result.files == sorted(result.files, key=lambda item: (item["source"], item["path"]))


def test_collect_disabled_missing_and_symlink_root(setup, tmp_path):
    home, _, config, _ = setup
    config.sources["codex"].enabled = False
    config.sources["claude"].root = home / "missing"
    assert collect(config, tmp_path / "stage").files == []
    (home / "missing").symlink_to(home / ".claude", target_is_directory=True)
    assert collect(config, tmp_path / "stage").skipped == [("claude", "symlink")]
    config.sources["claude"].root = home / ".claude/CLAUDE.md"
    with pytest.raises(SyncError, match="directory"):
        collect(config, tmp_path / "stage")


def test_collect_read_failure_and_growth(setup, tmp_path, monkeypatch):
    _, _, config, _ = setup
    import agent_memory_sync.collect as module

    monkeypatch.setattr(module, "read_relative", lambda *a: b"a" * (config.max_file_bytes + 1))
    result = collect(config, tmp_path / "stage")
    assert any(reason == "too-large" for _, reason in result.skipped)

    def fail(*args):
        raise OSError("contents must not be printed")

    monkeypatch.setattr(module, "read_relative", fail)
    with pytest.raises(SyncError, match="cannot safely read"):
        collect(config, tmp_path / "stage")


def test_walk_failure(setup, tmp_path, monkeypatch):
    _, _, config, _ = setup

    def fail(*args, **kwargs):
        kwargs["onerror"](OSError("denied"))

    monkeypatch.setattr(os, "walk", fail)
    with pytest.raises(SyncError, match="cannot walk"):
        collect(config, tmp_path / "stage")


def test_unicode_glob_roundtrip_and_nul_path(setup):
    home, _, config, path = setup
    config.sources["claude"].include = ["notes/📝.md"]
    path.write_bytes(dump(config))
    assert load(path, home) == config
    with pytest.raises(SyncError, match="NUL"):
        expand("bad\0path", home)
