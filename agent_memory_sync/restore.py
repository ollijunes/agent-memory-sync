"""Manifest-driven restore with explicit writes and conflict backups."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from .config import (
    SyncError,
    label,
    no_symlinks,
    read_relative,
    roots,
    safe_relative,
    write_private,
)


def restore(
    repo: Path,
    machine: str,
    home: Path,
    source: str = "all",
    apply: bool = False,
    overwrite: bool = False,
) -> int:
    base = repo / label(machine)
    no_symlinks(base)
    try:
        entries = json.loads(read_relative(base, "MANIFEST.json"))
    except (OSError, ValueError) as exc:
        raise SyncError("Cannot read backup MANIFEST.json") from exc
    if not isinstance(entries, list):
        raise SyncError("Manifest must be a list")
    targets, actions, seen = roots(home), [], set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"source", "path", "sha256", "bytes"}:
            raise SyncError("Invalid manifest entry")
        name, relative = entry["source"], entry["path"]
        if not isinstance(name, str) or name not in targets or not isinstance(relative, str):
            raise SyncError("Invalid manifest source or path")
        if not safe_relative(relative):
            raise SyncError("Unsafe manifest path")
        key = (name, relative)
        if key in seen:
            raise SyncError("Duplicate manifest path")
        seen.add(key)
        if source != "all" and source != name:
            continue
        destination = targets[name] / relative
        no_symlinks(destination)
        no_symlinks(base / name / relative)
        try:
            data = read_relative(base / name, relative)
        except OSError as exc:
            raise SyncError(f"{name}/{relative}: cannot read backup file") from exc
        if (
            type(entry["bytes"]) is not int
            or len(data) != entry["bytes"]
            or hashlib.sha256(data).hexdigest() != entry["sha256"]
        ):
            raise SyncError(f"{name}/{relative}: manifest integrity mismatch")
        if destination.exists():
            if not destination.is_file():
                raise SyncError(f"{name}/{relative}: destination is not a regular file")
            current = read_relative(targets[name], relative)
            if current == data:
                actions.append(("unchanged", destination, data, None))
                continue
            actions.append(("overwrite" if overwrite else "conflict", destination, data, current))
        else:
            actions.append(("write", destination, data, None))
    # Validate the complete selected manifest before applying even the first file.
    conflicts = False
    for action, destination, data, current in actions:
        if apply and action in ("write", "overwrite"):

            def recheck(destination=destination, current=current):
                if destination.is_symlink():
                    raise FileExistsError
                no_symlinks(destination)
                if current is not None:
                    if (
                        not destination.is_file()
                        or read_relative(destination.parent, destination.name) != current
                    ):
                        raise FileExistsError
                    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                    backup = destination.with_name(
                        f"{destination.name}.agent-memory-sync-{stamp}.bak"
                    )
                    write_private(backup, current, exclusive=True)

            try:
                write_private(destination, data, exclusive=current is None, before_replace=recheck)
            except FileExistsError:
                action = "conflict"
        conflicts |= action == "conflict"
        print(f"{action}: {destination}")
    return 2 if conflicts else 0
