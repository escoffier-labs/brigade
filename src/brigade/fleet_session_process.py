"""Fail-closed Linux process identity for local interactive session renewal."""

from __future__ import annotations

import os
import re
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    parent: int
    start: str
    boot: str
    executable: str
    arguments: tuple[str, ...]

    def proof(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if key in {"pid", "start", "boot"}}


class ProcessTable:
    """Read bounded procfs metadata, never process environments or output."""

    def __init__(self, root: Path = Path("/proc")) -> None:
        self.root = root

    def read(self, pid: int) -> ProcessIdentity | None:
        if sys.platform != "linux" or type(pid) is not int or pid <= 1:
            return None
        try:
            directory = self.root / str(pid)
            if directory.stat().st_uid != os.getuid():
                return None
            with (directory / "stat").open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                return None
            # comm can contain spaces and closing parentheses. Fields after
            # the final ')' start at field 3, with starttime at field 22.
            fields = raw[raw.rindex(b")") + 2 :].split()
            if fields[0] in {b"Z", b"X", b"x"}:
                return None
            parent, start = int(fields[1]), fields[19].decode("ascii")
            with (self.root / "sys/kernel/random/boot_id").open("rb") as stream:
                boot = stream.read(129).decode("ascii").strip()
            with (directory / "cmdline").open("rb") as stream:
                raw_args = stream.read(8193)
            if not boot or len(boot) > 128 or len(raw_args) > 8192 or not start.isdecimal():
                return None
            args = tuple(arg.decode("utf-8", "strict") for arg in raw_args.rstrip(b"\0").split(b"\0"))
            executable_path = Path(os.readlink(directory / "exe"))
            executable = executable_path.name
            # Native Claude's launcher is a symlink. procfs exposes the
            # versioned binary, so verify its installation path and inode
            # metadata rather than trusting argv[0] or a version basename.
            if (
                re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", executable)
                and executable_path.parent == (Path.home() / ".local/share/claude/versions").resolve()
            ):
                info = (directory / "exe").stat()
                if (
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == os.getuid()
                    and info.st_mode & stat.S_IXUSR
                    and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    executable = "claude"
            return ProcessIdentity(pid, parent, start, boot, executable, args)
        except (OSError, ValueError, IndexError, UnicodeError):
            return None

    def owner(self, harness: str) -> dict[str, object] | None:
        pid = os.getpid()
        seen: set[int] = set()
        for _ in range(32):
            if pid in seen:
                return None
            seen.add(pid)
            process = self.read(pid)
            if process is None:
                return None
            if _interactive(process, harness):
                # Do not bind a reused PID encountered during ancestry discovery.
                current = self.read(pid)
                return process.proof() if current is not None and current.proof() == process.proof() else None
            if harness == "codex" and process.executable == "codex":
                return None
            pid = process.parent
        return None

    def alive(self, proof: object, harness: str) -> bool:
        if not isinstance(proof, dict) or type(proof.get("pid")) is not int:
            return False
        process = self.read(proof["pid"])
        return process is not None and process.proof() == proof and _interactive(process, harness)


def _interactive(process: ProcessIdentity, harness: str) -> bool:
    args = process.arguments
    if harness == "codex":
        # app-server and exec can multiplex or perform bounded noninteractive
        # work. Their process lifetime does not prove a thread remains open.
        return process.executable == "codex" and not any(
            arg in {"app-server", "exec", "e", "mcp-server", "review"} for arg in args[1:]
        )
    if harness == "claude":
        return process.executable == "claude" or (
            process.executable in {"node", "nodejs"}
            and len(args) > 1
            and args[1].replace("\\", "/").endswith("/@anthropic-ai/claude-code/cli.js")
        )
    return False
