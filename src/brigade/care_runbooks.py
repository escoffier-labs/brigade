"""Publish shipped and generated care runbooks without ever destroying operator files.

Every writer behind ``brigade care install`` and ``brigade memory care init
--with-runbooks`` goes through :func:`ensure`, so they share one rule set:

* A destination that is a regular file counts as present and is kept. A
  symlink (live or dangling), directory, FIFO or any other entry at a runbook
  name is refused with exit code 2 and the path named; it is never replaced.
* Nothing is written through a symlinked ``.brigade``, ``memory-care`` or
  ``runbooks`` directory, or one that resolves outside the target.
* Every payload is validated and pinned in a private staging directory, and
  every destination is checked, before the first file is published, so a
  validation, pin or destination problem leaves nothing half-written.
* Publication is per file. Absent files are linked into place and cannot
  clobber a file that appeared meanwhile. An OS failure in the middle of the
  publish loop returns 2, names the failing file and lists the files already
  published (those stay published: the loop has no rollback).

``refresh=True`` is for the explicit ``memory care init --force
--with-runbooks``: it replaces shipped files that differ from the template, but
copies each replaced file to ``<name>.bak-<UTC timestamp>`` first and prints it.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import localio, runbook_cmd


@dataclass(frozen=True)
class RunbookSpec:
    """One runbook to publish: a target-relative path and its unpinned payload."""

    rel: str
    payload: dict[str, Any]


def shipped_specs(template_dir: Path, rel_dir: str, names: Iterable[str]) -> tuple[list[RunbookSpec] | None, int]:
    """Load shipped runbook templates as specs; returns (None, rc) when one is unusable."""
    specs: list[RunbookSpec] = []
    for name in names:
        src = template_dir / name
        if not src.is_file():
            print(f"error: memory-care runbook template missing: {src}", file=sys.stderr)
            return None, 4
        try:
            payload = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"error: invalid memory-care runbook template {src}: {exc}", file=sys.stderr)
            return None, 2
        if not isinstance(payload, dict):
            print(f"error: memory-care runbook template must be a JSON object: {src}", file=sys.stderr)
            return None, 2
        specs.append(RunbookSpec(f"{rel_dir}/{name}", payload))
    return specs, 0


def _dir_problem(target: Path, dest_dir: Path) -> str | None:
    """Name the first symlink, non-directory or outside-target part of dest_dir, if any."""
    current = target
    for part in dest_dir.relative_to(target).parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            return f"refusing to write runbooks through a symlink: {current}"
        if not stat.S_ISDIR(mode):
            return f"refusing to write runbooks: not a directory: {current}"
    try:
        dest_dir.resolve().relative_to(target.resolve())
    except ValueError:
        return f"refusing to write runbooks outside the target: {dest_dir}"
    return None


def _is_regular_file(path: Path) -> bool | None:
    """True for a regular file, None when absent, False for anything else (symlinks included)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except FileNotFoundError:
        return None


def _stage(target: Path, spec: RunbookSpec, stage_dir: Path) -> Path | int:
    staged = stage_dir / Path(spec.rel).name
    localio.write_json(staged, dict(spec.payload))
    validated, error = runbook_cmd._read_runbook(staged, require_pin_hashes=False)
    if validated is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    pins, pin_error = runbook_cmd._pin_payload_from_runbook(target, validated)
    if pins is None:
        print(f"error: {pin_error}", file=sys.stderr)
        return 2
    validated["pins"] = pins
    localio.write_json(staged, validated)
    return staged


def _backup(dest: Path, stamp: str) -> Path:
    for attempt in range(1000):
        backup = dest.with_name(f"{dest.name}.bak-{stamp}" + (f"-{attempt}" if attempt else ""))
        try:
            with open(dest, "rb") as source, open(backup, "xb") as copy:
                shutil.copyfileobj(source, copy)
        except FileExistsError:
            continue
        return backup
    raise FileExistsError(f"no free backup name beside {dest}")


def _publish(staged: Path, dest: Path, *, replace: bool, stamp: str) -> str | None:
    """Publish one file; return a status line to print, or None when nothing was published."""
    if not replace:
        try:
            os.link(staged, dest)  # never clobbers a file that appeared since the check
        except FileExistsError:
            return None
        except OSError:
            if _is_regular_file(dest) is not None:
                return None
            os.replace(staged, dest)
        return f"wrote runbook: {dest}"
    if _is_regular_file(dest) is not True:
        raise OSError(f"runbook is no longer a regular file: {dest}")
    if dest.read_bytes() == staged.read_bytes():
        return None
    backup = _backup(dest, stamp)
    os.replace(staged, dest)
    return f"replaced shipped runbook: {dest} (backup: {backup})"


def ensure(target: Path, specs: Iterable[RunbookSpec], *, refresh: bool = False) -> int:
    """Publish the specs under target. Returns 0, or 2 after an error naming the path."""
    try:
        return _ensure(target, list(specs), refresh=refresh)
    except OSError as exc:
        print(f"error: cannot write care runbooks: {exc}", file=sys.stderr)
        return 2


def _ensure(target: Path, specs: list[RunbookSpec], *, refresh: bool) -> int:
    work: list[tuple[RunbookSpec, Path, bool]] = []  # (spec, dest, destination exists)
    for spec in specs:
        dest = target / spec.rel
        problem = _dir_problem(target, dest.parent)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return 2
        regular = _is_regular_file(dest)
        if regular is False:
            print(
                f"error: refusing to replace a runbook that is not a regular file (symlink, directory or other): {dest}",
                file=sys.stderr,
            )
            return 2
        if regular is None or refresh:
            work.append((spec, dest, regular is True))
    if not work:
        return 0

    stage_dirs: dict[Path, Path] = {}
    try:
        staged: list[tuple[Path, Path, bool]] = []
        for spec, dest, exists in work:
            if dest.parent not in stage_dirs:
                dest.parent.parent.mkdir(parents=True, exist_ok=True)
                if (problem := _dir_problem(target, dest.parent)) is not None:
                    print(f"error: {problem}", file=sys.stderr)
                    return 2
                stage_dirs[dest.parent] = Path(tempfile.mkdtemp(dir=dest.parent.parent, prefix=".runbooks-"))
            result = _stage(target, spec, stage_dirs[dest.parent])
            if isinstance(result, int):
                return result
            staged.append((result, dest, exists))

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        done: list[str] = []
        for staged_path, dest, exists in staged:
            try:
                dest.parent.mkdir(exist_ok=True)
                if (problem := _dir_problem(target, dest.parent)) is not None:
                    raise OSError(problem)
                line = _publish(staged_path, dest, replace=exists, stamp=stamp)
            except OSError as exc:
                print(f"error: cannot publish runbook {dest}: {exc}", file=sys.stderr)
                if done:
                    print("already published before the failure: " + ", ".join(done), file=sys.stderr)
                return 2
            if line is not None:
                done.append(dest.name)
                if refresh:
                    print(line)
        return 0
    finally:
        for stage_dir in stage_dirs.values():
            shutil.rmtree(stage_dir, ignore_errors=True)
