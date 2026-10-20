"""Allowlist with exclusions that cannot be overridden."""

import fnmatch
from functools import lru_cache

INCLUDES = {
    "claude": (
        "CLAUDE.md",
        "rules/**/*.md",
        "projects/*/memory/**/*.md",
        "agent-memory/**/*.md",
        "settings.json",
        "agents/**",
        "commands/**",
        "skills/**",
        "output-styles/**",
        "hooks/**",
        "statusline.sh",
        "keybindings.json",
    ),
    "codex": (
        "AGENTS.md",
        "AGENTS.override.md",
        "config.toml",
        "*.config.toml",
        "rules/**",
        "prompts/**",
        "skills/**",
        "agents/**",
        "hooks.json",
        "memories/**/*.md",
    ),
}
EXCLUDES = (
    "**/.credentials.json",
    "**/auth.json",
    "**/*.pem",
    "**/*.key",
    "**/.env",
    "**/.env.*",
    "**/*token*",
    "**/*secret*",
    "**/*credential*",
    "**/*password*",
    "**/*.jsonl",
    "**/*.sqlite",
    "**/*.sqlite-*",
    "**/*.db",
    "**/cache/**",
    "**/plugins/**",
    "**/file-history/**",
    "**/shell-snapshots/**",
    "**/shell_snapshots/**",
    "**/session-env/**",
    "**/sessions/**",
    "**/archived_sessions/**",
    "**/log/**",
    "**/logs/**",
    "**/debug/**",
    "**/node_modules/**",
    "**/.git/**",
    "**/*.bak*",
    "**/*.lock",
    "settings.local.json",
    "projects/-private-var-folders-*/**",
    "projects/-tmp-*/**",
    "projects/-private-tmp-*/**",
    "projects/-var-folders-*/**",
)
NAME_RULES = {"**/*token*", "**/*secret*", "**/*credential*", "**/*password*"}


@lru_cache(maxsize=512)
def matches(path: str, pattern: str) -> bool:
    parts, globs = path.split("/"), pattern.split("/")

    def walk(i: int, j: int) -> bool:
        if j == len(globs):
            return i == len(parts)
        if globs[j] == "**":
            return walk(i, j + 1) or (i < len(parts) and walk(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], globs[j]) and walk(i + 1, j + 1)

    return walk(0, 0)


def reason(source: str, path: str, include=(), exclude=()) -> str | None:
    # User includes add coverage, but can never override a built-in exclusion.
    for pattern in (*EXCLUDES, *exclude):
        candidate = path.lower() if pattern in NAME_RULES else path
        if (
            (pattern == "settings.local.json" and path.rsplit("/", 1)[-1] == pattern)
            or matches(candidate, pattern)
            or (
                pattern in NAME_RULES
                and any(
                    fnmatch.fnmatchcase(part.lower(), pattern.rsplit("/", 1)[-1])
                    for part in path.split("/")
                )
            )
        ):
            return f"excluded:{pattern}"
    if not any(matches(path, pattern) for pattern in (*INCLUDES[source], *include)):
        return "not-included"
    return None
