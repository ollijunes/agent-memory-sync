"""Strict configuration and filesystem boundaries."""

import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class SyncError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def label(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9-]{1,32}", value):
        raise SyncError("machine must match [a-z0-9-]{1,32}")
    return value


def expand(value: str, home: Path) -> Path:
    if "\0" in value:
        raise SyncError("Paths cannot contain NUL characters")
    if value == "~" or value.startswith("~/"):
        return home / value[2:] if value != "~" else home
    if value.startswith("~"):
        raise SyncError("Only ~ or ~/ paths are supported")
    return Path(os.path.abspath(value))


def config_path(home: Path) -> Path:
    base = expand(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")), home)
    return base / "agent-memory-sync/config.toml"


def roots(home: Path) -> dict[str, Path]:
    return {
        "claude": expand(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude")), home),
        "codex": expand(os.environ.get("CODEX_HOME", str(home / ".codex")), home),
    }


def no_symlinks(path: Path) -> None:
    # Check lexically: resolving first would hide the links we need to reject.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SyncError("Refusing a symlink in a filesystem path")


def private_dir(path: Path) -> None:
    no_symlinks(path)
    if not path.exists():
        private_dir(path.parent)
        path.mkdir(mode=0o700)


def write_private(path: Path, data: bytes) -> None:
    private_dir(path.parent)
    no_symlinks(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)


def read_relative(root: Path, relative: str, limit: int | None = None) -> bytes:
    parts = relative.split("/")
    if not parts or any(p in ("", ".", "..") for p in parts) or "\\" in relative:
        raise SyncError("Unsafe relative path")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise SyncError("Not a regular file")
            return stream.read() if limit is None else stream.read(limit + 1)
    finally:
        os.close(fd)


@dataclass
class Source:
    root: Path
    enabled: bool = True
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class Config:
    repo: Path
    machine: str
    sources: dict[str, Source]
    push: bool = True
    scanner: str = "gitleaks"
    max_file_bytes: int = 1048576


def unknown(data: dict, allowed: set[str], prefix: str = "") -> None:
    for key in data.keys() - allowed:
        raise SyncError(f"Unknown config key: {prefix}{key}")


def load(path: Path, home: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SyncError("Cannot read config: expected a readable TOML file") from exc
    unknown(data, {"repo", "machine", "push", "scanner", "max_file_bytes", "sources"})
    for key in ("repo", "machine"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise SyncError(f"Config {key} must be a nonempty string")
    push = data.get("push", True)
    size = data.get("max_file_bytes", 1048576)
    if type(push) is not bool:
        raise SyncError("Config push must be a boolean")
    if type(size) is not int or size < 1:
        raise SyncError("Config max_file_bytes must be a positive integer")
    scanner = data.get("scanner", "gitleaks")
    if scanner not in ("gitleaks", "builtin"):
        raise SyncError("Config scanner must be gitleaks or builtin")
    raw_sources = data.get("sources", {})
    if not isinstance(raw_sources, dict):
        raise SyncError("Config sources must be a table")
    unknown(raw_sources, {"claude", "codex"}, "sources.")
    sources = {}
    for name, default in roots(home).items():
        raw = raw_sources.get(name, {})
        if not isinstance(raw, dict):
            raise SyncError(f"Config sources.{name} must be a table")
        unknown(raw, {"root", "enabled", "include", "exclude"}, f"sources.{name}.")
        if type(raw.get("enabled", True)) is not bool:
            raise SyncError(f"Config sources.{name}.enabled must be a boolean")
        if not isinstance(raw.get("root", ""), str):
            raise SyncError(f"Config sources.{name}.root must be a string")
        for key in ("include", "exclude"):
            values = raw.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise SyncError(f"Config sources.{name}.{key} must be an array of strings")
        sources[name] = Source(
            expand(raw["root"], home) if "root" in raw else default,
            raw.get("enabled", True),
            raw.get("include", []),
            raw.get("exclude", []),
        )
    return Config(expand(data["repo"], home), label(data["machine"]), sources, push, scanner, size)


def dump(config: Config) -> bytes:
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)  # noqa: E731
    lines = [
        f"repo = {quote(config.repo)}",
        f"machine = {quote(config.machine)}",
        f"push = {str(config.push).lower()}",
        f"scanner = {quote(config.scanner)}",
        f"max_file_bytes = {config.max_file_bytes}",
    ]
    for name, source in config.sources.items():
        lines.extend(
            [
                "",
                f"[sources.{name}]",
                f"enabled = {str(source.enabled).lower()}",
                f"root = {quote(source.root)}",
                f"include = {json.dumps(source.include, ensure_ascii=False)}",
                f"exclude = {json.dumps(source.exclude, ensure_ascii=False)}",
            ]
        )
    return ("\n".join(lines) + "\n").encode()


def separate(repo: Path, sources: dict[str, Source]) -> None:
    dest = repo.resolve()
    for source in sources.values():
        root = source.root.resolve()
        if dest == root or dest in root.parents or root in dest.parents:
            raise SyncError("Backup repository must not overlap a source root")
