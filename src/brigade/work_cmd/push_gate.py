"""Configure and inspect the clone-local advisory Git push gate."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from shutil import which
from typing import Any

from . import push_gate_log

_MODE_KEY = "brigade.pushGate"
_SHEBANG = b"#!/usr/bin/env bash\n"
_MANAGED_HEADER = b"# Brigade managed minimal push gate\n"
_GATE_BLOCK = re.compile(rb"^# BEGIN Brigade push gate v(\d+)\n.*?^# END Brigade push gate v\1\n", re.M | re.S)


def _git(target: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True, timeout=2, check=False)


def _mode(target: Path) -> str | None:
    result = _git(target, "config", "--get", _MODE_KEY)
    if result.returncode not in (0, 1):
        raise OSError(f"could not read Git config key {_MODE_KEY}")
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


def status(repo: Repository) -> dict[str, Any]:
    mode = _mode(repo.root)
    raw = _read_hook(repo.hook)
    match = _GATE_BLOCK.search(raw.replace(b"\r\n", b"\n")) if raw else None
    current = match is not None and match.group() == _snippet()
    executable = raw is not None and os.access(repo.hook, os.X_OK)
    wired = bool(executable and match and b"brigade work verify push-gate run" in match.group())
    brigade_on_path = which("brigade") is not None
    timeout_on_path = which("timeout") is not None
    rows = push_gate_log.read(push_gate_log.log_path(repo.common))
    recent = datetime.now(UTC) - timedelta(days=7)
    stamps = [datetime.fromisoformat(row["time"]) for row in rows]
    return {
        "mode": "warn" if mode in {"warn", "enforce"} else "off",
        "configured_mode": mode,
        "hook_path": str(repo.hook),
        "hook_exists": raw is not None,
        "hook_executable": executable,
        "hook_version": int(match[1]) if match else None,
        "current": current,
        "wired": wired,
        "brigade_on_path": brigade_on_path,
        "timeout_on_path": timeout_on_path,
        "will_run": mode in {"warn", "enforce"} and wired and brigade_on_path and timeout_on_path,
        "last_push": max(stamps).astimezone(UTC).isoformat() if stamps else None,
        "recent_gate_errors": sum(
            row["result"] == "gate_error" and stamp >= recent for row, stamp in zip(rows, stamps, strict=True)
        ),
    }


def describe(info: dict[str, Any]) -> str:
    mode = info["mode"]
    if info["configured_mode"] == "enforce":
        mode += " (enforce does not exist yet)"
    elif info["configured_mode"] not in {None, "off", "warn"}:
        mode += " (unrecognized configuration)"
    active = "wired" if info["wired"] else "not wired"
    path_status = "brigade on PATH" if info["brigade_on_path"] else "cannot run: brigade missing from PATH"
    if info["timeout_on_path"]:
        path_status += ", timeout on PATH"
    elif info["mode"] == "warn":
        path_status += ", configured but will not run: timeout missing from PATH"
    else:
        path_status += ", timeout missing from PATH"
    version = f"v{info['hook_version']}" if info["hook_version"] is not None else "none"
    freshness = ", update available" if info["wired"] and not info["current"] else ""
    return (
        f"mode={mode}, {active}, hook={info['hook_path']}, version={version}{freshness}, {path_status}, "
        f"last push={info['last_push'] or 'none'}, last 7 days gate errors={info['recent_gate_errors']}"
    )


def _install(repo: Repository, mode: str) -> None:
    _write_mode(repo.root, mode)
    if repo.hook.parent.resolve() == repo.common / "hooks" and not os.path.lexists(repo.hook):
        repo.hook.parent.mkdir(parents=True, exist_ok=True)
        try:
            with repo.hook.open("xb") as stream:
                stream.write(_minimal_hook())
            repo.hook.chmod(0o755)
        except FileExistsError:
            pass
    info = status(repo)
    print(describe(info))
    if not info["wired"] or not info["current"]:
        print("The effective hook was left in place. Add or update these lines after capturing pre-push input:")
        print(_snippet().decode("ascii"), end="")
        print("For a Brigade content guard hook, refresh from the shipped templates/hooks/pre-push file.")


def _uninstall(repo: Repository) -> None:
    _write_mode(repo.root, None)
    if (
        repo.hook.parent.resolve() == repo.common / "hooks"
        and not repo.hook.is_symlink()
        and _read_hook(repo.hook) == _minimal_hook()
    ):
        repo.hook.unlink()


def _report_text(report: dict[str, Any]) -> None:
    def line(label: str, info: dict[str, Any]) -> None:
        counts = ", ".join(f"{key}={value}" for key, value in sorted(info["results"].items())) or "no results"
        print(
            f"{label}  {counts} | would block {info['would_block_share']:.1%} | gate errors {info['gate_errors']}"
            f" | median {info['median_ms']} ms | p95 {info['p95_ms']} ms"
        )

    line("Total", report)
    for day, info in report["days"].items():
        line(day, info)


def register(parsers: Any) -> None:
    gate = parsers.add_parser("push-gate", help="Manage the advisory pre-push verification gate.")
    commands = gate.add_subparsers(dest="push_gate_command", required=True)
    commands.add_parser("run", help="Run the advisory check from a Git pre-push hook.")
    for name in ("install", "status", "uninstall", "report"):
        parser = commands.add_parser(name)
        parser.add_argument("--target", "-t", type=Path, default=Path("."))
        if name == "install":
            parser.add_argument("--mode", default="warn", metavar="{warn,off}")
        if name in {"status", "report"}:
            parser.add_argument("--json", action="store_true")
        if name == "report":
            parser.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD")


def dispatch(args: argparse.Namespace) -> int:
    command = args.push_gate_command
    if command == "run":
        from .push_gate_hook import run

        return run()
    if command == "install" and args.mode not in {"warn", "off"}:
        error = "enforce mode does not exist yet" if args.mode == "enforce" else "expected mode warn or off"
        print(f"push gate: {error}", file=sys.stderr)
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
            _install(repo, args.mode)
        else:
            _uninstall(repo)
            print(f"push gate: mode unset for {repo.root}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"push gate: {exc}", file=sys.stderr)
        return 1
    return 0
