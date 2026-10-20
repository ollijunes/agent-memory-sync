"""Secret detection that reports locations and rule identifiers only."""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import SyncError, safe_relative

PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "api-key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    "slack-token": re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"),
    "google-api-key": re.compile(r"\bAIza[A-Za-z0-9_-]{20,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{8,}"),
    "json-token": re.compile(r'"(?:access|refresh|id)_token"\s*:\s*"([^"\n]*)"'),
    "credential-assignment": re.compile(
        r"""(?:password|passwd|secret|api[_-]?key|token)['"]?\s*[:=]\s*(?:"([^"\n]*)"|'([^'\n]*)'|([^\s'"]+))""",
        re.I,
    ),
}
REFERENCE = re.compile(
    r"^(?:\$[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\}|"
    r"env:[A-Za-z_][A-Za-z0-9_]*|op://[A-Za-z0-9 ._-]+(?:/[A-Za-z0-9 ._-]+){2,3})$"
)
MARKERS = ("# agent-memory-sync: allow", "<!-- agent-memory-sync: allow -->")


def rules_in(text: str):
    for rule, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            if rule in ("credential-assignment", "json-token"):
                value = next(group for group in match.groups() if group is not None)
                if REFERENCE.fullmatch(value):
                    continue
            yield rule
            break


def builtin(staging: Path) -> list[tuple[str, int, str]]:
    findings = []
    for path in sorted(staging.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(staging).as_posix()
        path_rules = list(rules_in(relative))
        display = relative.split("/", 1)[0] + "/<path withheld>" if path_rules else relative
        findings.extend((display, 0, rule) for rule in path_rules)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Inline markers allow content only; they never exempt paths or gitleaks.
            if line.rstrip().endswith(MARKERS):
                continue
            findings.extend((display, number, rule) for rule in rules_in(line))
    return findings


def scan(staging: Path, repo: Path, scanner: str) -> list[tuple[str, int, str]]:
    findings = builtin(staging)
    if scanner == "builtin":
        return findings
    binary = shutil.which("gitleaks")
    if not binary:
        raise SyncError(
            "gitleaks missing: install gitleaks from https://github.com/gitleaks/gitleaks "
            'or opt in to scanner = "builtin" in config',
            7,
        )
    with tempfile.TemporaryDirectory(prefix="agent-memory-sync-") as directory:
        report = Path(directory) / "report.json"
        command = [
            binary,
            "dir",
            "--redact",
            "--no-banner",
            "--exit-code",
            "1",
            "-f",
            "json",
            "-r",
            str(report),
        ]
        allowlist = repo / ".gitleaks.toml"
        if allowlist.is_symlink():
            raise SyncError("Refusing a symlinked .gitleaks.toml")
        if allowlist.is_file():
            command.extend(["-c", str(allowlist)])
        command.append(str(staging))
        completed = subprocess.run(command, capture_output=True, check=False)  # noqa: S603
        if completed.returncode not in (0, 1):
            raise SyncError("gitleaks scan failed; backup blocked (scanner output withheld)")
        try:
            records = json.loads(report.read_text(encoding="utf-8"))
            if not isinstance(records, list) or bool(records) != (completed.returncode == 1):
                raise ValueError
            for record in records:
                path = Path(record["File"])
                if path.is_absolute():
                    path = path.relative_to(staging)
                if not safe_relative(path.as_posix()) or path.parts[0] not in ("claude", "codex"):
                    raise ValueError
                rule = record["RuleID"]
                line = record["StartLine"]
                if not isinstance(rule, str) or not re.fullmatch(r"[\w.-]{1,100}", rule):
                    raise ValueError
                if type(line) is not int or line < 1:
                    raise ValueError
                display = path.as_posix()
                if list(rules_in(display)):
                    display = path.parts[0] + "/<path withheld>"
                findings.append((display, line, rule))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SyncError("Invalid gitleaks report; backup blocked") from exc
    return sorted(set(findings))
