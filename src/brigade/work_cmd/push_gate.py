"""Configure and inspect the clone-local Git push gate."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from shutil import which
from typing import Any

from .. import config, localio
from . import push_gate_display, push_gate_log

_MODE_KEY = "brigade.pushGate"
ENFORCE_V1_NOTICE = (
    "enforce is configured but this hook block is version 1 and will not block pushes; replace the block"
)
_TIMEOUT_PROBE = ("-k", "1", "1", "true")
_SHEBANG = b"#!/usr/bin/env bash\n"
_MANAGED_HEADER = b"# Brigade managed minimal push gate\n"
_GATE_BLOCK = re.compile(rb"^# BEGIN Brigade push gate v(\d+)\n.*?^# END Brigade push gate v\1\n", re.M | re.S)


def _git(target: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True, timeout=2, check=False)


def _mode(target: Path, *, local: bool = True) -> str | None:
    scope = ("--local", "--includes") if local else ()
    result = _git(target, "config", *scope, "--get", _MODE_KEY)
    if result.returncode and (result.returncode != 1 or result.stderr):
        detail = result.stderr.strip() or f"Git exited with status {result.returncode}"
        raise OSError(detail)
    return result.stdout.removesuffix("\n") if result.returncode == 0 else None


def _write_mode(target: Path, value: str | None) -> None:
    args = ("--unset-all", _MODE_KEY) if value is None else ("--replace-all", _MODE_KEY, value)
    result = _git(target, "config", "--local", *args)
    if result.returncode != 0 and not (value is None and result.returncode == 5):
        raise OSError(f"could not update Git config key {_MODE_KEY}")


@dataclass(frozen=True)
class Repository:
    root: Path
    common: Path
    hook: Path

    @classmethod
    def locate(cls, target: Path) -> Repository | None:
        result = _git(
            target, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir", "--git-path", "hooks"
        )
        if result.returncode != 0:
            error = result.stderr.strip()
            if error and "not a git repository" not in error and "must be run in a work tree" not in error:
                raise OSError(error)
            return None
        root, common, hooks = (Path(line) for line in result.stdout.splitlines())
        return cls(root, common.resolve(), hooks / "pre-push")


def _guard_bytes() -> bytes:
    return files("brigade").joinpath("templates", "hooks", "pre-push").read_bytes().replace(b"\r\n", b"\n")


def _snippet() -> bytes:
    match = _GATE_BLOCK.search(_guard_bytes())
    if match is None:
        raise ValueError("packaged pre-push template has no push gate")
    return match.group()


def _minimal_hook() -> bytes:
    return _SHEBANG + _MANAGED_HEADER + b'STDIN_REFS="$(cat)"\n' + _snippet() + b"exit 0\n"


def _read_hook(path: Path) -> bytes | None:
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return b""
        with path.open("rb") as stream:
            return stream.read(128 * 1024)
    except FileNotFoundError:
        return None


def _normalized_mode(mode: str | None) -> str:
    return (mode or "").strip().lower()


def _timeout_state() -> str:
    command: list[str] = []
    via_git_bash = False
    if sys.platform == "win32" and (git := which("git")):
        # Git for Windows runs hooks in Git Bash, whose tool lookup can
        # select GNU timeout even when native PATH finds System32 first.
        for root in Path(git).resolve().parents:
            bash = root / "bin" / "bash.exe"
            if (root / "cmd" / "git.exe").is_file() and bash.is_file():
                command = [
                    str(bash),
                    "-c",
                    'PATH="/usr/bin:$PATH"; command -v timeout >/dev/null 2>&1 || exit 127; timeout "$@" || exit 1',
                    "push-gate-timeout-probe",
                    *_TIMEOUT_PROBE,
                ]
                via_git_bash = True
                break
    if not command:
        executable = which("timeout")
        if executable is None:
            return "missing"
        command = [executable, *_TIMEOUT_PROBE]
    try:
        result = subprocess.run(command, capture_output=True, timeout=2, check=False)
        if result.returncode == 0:
            return "usable"
        if via_git_bash and result.returncode == 127:
            return "missing"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "incompatible"


def _enforce_issue(info: dict[str, Any], *, will_create: bool = False) -> str | None:
    if not info["wired"] and not will_create:
        return "Wire the effective executable pre-push hook with the current Brigade block."
    if info["hook_version"] == 1:
        return (
            ENFORCE_V1_NOTICE
            if info["mode"] == "enforce"
            else "Replace the version 1 hook block before enabling enforce mode."
        )
    if not info["current"] and not will_create:
        return "Replace the installed Brigade hook block with the current block."
    if not info["brigade_on_path"]:
        return "Put brigade on Git's PATH."
    if not info["timeout_on_path"]:
        return "Put GNU coreutils timeout with -k support on Git's PATH."
    return None


def status(repo: Repository) -> dict[str, Any]:
    try:
        configured = _mode(repo.root)
        effective = _mode(repo.root, local=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError(f"Git could not read the configuration: {exc}") from exc
    mode = _normalized_mode(configured)
    raw = _read_hook(repo.hook)
    normalized = raw.replace(b"\r\n", b"\n") if raw else b""
    match = _GATE_BLOCK.search(normalized)
    hook_verified = normalized in (_minimal_hook(), _guard_bytes())
    current = match is not None and match.group() == _snippet()
    executable = raw is not None and os.access(repo.hook, os.X_OK)
    wired = bool(executable and match and b"brigade work verify push-gate run" in match.group())
    brigade_on_path = which("brigade") is not None
    timeout_state = _timeout_state()
    timeout_on_path = timeout_state == "usable"
    rows = push_gate_log.read(push_gate_log.log_path(repo.common))
    recent = datetime.now(UTC) - timedelta(days=7)
    stamps = [datetime.fromisoformat(row["time"]) for row in rows]
    info = {
        "mode": mode if mode in {"warn", "enforce"} else "off",
        "configured_mode": configured,
        "ignored_mode": effective if effective != configured else None,
        "invalid_mode": bool(mode and mode not in {"off", "warn", "enforce"}),
        "hook_path": str(repo.hook),
        "hook_exists": raw is not None,
        "hook_executable": executable,
        "hook_version": int(match[1]) if match else None,
        "current": current,
        "hook_verified": hook_verified,
        "outside_gate_lines": _outside_gate_lines(normalized, list(_BLOCK_MARKER.finditer(normalized))),
        "wired": wired,
        "brigade_on_path": brigade_on_path,
        "timeout_on_path": timeout_on_path,
        "timeout_state": timeout_state,
        "will_run": mode in {"warn", "enforce"} and wired and brigade_on_path and timeout_on_path,
        "last_push": max(stamps).astimezone(UTC).isoformat() if stamps else None,
        "recent_gate_errors": sum(
            row["result"] == "gate_error" and stamp >= recent for row, stamp in zip(rows, stamps, strict=True)
        ),
    }
    info["enforce_effective"] = mode == "enforce" and hook_verified and _enforce_issue(info) is None
    return info


def describe(info: dict[str, Any], *, include_issue: bool = True) -> str:
    mode = info["mode"]
    if info["invalid_mode"]:
        mode += f" (unrecognised brigade.pushGate value {info['configured_mode']!r})"
    active = "wired" if info["wired"] else "not wired"
    path_status = "brigade on PATH" if info["brigade_on_path"] else "cannot run: brigade missing from PATH"
    if info["timeout_on_path"]:
        path_status += ", timeout on PATH"
    elif info["timeout_state"] == "incompatible":
        path_status += ", timeout on PATH does not support the required -k invocation"
    elif info["mode"] == "warn":
        path_status += ", configured but will not run: timeout missing from PATH"
    else:
        path_status += ", timeout missing from PATH"
    version = f"v{info['hook_version']}" if info["hook_version"] is not None else "none"
    freshness = ", update available" if info["wired"] and not info["current"] else ""
    description = (
        f"mode={mode}, {active}, hook={info['hook_path']}, version={version}{freshness}, {path_status}, "
        f"last push={info['last_push'] or 'none'}, last 7 days gate errors={info['recent_gate_errors']}"
    )
    if info["ignored_mode"] is not None:
        description += (
            f". Ignored Git config value {info['ignored_mode']!r}. "
            "Only clone-local config and its includes select the push gate mode."
        )
    if info["hook_version"] is not None and not info["hook_verified"]:
        description += (
            ". Block present, hook not verified. Enforcement cannot be confirmed because the hook has other content."
        )
    if lines := info["outside_gate_lines"]:
        description += f". {_outside_gate_message(lines)}"
    if include_issue and mode == "enforce" and (issue := _enforce_issue(info)):
        description += f". {issue}"
    return push_gate_display.terminal_text(description)


def _create_minimal_hook(repo: Repository) -> bool:
    repo.hook.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = repo.hook.open("xb")
    except FileExistsError:
        return False
    try:
        with stream:
            stream.write(_minimal_hook())
        repo.hook.chmod(0o755)
    except OSError:
        repo.hook.unlink()
        raise
    return True


def _install(repo: Repository, mode: str) -> None:
    can_create = repo.hook.parent.resolve() == repo.common / "hooks" and not os.path.lexists(repo.hook)
    if mode != "enforce":
        _write_mode(repo.root, mode)
        if can_create:
            _create_minimal_hook(repo)
    info = status(repo)
    issue = _enforce_issue(info, will_create=can_create) if mode == "enforce" else None
    write_mode = mode == "enforce" and info["mode"] != mode
    if write_mode and issue is None:
        source = _git(repo.root, "config", "--local", "--includes", "--null", "--show-origin", "--get", _MODE_KEY)
        if source.returncode not in (0, 1):
            raise OSError(f"could not locate Git config key {_MODE_KEY}")
        origin = source.stdout.partition("\0")[0].removeprefix("file:")
        if origin and (repo.root / origin).resolve() != repo.common / "config":
            issue = f"Set {_MODE_KEY}=enforce in the included Git config {origin!r}."
    if mode == "enforce" and issue is None:
        created = can_create and _create_minimal_hook(repo)
        if can_create and not created:
            raise OSError("The effective pre-push hook appeared during install. Inspect it and retry.")
        try:
            if write_mode:
                _write_mode(repo.root, mode)
        except (OSError, subprocess.TimeoutExpired):
            if created and _read_hook(repo.hook) == _minimal_hook():
                repo.hook.unlink()
            raise
        info = status(repo)
    print(describe(info, include_issue=issue is None))
    if not info["wired"] or not info["current"]:
        print("The effective hook was left in place. Add or update these lines after capturing pre-push input:")
        print(_snippet().decode("ascii"), end="")
        if info["hook_version"] == 1 and issue is None and info["mode"] != "enforce":
            print("The version 1 hook block must be replaced before it can enforce.")
        print("For a Brigade content guard hook, refresh from the shipped templates/hooks/pre-push file.")
    if issue is not None:
        raise ValueError(issue)


_BLOCK_MARKER = re.compile(rb"^[ \t]*#[ \t]*(?:BEGIN|END)[ \t]+Brigade push gate[^\n]*", re.M)
_BLOCK_BOUNDARY = re.compile(rb"# (BEGIN|END) Brigade push gate v([0-9]+)\r?")
_BASH_SHEBANG = re.compile(rb"#![ \t]*(?:(?:\S*/)?env[ \t]+(?:-S[ \t]+)?)?(?:\S*/)?bash(?:\.exe)?(?:[ \t][^\r]*)?\r?")
_GATE_INVOCATION = re.compile(
    rb"""(?<![\w./'"-])"""
    + rb"[ \t]+".join(
        rb"(?:" + word + rb"|'" + word + rb"'|\"" + word + rb"\")"
        for word in (b"brigade", b"work", b"verify", b"push-gate", b"run")
    )
    + rb"""(?![\w./'"-])"""
)
_GATE_TOKEN = re.compile(rb"(?<![\w-])push-gate(?![\w-])")
_CAPTURE_REFS = re.compile(rb'^STDIN_REFS="\$\(cat\)"(\r?\n)', re.M)


def _outside_gate_lines(raw: bytes, markers: list[re.Match[bytes]]) -> list[int]:
    if len(markers) not in {0, 2}:
        return []
    start, end = (markers[0].start(), markers[1].end()) if markers else (0, 0)
    physical = raw.split(b"\n")
    lines = []
    offset = 0
    index = 0
    while index < len(physical):
        number = index + 1
        line_start = offset
        line = physical[index]
        offset += len(line) + 1
        index += 1
        if start <= line_start < end or line.lstrip().startswith(b"#"):
            # A comment line does not continue in the shell, so never join after it.
            continue
        logical = line
        while logical.endswith((b"\\", b"\\\r")) and index < len(physical):
            logical = logical.removesuffix(b"\r").removesuffix(b"\\")
            line = physical[index]
            offset += len(line) + 1
            index += 1
            logical += line
        if _GATE_TOKEN.search(logical):
            lines.append(number)
    return lines


def _outside_gate_message(lines: list[int]) -> str:
    if len(lines) == 1:
        return f"Brigade gate invocation on line {lines[0]} is outside the marked block."
    return f"Brigade gate invocations on lines {', '.join(map(str, lines))} are outside the marked block."


def _block_replacement(raw: bytes, *, insert: bool, preview: bool) -> tuple[bytes, str]:
    if not _BASH_SHEBANG.fullmatch(raw.partition(b"\n")[0]):
        raise ValueError("The effective pre-push hook must start with a Bash interpreter line.")
    markers = list(_BLOCK_MARKER.finditer(raw))
    if lines := _outside_gate_lines(raw, markers):
        pronoun = "it" if len(lines) == 1 else "them"
        raise ValueError(
            f"{_outside_gate_message(lines)} Move {pronoun} inside the markers or remove {pronoun}. "
            "Then run the command again."
        )
    calls = [
        match
        for match in _GATE_INVOCATION.finditer(raw)
        if not raw[raw.rfind(b"\n", 0, match.start()) + 1 : match.start()].lstrip().startswith(b"#")
    ]
    if not markers:
        if not (insert or preview):
            raise ValueError("No Brigade block found. Pass --confirm-insert to insert it.")
        captures = list(_CAPTURE_REFS.finditer(raw))
        if len(captures) != 1:
            raise ValueError('Insertion needs one STDIN_REFS="$(cat)" line to locate the gate.')
        capture = captures[0]
        start = end = capture.end()
        newline = capture[1]
        action = "Insert the Brigade push gate block"
    else:
        if len(markers) > 2:
            raise ValueError("The hook contains multiple Brigade push gate block markers.")
        boundaries = []
        for marker in markers:
            boundary = _BLOCK_BOUNDARY.fullmatch(marker.group())
            if boundary is None:
                line = raw.count(b"\n", 0, marker.start()) + 1
                raise ValueError(
                    f"Invalid Brigade block marker on line {line}. Use the exact marker with no indentation or trailing spaces."
                )
            boundaries.append(boundary)
        if len(boundaries) != 2:
            raise ValueError("The hook contains damaged Brigade push gate block markers.")
        begin, finish = boundaries
        if begin[1] != b"BEGIN" or finish[1] != b"END" or begin[2] != finish[2]:
            raise ValueError("The hook contains damaged Brigade push gate block markers.")
        if len(calls) != 1:
            raise ValueError("The marked Brigade block must contain exactly one gate invocation.")
        start, end = markers[0].start(), markers[1].end()
        end += raw[end : end + 1] == b"\n"
        newline = b"\r\n" if markers[0].group().endswith(b"\r") else b"\n"
        action = f"Replace the Brigade push gate block v{begin[2].decode('ascii')}"
    return raw[:start] + _snippet().replace(b"\n", newline) + raw[end:], action


def _install_block(repo: Repository, *, insert: bool, preview: bool) -> None:
    hooks = repo.hook.parent.resolve()
    try:
        path = repo.hook.resolve(strict=True)
        original = path.stat()
    except FileNotFoundError:
        raise ValueError("no effective pre-push hook to update") from None
    if not path.is_relative_to(hooks):
        raise ValueError("The effective pre-push hook points outside the hooks directory.")
    mode = stat.S_IMODE(original.st_mode)
    if not stat.S_ISREG(original.st_mode):
        raise ValueError("The effective pre-push hook must be a regular file.")
    if not preview and (not mode & 0o222 or not os.access(path, os.W_OK)):
        raise PermissionError("The effective pre-push hook is not writable.")
    raw = path.read_bytes()
    updated, action = _block_replacement(raw, insert=insert, preview=preview)
    if raw == updated:
        print("The Brigade push gate block is already current.")
        return
    if preview:
        diff = difflib.diff_bytes(
            difflib.unified_diff,
            raw.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            b"pre-push (before)",
            b"pre-push (after)",
        )
        for line in diff:
            print(push_gate_display.terminal_text(line.rstrip(b"\r\n").decode("utf-8", errors="backslashreplace")))
        return

    print(push_gate_display.terminal_text(f"{action} in {repo.hook}."))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = repo.hook.with_name(f"{repo.hook.name}.{stamp}.bak")
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        localio.write_bytes_exclusive(backup, raw)
        try:
            backup.chmod(mode)
            current = path.stat()
            if (
                repo.hook.resolve(strict=True) != path
                or not os.path.samestat(original, current)
                or current.st_mtime_ns != original.st_mtime_ns
                or current.st_ctime_ns != original.st_ctime_ns
                or current.st_mode != original.st_mode
            ):
                raise OSError("The pre-push hook changed during the update. Inspect it and retry.")
            os.replace(temporary, path)
        except OSError:
            backup.unlink()
            raise
    finally:
        temporary.unlink(missing_ok=True)

    backup_arg = push_gate_display.shell_arg(backup.as_posix())
    path_arg = push_gate_display.shell_arg(path.as_posix())
    print(f"Restore the previous hook with: cp -p -- {backup_arg} {path_arg}")


def _uninstall(repo: Repository) -> None:
    _write_mode(repo.root, None)
    if (
        repo.hook.parent.resolve() == repo.common / "hooks"
        and not repo.hook.is_symlink()
        and _read_hook(repo.hook) == _minimal_hook()
    ):
        repo.hook.unlink()


_PRUNE_GRACE_SECONDS = 60


def _prune(repo: Repository) -> int:
    from .verification import _archive_verify_run, _verify_archive_root
    from .verify_push import _RECEIPT_LIMIT, _Indeterminate, _Receipts, _receipt_started_at

    keep = config.resolve_verify_runs_keep(repo.root)
    if keep > _RECEIPT_LIMIT:
        raise ValueError(f"Set verify_runs_keep in {config.CONFIG_REL_PATH} to at most {_RECEIPT_LIMIT}.")
    receipts = _Receipts(repo.root, float("inf"))
    try:
        named, stray = receipts.inventory()
        for name in sorted(stray):
            problem = receipts._entry_problem(name, "has a non-writer directory name")
            push_gate_display.diagnostic(problem, remedy=problem.remedy)
        now = datetime.now(UTC)
        runs: list[tuple[datetime, str, bool]] = []
        for name, second in named:
            receipt = receipts._load(name)
            if receipt is None:
                continue
            if isinstance(receipt, _Indeterminate):
                raise receipt
            started = _receipt_started_at(receipt)
            if started is None or started.replace(microsecond=0) != second or started > now:
                raise receipts._entry_problem(name, "has an invalid, mismatched, or future start timestamp")
            runs.append((started, name, receipt["status"] == "running"))
    except _Indeterminate as exc:
        push_gate_display.diagnostic(f"push gate: {exc}", remedy=exc.remedy)
        return 1

    # The writer does not hold a retention lock. Preserve active runs and
    # recent writes even when they fall outside the configured retention.
    cutoff = now.timestamp() - _PRUNE_GRACE_SECONDS
    stale = []
    for started, name, running in sorted(runs, reverse=True)[keep:]:
        if running or started.timestamp() > cutoff:
            continue
        path = receipts.root / name
        if max(path.lstat().st_mtime, (path / "receipt.json").lstat().st_mtime) <= cutoff:
            stale.append(path)

    removed = 0
    if stale:
        _, configured_archive = config.resolve_verify_archive(repo.root)
        try:
            archive = _verify_archive_root(repo.root)
            if archive is None:
                raise OSError("verify archival is disabled")
            if archive.exists() and not archive.stat().st_mode & 0o222:
                raise PermissionError("archive directory has no write permissions")
            for path in reversed(stale):
                _archive_verify_run(path, archive)
                if max(path.lstat().st_mtime, (path / "receipt.json").lstat().st_mtime) <= cutoff:
                    shutil.rmtree(path)
                    removed += 1
        except (OSError, ValueError, RuntimeError) as exc:
            raise OSError(
                f"Could not write or complete pruning via verify archive {str(configured_archive)!r}: {exc}"
            ) from exc

    remaining = len(runs) - removed
    if remaining > _RECEIPT_LIMIT:
        raise ValueError(
            f"{remaining} receipts remain in {str(receipts.root)!r}. "
            f"Wait for active runs and the {_PRUNE_GRACE_SECONDS}-second grace period, then retry."
        )
    print(f"Pruned {removed} local verify run(s). Local receipts remaining: {remaining}.")
    return 0


def _report_text(report: dict[str, Any]) -> None:
    def line(label: str, info: dict[str, Any]) -> None:
        counts = ", ".join(f"{key}={value}" for key, value in sorted(info["results"].items())) or "no results"
        median = f"{info['median_ms']} ms" if info["median_ms"] is not None else "n/a"
        p95 = f"{info['p95_ms']} ms" if info["p95_ms"] is not None else "n/a"
        print(
            push_gate_display.terminal_text(
                f"{label}  {counts} | would block {info['would_block_share']:.1%} | blocked rows {info['blocked']}"
                f" | gate errors {info['gate_errors']}"
                f" | median {median} | p95 {p95}"
            )
        )

    line("Total", report)
    for day, info in report["days"].items():
        line(day, info)


def register(parsers: Any) -> None:
    gate = parsers.add_parser("push-gate", help="Manage the pre-push verification gate.")
    commands = gate.add_subparsers(dest="push_gate_command", required=True)
    run = commands.add_parser("run", help="Run the configured check from a Git pre-push hook.")
    run.add_argument("--hook-v2", action="store_true", help="Use the v2 hook protocol.")
    for name in ("install", "status", "uninstall", "report", "prune"):
        parser = commands.add_parser(name)
        parser.add_argument("--target", "-t", type=Path, default=Path("."))
        if name == "install":
            parser.add_argument("--mode", metavar="{warn,enforce,off}")
            parser.add_argument("--block-only", action="store_true", help="Update only the Brigade block in the hook.")
            parser.add_argument("--dry-run", action="store_true", help="Print the proposed diff.")
            parser.add_argument(
                "--confirm-insert", action="store_true", help="Allow insertion when the block is absent."
            )
        if name in {"status", "report"}:
            parser.add_argument("--json", action="store_true")
        if name == "report":
            parser.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD")


def dispatch(args: argparse.Namespace) -> int:
    command = args.push_gate_command
    if command == "run":
        from .push_gate_hook import run

        return run(hook_v2=args.hook_v2)
    if command == "install":
        if args.mode not in {None, "warn", "enforce", "off"}:
            print("push gate: expected mode warn, enforce, or off", file=sys.stderr)
            return 2
        if args.block_only and args.mode is not None:
            print("push gate: --block-only cannot be used with --mode", file=sys.stderr)
            return 2
        if not args.block_only and (args.dry_run or args.confirm_insert):
            print("push gate: --dry-run and --confirm-insert require --block-only", file=sys.stderr)
            return 2
    try:
        repo = Repository.locate(args.target)
        if repo is None:
            raise OSError("a Git worktree is required")
        if command == "report":
            since = args.since.isoformat() if args.since else None
            report = push_gate_log.report(push_gate_log.read(push_gate_log.log_path(repo.common)), since)
            if args.json:
                print(json.dumps(report))
            else:
                _report_text(report)
        elif command == "status":
            info = status(repo)
            print(json.dumps(info) if args.json else describe(info))
        elif command == "install":
            if args.block_only:
                _install_block(repo, insert=args.confirm_insert, preview=args.dry_run)
            else:
                _install(repo, args.mode or "warn")
        elif command == "prune":
            return _prune(repo)
        else:
            _uninstall(repo)
            print(push_gate_display.terminal_text(f"push gate: mode unset for {repo.root}"))
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        push_gate_display.diagnostic(f"push gate: {exc}")
        return 1
    return 0
