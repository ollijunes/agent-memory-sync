"""Secret detection that reports locations and rule identifiers only."""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import SyncError

PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "api-key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    "slack-token": re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"),
    "google-api-key": re.compile(r"\bAIza[A-Za-z0-9_-]{20,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{8,}"),
    "json-token": re.compile(r'"(?:access|refresh|id)_token"\s*:\s*"'),
    "credential-assignment": re.compile(
        r"""(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['"]?([^\s'"<>]{12,})""",
        re.I,
    ),
}
REFERENCE = re.compile(
    r"^(?:\$[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\}|"
    r"env:[A-Za-z_][A-Za-z0-9_]*|op://\S+)$"
)
MARKERS = ("# agent-memory-sync: allow", "<!-- agent-memory-sync: allow -->")


def builtin(staging: Path) -> list[tuple[str, int, str]]:
    findings = []
    for path in sorted(staging.rglob("*")):
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Inline markers are an explicit allowlist for this scanner only, never for gitleaks.
            if line.rstrip().endswith(MARKERS):
                continue
            for rule, pattern in PATTERNS.items():
                for match in pattern.finditer(line):
                    if rule == "credential-assignment" and REFERENCE.fullmatch(match[1]):
                        continue
                    if rule == "json-token":
                        value = line[match.end() :].split('"', 1)[0]
                        if REFERENCE.fullmatch(value):
                            continue
                    findings.append((path.relative_to(staging).as_posix(), number, rule))
                    break
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
    with tempfile.TemporaryDirectory(prefix="agent-memory-scan-") as directory:
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
        if completed.returncode == 1:
            try:
                records = json.loads(report.read_text(encoding="utf-8"))
                if not isinstance(records, list) or not records:
                    raise ValueError
                for record in records:
                    path = Path(record["File"])
                    if path.is_absolute():
                        path = path.relative_to(staging)
                    if (
                        ".." in path.parts
                        or not path.parts
                        or path.parts[0] not in ("claude", "codex")
                    ):
                        raise ValueError
                    rule = record["RuleID"]
                    line = record["StartLine"]
                    if not isinstance(rule, str) or not re.fullmatch(r"[\w.-]{1,100}", rule):
                        raise ValueError
                    if type(line) is not int or line < 1:
                        raise ValueError
                    findings.append((path.as_posix(), line, rule))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise SyncError("Invalid gitleaks report; backup blocked") from exc
    return sorted(set(findings))
