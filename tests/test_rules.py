import pytest

from agent_memory_sync.rules import EXCLUDES, INCLUDES, matches, reason

INCLUDED = {
    "claude": [
        "CLAUDE.md",
        "rules/style.md",
        "projects/demo/memory/MEMORY.md",
        "agent-memory/demo/MEMORY.md",
        "settings.json",
        "agents/demo.txt",
        "commands/demo.sh",
        "skills/demo/SKILL.md",
        "output-styles/demo.md",
        "hooks/demo.py",
        "statusline.sh",
        "keybindings.json",
    ],
    "codex": [
        "AGENTS.md",
        "AGENTS.override.md",
        "config.toml",
        "demo.config.toml",
        "rules/a.rules",
        "prompts/a.md",
        "skills/a/SKILL.md",
        "agents/a.toml",
        "hooks.json",
        "memories/MEMORY.md",
    ],
}
EXCLUDED = [
    ".claude.json",
    ".credentials.json",
    "auth.json",
    "a.pem",
    "a.key",
    ".env",
    ".env.demo",
    "TOKEN.md",
    "mySecret.md",
    "credentials.txt",
    "password.md",
    "a.jsonl",
    "a.sqlite",
    "a.sqlite-wal",
    "a.db",
    "cache/a.md",
    "plugins/a.md",
    "file-history/a.md",
    "shell-snapshots/a.md",
    "shell_snapshots/a.md",
    "session-env/a.md",
    "sessions/a.md",
    "archived_sessions/a.md",
    "log/a.md",
    "logs/a.md",
    "debug/a.md",
    "node_modules/a.md",
    ".git/a.md",
    "a.bak1",
    "a.lock",
    "settings.local.json",
    "projects/-private-var-folders-demo/memory/MEMORY.md",
    "projects/-tmp-demo/memory/MEMORY.md",
    "projects/-private-tmp-demo/memory/MEMORY.md",
    "projects/-var-folders-demo/memory/MEMORY.md",
]


@pytest.mark.parametrize(
    "source,path", [(source, path) for source, paths in INCLUDED.items() for path in paths]
)
def test_every_include(source, path):
    assert reason(source, path) is None
    assert len(INCLUDED[source]) == len(INCLUDES[source])


@pytest.mark.parametrize("path,pattern", list(zip(EXCLUDED, EXCLUDES, strict=True)))
def test_every_exclude_always_wins(path, pattern):
    assert reason("claude", path, include=["**"]) == f"excluded:{pattern}"
    assert reason("codex", path, include=["**"]) == f"excluded:{pattern}"


@pytest.mark.parametrize("path", ["rules/style.md", "rules/a/b/style.md"])
def test_zero_or_many_depth(path):
    assert matches(path, "rules/**/*.md")


def test_user_rules():
    assert reason("claude", "custom.txt") == "not-included"
    assert reason("claude", "custom.txt", include=["*.txt"]) is None
    assert reason("claude", "CLAUDE.md", exclude=["CLAUDE.md"]) == "excluded:CLAUDE.md"
    assert reason("claude", "skills/SECRET/a.md").startswith("excluded:")
    assert not matches("rules/a/b.md", "rules/*.md")


def test_settings_local_name_rule_at_any_depth():
    assert reason("claude", "skills/demo/settings.local.json") == "excluded:**/settings.local.json"
