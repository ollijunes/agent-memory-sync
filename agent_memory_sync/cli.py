"""Command-line entry points."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import (
    Config,
    Source,
    SyncError,
    config_path,
    dump,
    expand,
    label,
    load,
    no_symlinks,
    private_dir,
    roots,
    separate,
    write_private,
)
from .gitops import git, identity, remote, repository, require_identity, run, visibility
from .restore import restore
from .schedule import schedule

EPILOG = (
    "Exit codes: 0 ok or no changes; 1 usage or config error; 3 secrets found; "
    "4 uncommitted changes in the backup folder; 5 no git identity; 6 push failed; "
    "7 gitleaks missing."
)
# Generated backup-repository text; reviewed separately from the public project documentation.
BACKUP_README = (
    "# Agent memory backup\n\n"
    "Keep this repository private. It holds Claude Code and Codex memory and "
    "instruction files, written by agent-memory-sync. Each top-level folder is one "
    "machine.\n\n"
    "Transcripts and credential files are never copied, and every run is scanned for "
    "secrets before it is committed. The scan can miss things, so do not share this "
    "repository.\n\n"
    "Preview a restore with `agent-memory-sync restore --from PATH --machine LABEL`. "
    "Add `--apply` to write files that are missing.\n"
)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SyncError(message)


def common(parser, suppress=False):
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--home", type=Path, default=default)
    parser.add_argument("--config", type=Path, default=default)


def parser() -> Parser:
    root = Parser(prog="agent-memory-sync", epilog=EPILOG)
    common(root)
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    init = commands.add_parser("init")
    common(init, True)
    init.add_argument("--repo", required=True)
    init.add_argument("--machine", required=True)
    init.add_argument("--remote")
    init.add_argument("--no-push", action="store_true")
    init.add_argument("--force", action="store_true")
    for name in ("run", "plan", "doctor"):
        sub = commands.add_parser(name)
        common(sub, True)
        if name == "run":
            sub.add_argument("--dry-run", action="store_true")
    sub = commands.add_parser("restore")
    common(sub, True)
    sub.add_argument("--from", dest="repo", required=True)
    sub.add_argument("--machine", required=True)
    sub.add_argument("--source", choices=("claude", "codex", "all"), default="all")
    sub.add_argument("--apply", action="store_true")
    sub.add_argument("--overwrite", action="store_true")
    sub = commands.add_parser("schedule")
    common(sub, True)
    actions = sub.add_subparsers(dest="action", required=True, parser_class=Parser)
    for name in ("print", "install", "uninstall"):
        action = actions.add_parser(name)
        common(action, True)
        action.add_argument("--at", required=name != "uninstall", default="02:00")
        action.add_argument("--platform", choices=("launchd", "systemd"))
        action.add_argument("--dry-run", action="store_true")
    return root


def initialize(args, home: Path, path: Path) -> int:
    config = Config(
        expand(args.repo, home),
        label(args.machine),
        {name: Source(root) for name, root in roots(home).items()},
        not args.no_push,
    )
    separate(config.repo, config.sources)
    for source in config.sources.values():
        if (
            path.resolve() == source.root.resolve()
            or source.root.resolve() in path.resolve().parents
        ):
            raise SyncError("Config must not be written inside a source root")
    no_symlinks(path)
    if path.exists() and not args.force:
        raise SyncError("Config already exists; use --force to replace it")
    private_dir(config.repo)
    present = git(config.repo, "rev-parse", "--show-toplevel", check=False)
    if present.returncode:
        git(config.repo, "init", "-b", "main")
    repository(config.repo)
    empty = git(config.repo, "rev-parse", "--verify", "HEAD", check=False).returncode != 0
    if empty:
        require_identity(config.repo)
    for filename, data in (
        ("README.md", BACKUP_README.encode()),
        (".gitignore", b"*.lock\n.DS_Store\n"),
    ):
        target = config.repo / filename
        if not target.exists():
            write_private(target, data)
    if empty:
        git(config.repo, "add", "--", "README.md", ".gitignore")
        git(
            config.repo,
            "commit",
            "--only",
            "-m",
            "Initialize private memory backup",
            "--",
            "README.md",
            ".gitignore",
        )
    if args.remote:
        git(config.repo, "remote", "add", "origin", args.remote)
    write_private(path, dump(config))
    warning = visibility(config.repo)
    if warning:
        print(f"warn: {warning}")
    print("Backup repository and config initialized")
    return 0


def doctor(path: Path, home: Path) -> int:
    failures = 0

    def report(state, name, detail=""):
        nonlocal failures
        print(f"{state}: {name}" + (f": {detail}" if detail else ""))
        failures += state == "fail"

    has_git = bool(shutil.which("git"))
    report("ok" if has_git else "fail", "git")
    try:
        config = load(path, home)
        report("ok", "config")
        report("ok", "machine", config.machine)
    except SyncError as exc:
        report("fail", "config", str(exc))
        config = None
    binary = shutil.which("gitleaks")
    if binary:
        result = subprocess.run([binary, "version"], capture_output=True, text=True, check=False)  # noqa: S603
        report(
            "ok" if result.returncode == 0 else "fail",
            "gitleaks",
            result.stdout.strip() if result.returncode == 0 else "version check failed",
        )
    else:
        report("warn" if config and config.scanner == "builtin" else "fail", "gitleaks", "missing")
    if config and has_git:
        try:
            separate(config.repo, config.sources)
            repository(config.repo)
            report("ok", "repo")
            report(
                "ok" if identity(config.repo) else "fail", "git identity", "git config user.email"
            )
            report("ok" if remote(config.repo) else "warn", "remote")
            warning = visibility(config.repo)
            if warning:
                report("warn", "visibility", warning)
        except SyncError as exc:
            report("fail", "repo", str(exc))
    return int(bool(failures))


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        home = (args.home or Path.home()).absolute().resolve()
        path = expand(str(args.config), home) if args.config else config_path(home)
        if args.command == "init":
            return initialize(args, home, path)
        if args.command == "doctor":
            return doctor(path, home)
        if args.command == "restore":
            return restore(
                expand(args.repo, home), args.machine, home, args.source, args.apply, args.overwrite
            )
        if args.command == "schedule":
            return schedule(args.action, home, path, args.at, args.platform, args.dry_run)
        return run(load(path, home), args.command == "plan" or args.dry_run)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except (OSError, UnicodeError):
        print(
            "error: filesystem or external command operation failed (details withheld)",
            file=sys.stderr,
        )
        return 1
