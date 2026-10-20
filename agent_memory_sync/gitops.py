"""Git operations scoped to one machine's backup directory."""

import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from .collect import collect
from .config import Config, SyncError, no_symlinks, separate, write_private
from .scan import rules_in, scan


def redact(text: str) -> str:
    return re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/]*@", r"\1", text)


def git(
    repo: Path,
    *args: str,
    check: bool = True,
    index: Path | None = None,
    input: bytes | None = None,
) -> subprocess.CompletedProcess:
    result = subprocess.run(  # noqa: S603 - argv, never a shell.
        ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        input=input,
        check=False,
        env={**os.environ, "GIT_INDEX_FILE": str(index)} if index else None,
    )
    result.stdout = result.stdout.decode("utf-8", errors="replace")
    result.stderr = result.stderr.decode("utf-8", errors="replace")
    if check and result.returncode:
        raise SyncError(f"git {args[0]} failed (output withheld)")
    return result


def repository(repo: Path) -> Path:
    no_symlinks(repo)
    result = git(repo, "rev-parse", "--show-toplevel", check=False)
    if result.returncode or Path(result.stdout.strip()).resolve() != repo.resolve():
        raise SyncError("repo must be the root of a Git work tree")
    directory = Path(git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    no_symlinks(directory)
    return directory


@contextmanager
def lock(repo: Path):
    directory = repository(repo)
    fd = os.open(
        directory / "agent-memory-sync.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "w") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def identity(repo: Path) -> bool:
    return all(
        git(repo, "config", "--get", key, check=False).stdout.strip()
        for key in ("user.name", "user.email")
    )


def require_identity(repo: Path) -> None:
    if not identity(repo):
        raise SyncError("Set git config user.name and git config user.email in the backup repo", 5)


def remote(repo: Path) -> bool:
    return bool(git(repo, "remote").stdout.strip())


def branch(repo: Path) -> str:
    result = git(repo, "symbolic-ref", "-q", "HEAD", check=False)
    if result.returncode:
        raise SyncError("Detached HEAD: run requires a branch (or an unborn branch)")
    return result.stdout.strip()


def push(repo: Path, enabled: bool) -> None:
    names = git(repo, "remote").stdout.splitlines() if enabled else []
    if names:
        name = "origin" if "origin" in names else names[0]
        result = git(repo, "push", name, f"HEAD:{branch(repo)}", check=False)
        if result.returncode:
            raise SyncError("Push failed: " + redact(result.stderr.strip()), 6)


def visibility(repo: Path) -> str | None:
    if not shutil.which("gh"):
        return None
    names = git(repo, "remote", check=False).stdout.splitlines()
    for name in names:
        url = git(repo, "remote", "get-url", name).stdout.strip()
        match = re.fullmatch(
            r"(?:https?://(?:[^/@]+@)?github\.com/|git@github\.com:)([^/?#]+/[^/?#]+?)(?:\.git)?",
            url,
        )
        if not match:
            continue
        result = subprocess.run(  # noqa: S603 - fixed gh command, no shell.
            ["gh", "repo", "view", match[1], "--json", "visibility"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            if result.returncode:
                return "could not determine remote repository visibility"
            state = json.loads(result.stdout).get("visibility")
            if state not in ("PUBLIC", "PRIVATE", "INTERNAL"):
                return "could not determine remote repository visibility"
            if state == "PUBLIC":
                return "remote repository is public; memory backups must stay private"
        except (ValueError, AttributeError):
            return "could not determine remote repository visibility"
    return None


def tree_files(root: Path) -> dict[str, Path]:
    no_symlinks(root)
    files = {}
    if not root.exists():
        return files
    if not root.is_dir():
        raise SyncError("Machine backup path must be a directory")
    for parent, directories, names in os.walk(root, followlinks=False):
        for name in (*directories, *names):
            path = Path(parent) / name
            if path.is_symlink():
                raise SyncError("Refusing a symlink in the machine backup tree")
            if path.is_file():
                files[path.relative_to(root).as_posix()] = path
            elif not path.is_dir():
                raise SyncError("Refusing a non-regular file in the machine backup tree")
    return files


def clean_stale_staging() -> None:
    cutoff = time.time() - 3600
    for path in Path(tempfile.gettempdir()).glob("agent-memory-sync-*"):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and info.st_mtime < cutoff:
            shutil.rmtree(path)  # rmtree does not traverse symlinked children.


@contextmanager
def staging_directory():
    def interrupted(signum, frame):
        raise SyncError(f"Run interrupted by signal {signum}")

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGHUP)}
    staging = None
    try:
        staging = Path(tempfile.mkdtemp(prefix="agent-memory-sync-")).resolve()
        yield staging
    finally:
        try:
            if staging is not None:
                shutil.rmtree(staging)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def committed_files(repo: Path, machine: str) -> dict[str, tuple[str, str]]:
    head = git(repo, "rev-parse", "--verify", "HEAD", check=False)
    if head.returncode:
        return {}
    entries = git(repo, "ls-tree", "-rz", "HEAD", "--", machine).stdout
    result = {}
    for entry in entries.split("\0"):
        if entry:
            metadata, path = entry.split("\t", 1)
            mode, _, oid = metadata.split()
            result[path.removeprefix(machine + "/")] = (mode, oid)
    return result


def commit_staging(config: Config, before: dict, after: dict) -> tuple[int, int]:
    expected = {
        key: (
            "100644",
            git(
                config.repo, "hash-object", "-w", "--no-filters", "--stdin", input=path.read_bytes()
            ).stdout.strip(),
        )
        for key, path in after.items()
    }
    changed = sum(before.get(key) != value for key, value in expected.items())
    removed = len(before.keys() - expected.keys())
    if changed or removed:
        with tempfile.TemporaryDirectory(prefix="agent-memory-sync-") as directory:
            index = Path(directory) / "index"
            head = git(config.repo, "rev-parse", "--verify", "HEAD", check=False)
            git(
                config.repo, "read-tree", "HEAD" if head.returncode == 0 else "--empty", index=index
            )
            if before:
                git(
                    config.repo,
                    "update-index",
                    "--force-remove",
                    "-z",
                    "--stdin",
                    index=index,
                    input="".join(f"{config.machine}/{key}\0" for key in before).encode(),
                )
            git(
                config.repo,
                "update-index",
                "--add",
                "-z",
                "--index-info",
                index=index,
                input="".join(
                    f"{mode} {oid}\t{config.machine}/{key}\0"
                    for key, (mode, oid) in expected.items()
                ).encode(),
            )
            tree = git(config.repo, "write-tree", index=index).stdout.strip()
            parent = ["-p", head.stdout.strip()] if head.returncode == 0 else []
            message = f"backup({config.machine}): {changed} changed, {removed} removed"
            commit = git(config.repo, "commit-tree", tree, *parent, "-m", message).stdout.strip()
            git(
                config.repo,
                "update-ref",
                branch(config.repo),
                commit,
                head.stdout.strip() if head.returncode == 0 else "0" * len(commit),
            )
    else:
        print("no changes")
    # Recompute from staging, independently of the index and the working tree.
    verified = {
        key: (
            "100644",
            git(
                config.repo, "hash-object", "--no-filters", "--stdin", input=path.read_bytes()
            ).stdout.strip(),
        )
        for key, path in after.items()
    }
    if committed_files(config.repo, config.machine) != verified:
        raise SyncError("Committed backup does not match scanned staging bytes")
    return changed, removed


def run(config: Config, dry_run: bool = False) -> int:
    separate(config.repo, config.sources)
    branch(config.repo)
    clean_stale_staging()
    with lock(config.repo) as acquired:
        if not acquired:
            print("another run is in progress")
            return 0
        target = config.repo / config.machine
        if not dry_run:
            status = git(
                config.repo, "status", "--porcelain", "--untracked-files=all", "--", config.machine
            ).stdout
            if status:
                raise SyncError("Uncommitted changes in the backup folder; review them first", 4)
            require_identity(config.repo)
        with staging_directory() as staging:
            result = collect(config, staging)
            findings = scan(staging, config.repo, config.scanner)
            if findings:
                for path, line, rule in findings:
                    print(f"{path}:{line}: {rule}")
                return 3
            if dry_run:
                for item in result.files:
                    print(f"include {item['source']}/{item['path']}")
                for path, reason in result.skipped:
                    if list(rules_in(path)):
                        path = path.split("/", 1)[0] + "/<path withheld>"
                    print(f"skip {path}: {reason}")
                return 0
            manifest = (json.dumps(result.files, indent=2, ensure_ascii=False) + "\n").encode()
            write_private(staging / "MANIFEST.json", manifest)
            before, after = committed_files(config.repo, config.machine), tree_files(staging)
            no_symlinks(target)
            for key in before.keys() | after.keys():
                no_symlinks(target / key)
            changed, removed = commit_staging(config, before, after)
            # Only previously tracked paths are removed; ignored housekeeping files stay put.
            for key in before.keys() - after.keys():
                (target / key).unlink()
            if target.exists():
                for parent, directories, _ in os.walk(target, topdown=False, followlinks=False):
                    for directory in directories:
                        path = Path(parent) / directory
                        if not path.is_symlink() and not any(path.iterdir()):
                            path.rmdir()
            for key, path in after.items():
                write_private(target / key, path.read_bytes())
            git(config.repo, "reset", "-q", "--", config.machine)
            push(config.repo, config.push)
            sha = git(config.repo, "rev-parse", "HEAD").stdout.strip()
            counts = Counter(reason for _, reason in result.skipped)
            skipped = ",".join(f"{key}={value}" for key, value in sorted(counts.items())) or "0"
            print(
                f"copied={changed} unchanged={len(after) - changed} removed={removed} "
                f"skipped={skipped} commit={sha}"
            )
            return 0
