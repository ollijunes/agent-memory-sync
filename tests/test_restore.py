import json

import pytest

from agent_memory_sync.cli import main
from agent_memory_sync.config import SyncError
from agent_memory_sync.gitops import run
from agent_memory_sync.restore import restore


def test_preview_apply_conflict_and_overwrite(setup, tmp_path, capsys):
    home, repo, config, _ = setup
    run(config)
    target = tmp_path / "restore-home"
    assert main(["restore", "--from", str(repo), "--machine", "laptop", "--home", str(target)]) == 0
    assert "write:" in capsys.readouterr().out
    assert not target.exists()
    restore(repo, "laptop", target, apply=True)
    note = target / ".claude/CLAUDE.md"
    assert note.read_bytes() == (home / ".claude/CLAUDE.md").read_bytes()
    for path in target.rglob("*"):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    note.write_text("A conflicting synthetic note.")
    restore(repo, "laptop", target, apply=True)
    assert "conflict:" in capsys.readouterr().out
    assert note.read_text() == "A conflicting synthetic note."
    restore(repo, "laptop", target, apply=True, overwrite=True)
    assert next(note.parent.glob("*.bak")).read_text() == "A conflicting synthetic note."
    assert note.read_bytes() == (home / ".claude/CLAUDE.md").read_bytes()
    restore(repo, "laptop", target, apply=True)
    assert "unchanged:" in capsys.readouterr().out
    only = tmp_path / "only"
    restore(repo, "laptop", only, source="codex", apply=True)
    assert not (only / ".claude").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        "/escape.md",
        "a/../../escape.md",
        "a//b",
        "a\\b",
        ".",
        "",
        "a:b",
        "line\nfile",
    ],
)
def test_manifest_path_escape(setup, tmp_path, path):
    _, repo, config, _ = setup
    run(config)
    manifest = repo / "laptop/MANIFEST.json"
    entries = json.loads(manifest.read_text())
    entries[-1]["path"] = path
    manifest.write_text(json.dumps(entries))
    with pytest.raises(SyncError, match="Unsafe"):
        restore(repo, "laptop", tmp_path / "target", apply=True)
    assert not (tmp_path / "target").exists()


def test_manifest_and_destination_symlinks(setup, tmp_path):
    _, repo, config, _ = setup
    run(config)
    target = tmp_path / "target"
    target.mkdir()
    (target / ".claude").symlink_to(tmp_path / "outside")
    with pytest.raises(SyncError, match="symlink"):
        restore(repo, "laptop", target, apply=True)
    (target / ".claude").unlink()
    source = repo / "laptop/claude/CLAUDE.md"
    source.unlink()
    source.symlink_to(tmp_path / "outside")
    with pytest.raises(SyncError, match="symlink"):
        restore(repo, "laptop", target, apply=True)


@pytest.mark.parametrize(
    "variant",
    [
        "object",
        "bad-entry",
        "bad-source",
        "bad-path-type",
        "duplicate",
        "hash",
        "bytes",
        "missing",
        "invalid-json",
    ],
)
def test_manifest_errors_before_write(setup, tmp_path, variant):
    _, repo, config, _ = setup
    run(config)
    manifest = repo / "laptop/MANIFEST.json"
    entries = json.loads(manifest.read_text())
    if variant == "object":
        entries = {}
    elif variant == "bad-entry":
        entries[-1] = {}
    elif variant == "bad-source":
        entries[-1]["source"] = "unknown"
    elif variant == "bad-path-type":
        entries[-1]["path"] = 1
    elif variant == "duplicate":
        entries.append(entries[0])
    elif variant == "hash":
        entries[-1]["sha256"] = "0" * 64
    elif variant == "bytes":
        entries[-1]["bytes"] = -1
    elif variant == "missing":
        (repo / "laptop" / entries[-1]["source"] / entries[-1]["path"]).unlink()
    manifest.write_text("invalid" if variant == "invalid-json" else json.dumps(entries))
    with pytest.raises(SyncError):
        restore(repo, "laptop", tmp_path / "target", apply=True)
    assert not (tmp_path / "target").exists()


def test_restore_directory_conflict(setup, tmp_path):
    _, repo, config, _ = setup
    run(config)
    target = tmp_path / "target"
    (target / ".claude/CLAUDE.md").mkdir(parents=True)
    with pytest.raises(SyncError, match="not a regular"):
        restore(repo, "laptop", target, apply=True)
