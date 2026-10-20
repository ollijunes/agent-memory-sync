"""Git operations scoped to one machine's backup directory."""

import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from .collect import collect
from .config import Config, SyncError, no_symlinks, separate, write_private
from .scan import scan


def redact(text: str) -> str:
    return re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/]*@", r"\1", text)


def git(
    repo: Path, *args: str, check: bool = True, index: Path | None = None
) -> subprocess.CompletedProcess:
    result = subprocess.run(  # noqa: S603 - argv, never a shell.
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_INDEX_FILE": str(index)} if index else None,
    )
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


def push(repo: Path, enabled: bool) -> None:
    if enabled and remote(repo):
        # Explicit refspec also works for a freshly initialized branch without an upstream.
        result = git(repo, "push", "origin", "HEAD", check=False)
        if result.returncode:
            # A repository may use a remote name other than origin.
            names = git(repo, "remote").stdout.splitlines()
            if "origin" not in names:
                result = git(repo, "push", names[0], "HEAD", check=False)
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
            if result.returncode == 0 and json.loads(result.stdout).get("visibility") == "PUBLIC":
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


def run(config: Config, dry_run: bool = False) -> int:
    separate(config.repo, config.sources)
    with lock(config.repo) as acquired:
        if not acquired:
            print("another run is in progress")
            return 0
        target = config.repo / config.machine
        if not dry_run:
            status = git(
                config.repo,
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--ignored",
                "--",
                config.machine,
            ).stdout
            if status:
                raise SyncError("Uncommitted changes in the backup folder; review them first", 4)
            tree_files(target)
            require_identity(config.repo)
        staging = Path(tempfile.mkdtemp(prefix="agent-memory-sync-")).resolve()
        try:
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
                    print(f"skip {path}: {reason}")
                return 0
            manifest = (json.dumps(result.files, indent=2, ensure_ascii=False) + "\n").encode()
            write_private(staging / "MANIFEST.json", manifest)
            before, after = tree_files(target), tree_files(staging)
            removed = sorted(before.keys() - after.keys())
            changed = sorted(
                key
                for key in after
                if key not in before or before[key].read_bytes() != after[key].read_bytes()
            )
            unchanged = len(after) - len(changed)
            # Remove old paths first so file-to-directory transitions can be mirrored.
            for key in removed:
                before[key].unlink()
            if target.exists():
                for parent, directories, _ in os.walk(target, topdown=False):
                    for directory in directories:
                        path = Path(parent) / directory
                        if not any(path.iterdir()):
                            path.rmdir()
            for key in changed:
                write_private(target / key, after[key].read_bytes())
            git(config.repo, "add", "-A", "--", config.machine)
            staged = git(
                config.repo, "diff", "--cached", "--quiet", "--", config.machine, check=False
            )
            if staged.returncode == 0:
                print("no changes")
            elif staged.returncode == 1:
                message = (
                    f"backup({config.machine}): {len(changed)} changed, {len(removed)} removed"
                )
                paths = [f"{config.machine}/{key}" for key in [*changed, *removed]][:50]
                # A temporary index preserves unrelated staged work and handles path type changes.
                with tempfile.TemporaryDirectory(prefix="agent-memory-index-") as directory:
                    index = Path(directory) / "index"
                    head = git(config.repo, "rev-parse", "--verify", "HEAD", check=False)
                    git(
                        config.repo,
                        "read-tree",
                        "HEAD" if head.returncode == 0 else "--empty",
                        index=index,
                    )
                    git(config.repo, "add", "-A", "--", config.machine, index=index)
                    git(config.repo, "commit", "-m", message, "-m", "\n".join(paths), index=index)
            else:
                raise SyncError("Cannot inspect staged backup changes")
            push(config.repo, config.push)
            sha = git(config.repo, "rev-parse", "HEAD").stdout.strip()
            counts = Counter(reason for _, reason in result.skipped)
            skipped = ",".join(f"{key}={value}" for key, value in sorted(counts.items())) or "0"
            print(
                f"copied={len(changed)} unchanged={unchanged} removed={len(removed)} "
                f"skipped={skipped} commit={sha}"
            )
            return 0
        finally:
            shutil.rmtree(staging)
