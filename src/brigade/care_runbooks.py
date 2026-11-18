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
* Publication is per file. An absent file is hard-linked into place, which
  cannot clobber a file that appeared meanwhile. Only when the filesystem
  refuses hard links does the writer re-check that the name is still absent and
  fall back to ``os.replace``, which leaves a small window in which a file
  created at that moment would be replaced. An OS failure in the middle of the
  publish loop returns 2, names the failing file and lists the files already
  published (those stay published: the loop has no rollback).
* ``dry_run=True`` checks every destination exactly like a real run, then prints
  what would be written and touches nothing.

A shipped file that already exists is kept unless it is *pristine*: its content
(minus the top-level ``pins`` block, and only that block) equals a text a past
release shipped for that runbook (``shipped_history``). Such a copy has no local
edits and is replaced with the current text and fresh pins, no backup, printed as
``refreshed (pristine): <path>``. It is re-read without following a link right
before the publish and kept if it changed meanwhile. Any other differing copy is
never overwritten and is reported once as ``kept (drifted): <path>``. These two
lines go to stderr when *quiet* is set, so a ``--json`` caller's stdout stays clean.

``refresh=True`` is for the explicit ``memory care init --force
--with-runbooks`` only. A shipped file whose content differs from the template
is copied to ``<name>.bak-<UTC timestamp>`` and then replaced. A file that
differs only in its ``pins`` block (for example after a ``brigade`` upgrade
changed the pinned binary hash) is rewritten with fresh pins and no backup.
Both cases are printed.
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

from . import localio, runbook_cmd, shipped_history


@dataclass(frozen=True)
class RunbookSpec:
    """One runbook to publish: a target-relative path and its unpinned payload."""

    rel: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class _Item:
    """One destination to publish. ``expected`` is the bytes of a pristine past-release copy to replace."""

    spec: RunbookSpec
    dest: Path
    exists: bool
    expected: bytes | None = None


def _say(line: str, *, quiet: bool) -> None:
    print(line, file=sys.stderr if quiet else sys.stdout)


def _classify(spec: RunbookSpec, dest: Path) -> tuple[str, bytes | None]:
    """Compare an existing shipped file with the spec: ``same``, ``pristine`` (a past release) or ``drifted``."""
    try:
        raw = shipped_history.read_regular_file(dest, shipped_history.MAX_INSTALLED_BYTES)
    except OSError:
        return "drifted", None
    digest = shipped_history.runbook_bytes_digest(raw)
    if digest is None:
        return "drifted", raw
    wanted = shipped_history.runbook_payload_digest(spec.payload)
    if digest == wanted:
        return "same", raw
    known = shipped_history.digests(shipped_history.runbook_key(spec.rel))
    # Both the installed text and the one about to be written must be recorded shipped texts.
    return ("pristine" if digest in known and wanted in known else "drifted"), raw


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


def _canonical(payload: Any) -> str | None:
    """Compact sorted JSON of a runbook without its top-level pins, or None when it is not an object."""
    if not isinstance(payload, dict):
        return None
    return json.dumps({k: v for k, v in payload.items() if k != "pins"}, sort_keys=True, separators=(",", ":"))


def _same_without_pins(existing: bytes, wanted: bytes | dict[str, Any]) -> bool:
    try:
        left = _canonical(json.loads(existing))
        right = _canonical(wanted if isinstance(wanted, dict) else json.loads(wanted))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return left is not None and left == right


def _backup(dest: Path, stamp: str) -> Path:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for attempt in range(1000):
        backup = dest.with_name(f"{dest.name}.bak-{stamp}" + (f"-{attempt}" if attempt else ""))
        try:
            fd = os.open(dest, os.O_RDONLY | nofollow)
            with os.fdopen(fd, "rb") as source:
                # Windows has no O_NOFOLLOW: require the opened file to be the
                # regular file the name points at now, not a link's target.
                opened, named = os.fstat(source.fileno()), os.lstat(dest)
                if stat.S_ISLNK(named.st_mode) or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                    raise OSError(f"runbook is a link, not a regular file: {dest}")
                with open(backup, "xb") as copy:
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
    current, wanted = dest.read_bytes(), staged.read_bytes()
    if current == wanted:
        return None
    if _same_without_pins(current, wanted):
        os.replace(staged, dest)  # only the pins moved: refresh them, keep no backup
        return f"re-pinned runbook: {dest}"
    backup = _backup(dest, stamp)
    os.replace(staged, dest)
    return f"replaced shipped runbook: {dest} (backup: {backup})"


def _publish_pristine(staged: Path, dest: Path, expected: bytes) -> str | None:
    """Replace a pristine past-release copy, only if it still holds exactly ``expected``; None means kept."""
    try:
        current = shipped_history.read_regular_file(dest, shipped_history.MAX_INSTALLED_BYTES)
    except OSError:
        return None
    if current != expected:
        return None
    os.replace(staged, dest)
    return f"refreshed (pristine): {dest}"


def _report_plan(work: list[_Item], *, quiet: bool) -> int:
    """Print what a real run would do for each destination; writes nothing."""
    if quiet:
        return 0
    for item in work:
        spec, dest, exists = item.spec, item.dest, item.exists
        if not exists:
            print(f"would write runbook: {dest}")
            continue
        if item.expected is not None:
            print(f"would refresh (pristine): {dest}")
            continue
        try:
            current = dest.read_bytes()
        except OSError as exc:
            print(f"error: cannot read runbook {dest}: {exc}", file=sys.stderr)
            return 2
        if not _same_without_pins(current, spec.payload):
            print(f"would replace shipped runbook: {dest} (backup first)")
    return 0


def ensure(
    target: Path,
    specs: Iterable[RunbookSpec],
    *,
    refresh: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
) -> int:
    """Publish the specs under target as one batch. Returns 0, or 2 after an error naming the path.

    Every destination is checked before the first file is published. With
    *dry_run* nothing is written; the plan is printed unless *quiet* is set.
    """
    try:
        return _ensure(target, list(specs), refresh=refresh, dry_run=dry_run, quiet=quiet)
    except OSError as exc:
        print(f"error: cannot write care runbooks: {exc}", file=sys.stderr)
        return 2


def _ensure(target: Path, specs: list[RunbookSpec], *, refresh: bool, dry_run: bool, quiet: bool) -> int:
    work: list[_Item] = []
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
            work.append(_Item(spec, dest, regular is True))
            continue
        state, raw = _classify(spec, dest)
        if state == "pristine":
            work.append(_Item(spec, dest, True, raw))
        elif state == "drifted":
            _say(f"kept (drifted): {dest}", quiet=quiet)
    if dry_run:
        return _report_plan(work, quiet=quiet)
    if not work:
        return 0

    stage_dirs: dict[Path, Path] = {}
    try:
        staged: list[tuple[Path, _Item]] = []
        for item in work:
            spec, dest = item.spec, item.dest
            if dest.parent not in stage_dirs:
                dest.parent.parent.mkdir(parents=True, exist_ok=True)
                if (problem := _dir_problem(target, dest.parent)) is not None:
                    print(f"error: {problem}", file=sys.stderr)
                    return 2
                stage_dirs[dest.parent] = Path(tempfile.mkdtemp(dir=dest.parent.parent, prefix=".runbooks-"))
            result = _stage(target, spec, stage_dirs[dest.parent])
            if isinstance(result, int):
                return result
            staged.append((result, item))

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        done: list[str] = []
        for staged_path, item in staged:
            dest = item.dest
            try:
                dest.parent.mkdir(exist_ok=True)
                if (problem := _dir_problem(target, dest.parent)) is not None:
                    raise OSError(problem)
                if item.expected is not None:
                    line = _publish_pristine(staged_path, dest, item.expected)
                    _say(line or f"kept (drifted): {dest}", quiet=quiet)
                    if line is not None:
                        done.append(dest.name)
                    continue
                line = _publish(staged_path, dest, replace=item.exists, stamp=stamp)
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
