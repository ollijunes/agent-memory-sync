"""Collect files into a private staging tree without following links."""

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, SyncError, no_symlinks, read_relative, write_private
from .rules import reason


@dataclass
class Collection:
    files: list[dict] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def collect(config: Config, staging: Path) -> Collection:
    result = Collection()
    for name, source in config.sources.items():
        if not source.enabled:
            continue
        if source.root.is_symlink():
            result.skipped.append((name, "symlink"))
            continue
        no_symlinks(source.root)
        if not source.root.exists():
            continue
        if not source.root.is_dir():
            raise SyncError(f"{name}: source root is not a directory")

        def walk_error(error, name=name):
            raise SyncError(f"{name}: cannot walk source") from error

        for parent, directories, files in os.walk(
            source.root, followlinks=False, onerror=walk_error
        ):
            for directory in sorted(directories):
                path = Path(parent) / directory
                if path.is_symlink():
                    result.skipped.append((f"{name}/{path.relative_to(source.root)}", "symlink"))
                    directories.remove(directory)
            directories.sort()
            for filename in sorted(files):
                path = Path(parent) / filename
                relative = path.relative_to(source.root).as_posix()
                display = f"{name}/{relative}"
                skip = (
                    "symlink"
                    if path.is_symlink()
                    else reason(name, relative, source.include, source.exclude)
                )
                if skip is None:
                    try:
                        info = path.lstat()
                        if not stat.S_ISREG(info.st_mode):
                            skip = "binary"
                        elif info.st_size > config.max_file_bytes:
                            skip = "too-large"
                        else:
                            data = read_relative(source.root, relative, config.max_file_bytes)
                            if len(data) > config.max_file_bytes:
                                skip = "too-large"
                            elif b"\0" in data[:8192]:
                                skip = "binary"
                            else:
                                try:
                                    data.decode("utf-8")
                                except UnicodeDecodeError:
                                    skip = "not-utf8"
                    except OSError as exc:
                        raise SyncError(f"{display}: cannot safely read source file") from exc
                if skip:
                    result.skipped.append((display, skip))
                    continue
                write_private(staging / name / relative, data)
                result.files.append(
                    {
                        "source": name,
                        "path": relative,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data),
                    }
                )
    result.files.sort(key=lambda item: (item["source"], item["path"]))
    result.skipped.sort()
    return result
