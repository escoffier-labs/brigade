"""Pinned care scheduler commands and their explicit workspace identity."""

from __future__ import annotations

import shlex
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


MEMORY_VERIFY_ID = "memory-verify"


def verify_entry(entry_type: type[CareEntry]) -> CareEntry:
    """The memory verification pass, in its own verify folder (phase-memory-verify D3)."""
    from .care_schedules import MEMORY_VERIFY_CALENDAR, MEMORY_VERIFY_SCHEDULE

    return entry_type(
        entry_id=MEMORY_VERIFY_ID,
        schedule=MEMORY_VERIFY_SCHEDULE,
        on_calendar=MEMORY_VERIFY_CALENDAR,
        description="Brigade hub memory verification proposals",
        kind="verify",
    )


def install_refusal(target: Path, entries: tuple[CareEntry, ...]) -> str | None:
    """Refuse memory-verify in the reflect job's folder, beside memory-reflect, or without its own credential."""
    if not any(entry.kind == "verify" for entry in entries):
        return None
    if any(entry.kind == "reflect" for entry in entries):
        return "memory-verify and memory-reflect need separate folders; install them from each folder"
    from .memory_verify_run import install_refusal as refusal

    reason = refusal(target)
    return f"care entry {MEMORY_VERIFY_ID} is not installable: {reason}" if reason else None


def hub_job_status(target: Path, entry: CareEntry, payload: dict[str, Any]) -> dict[str, Any]:
    """Pause state for the hub memory jobs, from their own state files."""
    try:
        if entry.kind == "verify":
            from .memory_verify_state import load

            state = load(target)
        else:
            from .memory_reflect_hub import schedule_status

            state = schedule_status(target)
        payload.update({k: state[k] for k in ("consecutive_failures", "paused")})
        payload["last_receipt"] = {"status": state.get("last_status")} if "last_status" in state else None
        payload["repeated_failure"] = state["paused"]
    except (OSError, ValueError):
        payload.update(paused=True, state_error=f"{entry.entry_id} state is unreadable")
    return payload


def entry_argv(entry: CareEntry, workspace: Path) -> list[str]:
    if entry.kind == "verify":
        return module_argv(
            "brigade", "memory", "verify", "run", "--scheduled", "--target", str(workspace.expanduser().resolve())
        )
    if entry.entry_id == "memory-reflect":
        argv = module_argv(
            "brigade",
            "memory",
            "reflect",
            "run",
            "--hub",
            "--scheduled",
            "--target",
            str(workspace.expanduser().resolve()),
        )
        if entry.reflect_scope is not None:
            argv.extend(["--scope", entry.reflect_scope])
        return argv
    assert entry.runbook_rel is not None
    return runbook_argv(entry.runbook_rel, workspace)


def scoped_entries(target: Path, entries: tuple[CareEntry, ...], scope: str | None) -> tuple[CareEntry, ...]:
    """Scopes select data, never a destination or credential."""
    if scope is None:
        return entries
    from . import memory_hub_client as hubc

    try:
        table = hubc.read_hub_table(target)
        scopes = hubc._read_scopes(table, table.get("scope"))
        if not any(entry.kind == "reflect" for entry in entries) or not hubc.valid_scope(scope) or scope not in scopes:
            raise ValueError("invalid care scope")
    except (OSError, ValueError):
        raise ValueError("--scope requires memory-reflect and a configured hub read scope") from None
    return tuple(replace(entry, reflect_scope=scope) if entry.kind == "reflect" else entry for entry in entries)


def persisted_scope(entry: CareEntry, path: Path, backend: str) -> CareEntry:
    """Retain a managed reflect scope on reinstall and status rendering."""
    if entry.kind != "reflect" or entry.reflect_scope is not None:
        return entry
    from . import managed_block, memory_hub_client as hubc

    try:
        raw = managed_block.read_text_nofollow(path)
        if backend == "systemd":
            parsed = managed_block.parse_blocks(raw, kind="CARE", style=managed_block.MARKER_STYLE_HASH)
            if parsed.status != "ok" or parsed.meta is None or parsed.actual_hash != parsed.meta.recorded_hash:
                return entry
            argv = shlex.split(next(line[10:] for line in parsed.body.splitlines() if line.startswith("ExecStart=")))
            argv = [arg.replace("%%", "%").replace("$$", "$") for arg in argv]
        else:
            import plistlib

            argv = plistlib.loads(raw.encode())["ProgramArguments"]
        if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
            return entry
        if argv.count("--scope") == 1:
            scope = argv[argv.index("--scope") + 1]
            if hubc.valid_scope(scope):
                return replace(entry, reflect_scope=scope)
    except (OSError, ValueError, IndexError, KeyError, StopIteration):
        pass
    return entry
