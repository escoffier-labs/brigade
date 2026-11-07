"""Pinned care scheduler commands and their explicit workspace identity."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from .isolated_exec import module_argv

if TYPE_CHECKING:
    from .care_cmd import CareEntry


def runbook_argv(runbook_rel: str, workspace: Path) -> list[str]:
    workspace = workspace.expanduser().resolve()
    return module_argv(
        "brigade", "runbook", "run", "--approved", str(workspace / runbook_rel), "--target", str(workspace)
    )


def explicit_workspace(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("ExecStart=") or "&&" in line:
            try:
                argv = shlex.split(
                    line.removeprefix("ExecStart=") if line.startswith("ExecStart=") else line.replace("\\%", "%")
                )
                index = argv.index("--target")
                if argv[index + 1] != ".":
                    value = argv[index + 1]
                    if line.startswith("ExecStart="):
                        value = value.replace("%%", "%").replace("$$", "$")
                    else:
                        value = value.replace("\\$", "$").replace("\\`", "`")
                    return value
            except (ValueError, IndexError):
                pass
    return None


def entry_argv(entry: CareEntry, workspace: Path) -> list[str]:
    if entry.entry_id == "memory-reflect":
        return module_argv(
            "brigade",
            "memory",
            "reflect",
            "run",
            "--hub",
            "--scheduled",
            "--target",
            str(workspace.expanduser().resolve()),
        )
    assert entry.runbook_rel is not None
    return runbook_argv(entry.runbook_rel, workspace)
