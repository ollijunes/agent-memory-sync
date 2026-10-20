import json

import pytest

from agent_memory_sync.config import SyncError
from agent_memory_sync.scan import PATTERNS, builtin, scan


def canaries():
    return {
        "private-key": "-----BEGIN " + "PRIVATE KEY-----",
        "aws-access-key": "AK" + "IA" + "A" * 16,
        "github-token": "gh" + "p_" + "a" * 36,
        "api-key": "s" + "k-" + "a" * 32,
        "slack-token": "xo" + "xb-" + "1234567890" * 3,
        "google-api-key": "AI" + "za" + "a" * 35,
        "jwt": "ey" + "J" + "a" * 10 + "." + "b" * 12 + "." + "c" * 12,
        "json-token": '"access_' + 'token": "' + "a" * 16 + '"',
        "credential-assignment": "pass" + "word = " + "a" * 16,
    }


@pytest.mark.parametrize("rule", list(PATTERNS))
def test_builtin_rule(tmp_path, rule, capsys):
    stage = tmp_path / "stage/claude"
    stage.mkdir(parents=True)
    text = canaries()[rule]
    (stage / "CLAUDE.md").write_text(text)
    findings = builtin(stage.parent)
    assert ("claude/CLAUDE.md", 1, rule) in findings
    assert text not in repr(findings)
    assert not capsys.readouterr().out


@pytest.mark.parametrize("prefix", ["ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"])
def test_github_variants(tmp_path, prefix):
    (tmp_path / "note.md").write_text(prefix + "a" * 40)
    assert builtin(tmp_path)[0][2] == "github-token"


@pytest.mark.parametrize(
    "marker", ["# agent-memory-sync: allow", "<!-- agent-memory-sync: allow -->"]
)
def test_inline_allow(tmp_path, marker):
    path = tmp_path / "note.md"
    path.write_text(canaries()["api-key"] + " " + marker + "\n")
    assert builtin(tmp_path) == []
    path.write_text(canaries()["api-key"] + " " + marker + " trailing text")
    assert builtin(tmp_path)


@pytest.mark.parametrize(
    "value",
    [
        "$SYNTHETIC_VARIABLE",
        "${SYNTHETIC_VARIABLE}",
        "env:SYNTHETIC_VARIABLE",
        "op://example/item/field",
    ],
)
def test_references(tmp_path, value):
    (tmp_path / "note.md").write_text(f'api_key = "{value}"\n"access_token": "{value}"')
    assert builtin(tmp_path) == []


def test_gitleaks_clean_config_and_builtin(setup, tmp_path, fake_gitleaks):
    _, repo, _, _ = setup
    fake_gitleaks()
    stage = tmp_path / "stage"
    (stage / "claude").mkdir(parents=True)
    assert scan(stage, repo, "gitleaks") == []
    argv_path = tmp_path / "scanner-argv.json"
    assert "-c" not in json.loads(argv_path.read_text())
    (repo / ".gitleaks.toml").write_text("title = 'Synthetic allowlist'\n")
    (stage / "claude/CLAUDE.md").write_text(canaries()["api-key"])
    assert scan(stage, repo, "gitleaks") == [("claude/CLAUDE.md", 1, "api-key")]
    argv = json.loads(argv_path.read_text())
    assert argv[argv.index("-c") + 1] == str(repo / ".gitleaks.toml")
    assert "--redact" in argv


def test_gitleaks_findings(setup, tmp_path, fake_gitleaks):
    _, repo, _, _ = setup
    stage = tmp_path / "stage"
    stage.mkdir()
    fake_gitleaks(
        1,
        [
            {
                "File": str(stage / "claude/CLAUDE.md"),
                "StartLine": 2,
                "RuleID": "example-rule",
                "Secret": "withheld value",
            }
        ],
    )
    assert scan(stage, repo, "gitleaks") == [("claude/CLAUDE.md", 2, "example-rule")]
    fake_gitleaks(1, [{"File": "codex/AGENTS.md", "StartLine": 3, "RuleID": "example-rule"}])
    assert scan(stage, repo, "gitleaks") == [("codex/AGENTS.md", 3, "example-rule")]


@pytest.mark.parametrize(
    "report",
    [
        None,
        [],
        {},
        [{}],
        [{"File": "../outside"}],
        [{"File": "/outside", "StartLine": 1, "RuleID": "demo"}],
        [{"File": "claude/a", "StartLine": 0, "RuleID": "demo"}],
        [{"File": "claude/a", "StartLine": 1, "RuleID": "unsafe value"}],
    ],
)
def test_invalid_report_blocks(setup, tmp_path, fake_gitleaks, report):
    _, repo, _, _ = setup
    fake_gitleaks(1, report)
    with pytest.raises(SyncError, match="Invalid gitleaks report"):
        scan(tmp_path / "stage", repo, "gitleaks")


def test_scanner_failure_and_missing(setup, tmp_path, fake_gitleaks, monkeypatch):
    _, repo, _, _ = setup
    fake_gitleaks(2)
    with pytest.raises(SyncError, match="scan failed"):
        scan(tmp_path / "stage", repo, "gitleaks")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(SyncError) as caught:
        scan(tmp_path / "stage", repo, "gitleaks")
    assert caught.value.code == 7
    assert 'scanner = "builtin"' in str(caught.value)


def test_allowlist_symlink_refused(setup, tmp_path, fake_gitleaks):
    _, repo, _, _ = setup
    fake_gitleaks()
    (repo / ".gitleaks.toml").symlink_to(tmp_path / "outside")
    with pytest.raises(SyncError, match="symlinked"):
        scan(tmp_path / "stage", repo, "gitleaks")


def test_prefixed_keyword_and_short_jwt_payload(tmp_path):
    (tmp_path / "note.md").write_text("SYNTHETIC_API_KEY=" + "a" * 16)
    assert builtin(tmp_path)[0][2] == "credential-assignment"
    value = "ey" + "J" + "a" * 10 + ".e30." + "b" * 12
    (tmp_path / "note.md").write_text(value)
    assert builtin(tmp_path)[0][2] == "jwt"
