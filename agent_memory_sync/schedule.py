"""User scheduler generation and explicitly requested installation."""

import os
import plistlib
import re
import shlex
import subprocess
import sys
from pathlib import Path

from .config import SyncError, no_symlinks, private_dir, write_private

LABEL = "com.github.ollijunes.agent-memory-sync"
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def time_parts(at: str) -> tuple[int, int]:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
        raise SyncError("--at must be HH:MM (00:00 through 23:59)")
    hour, minute = at.split(":")
    return int(hour), int(minute)


def command(home: Path, config: Path) -> list[str]:
    return [
        str(Path(sys.executable).absolute()),
        "-m",
        "agent_memory_sync",
        "--home",
        str(home),
        "--config",
        str(config),
        "run",
    ]


def unit_quote(value: str) -> str:
    if any(c in value for c in "\n\r\0"):
        raise SyncError("Scheduler paths cannot contain control characters")
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


def render(home: Path, config: Path, at: str, platform: str) -> dict[Path, bytes]:
    hour, minute = time_parts(at)
    argv = command(home, config)
    if platform == "launchd":
        log = home / "Library/Logs/agent-memory-sync"
        data = {
            "Label": LABEL,
            "ProgramArguments": argv,
            "StartCalendarInterval": {"Hour": hour, "Minute": minute},
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "Nice": 10,
            "RunAtLoad": False,
            "EnvironmentVariables": {"PATH": PATH},
            "StandardOutPath": str(log / "stdout.log"),
            "StandardErrorPath": str(log / "stderr.log"),
        }
        return {home / f"Library/LaunchAgents/{LABEL}.plist": plistlib.dumps(data)}
    directory = home / ".config/systemd/user"
    service = (
        "[Unit]\nDescription=Agent memory backup\n\n[Service]\nType=oneshot\n"
        f"ExecStart={' '.join(unit_quote(arg) for arg in argv)}\n"
        f"Nice=10\nIOSchedulingClass=idle\nEnvironment=PATH={PATH}\n"
    )
    timer = (
        "[Unit]\nDescription=Daily agent memory backup\n\n[Timer]\n"
        f"OnCalendar=*-*-* {at}:00\nPersistent=true\nRandomizedDelaySec=5m\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return {
        directory / "agent-memory-sync.service": service.encode(),
        directory / "agent-memory-sync.timer": timer.encode(),
    }


def schedule(
    action: str,
    home: Path,
    config: Path,
    at: str = "02:00",
    platform: str | None = None,
    dry_run: bool = False,
) -> int:
    platform = platform or ("launchd" if sys.platform == "darwin" else "systemd")
    files = render(home, config, at, platform)
    if action == "print":
        for path, content in files.items():
            print(f"# {path.name}\n{content.decode()}", end="")
        return 0
    installing = action == "install"
    if platform == "launchd":
        path = next(iter(files))
        domain = f"gui/{os.getuid()}"
        commands = (
            [
                ("plutil", "-lint", str(path)),
                ("launchctl", "bootout", domain, str(path)),
                ("launchctl", "bootstrap", domain, str(path)),
            ]
            if installing
            else [("launchctl", "bootout", domain, str(path))]
        )
    else:
        commands = (
            [
                ("systemctl", "--user", "daemon-reload"),
                ("systemctl", "--user", "enable", "--now", "agent-memory-sync.timer"),
            ]
            if installing
            else [
                ("systemctl", "--user", "disable", "--now", "agent-memory-sync.timer"),
                ("systemctl", "--user", "daemon-reload"),
            ]
        )
    for path in files:
        no_symlinks(path)
    if dry_run:
        for path, content in files.items():
            print(f"{'write' if installing else 'remove'} {path}")
            if installing:
                print(content.decode(), end="")
        for argv in commands:
            print(shlex.join(argv))
        return 0
    if installing:
        for path, content in files.items():
            write_private(path, content)
        if platform == "launchd":
            private_dir(home / "Library/Logs/agent-memory-sync")
    for argv in commands:
        if not installing and argv[-1] == "daemon-reload":
            for path in files:
                path.unlink(missing_ok=True)
        result = subprocess.run(argv, capture_output=True, check=False)  # noqa: S603
        if result.returncode and "bootout" not in argv:
            raise SyncError(f"Scheduler command failed: {argv[0]} {argv[1]}")
    if not installing:
        for path in files:
            path.unlink(missing_ok=True)
    return 0
