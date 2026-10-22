"""Pinned care scheduler commands and their explicit workspace identity."""

from __future__ import annotations

import shlex
from pathlib import Path

from .isolated_exec import module_argv


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
