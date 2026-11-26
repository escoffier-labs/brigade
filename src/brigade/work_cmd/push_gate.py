"""Configure and inspect the clone-local Git push gate."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from shutil import which
from typing import Any

from .. import config
from . import push_gate_log

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


def _normalized_mode(mode: str | None) -> str:
    return (mode or "").strip().lower()


def _timeout_state() -> str:
    executable = which("timeout")
    if executable is None:
        return "missing"
    try:
        if subprocess.run([executable, *_TIMEOUT_PROBE], capture_output=True, timeout=2, check=False).returncode == 0:
            return "usable"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "incompatible"


def _enforce_issue(info: dict[str, Any]) -> str | None:
    if not info["wired"]:
        return "Wire the effective executable pre-push hook with the current Brigade block."
    if info["hook_version"] == 1:
        return ENFORCE_V1_NOTICE
    if not info["current"]:
        return "Replace the installed Brigade hook block with the current block."
    if not info["brigade_on_path"]:
        return "Put brigade on Git's PATH."
    if not info["timeout_on_path"]:
        return "Put GNU coreutils timeout with -k support on Git's PATH."
    return None


def status(repo: Repository) -> dict[str, Any]:
    configured = _mode(repo.root)
    mode = _normalized_mode(configured)
    effective = _mode(repo.root, local=False)
    raw = _read_hook(repo.hook)
    match = _GATE_BLOCK.search(raw.replace(b"\r\n", b"\n")) if raw else None
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
    info["enforce_effective"] = mode == "enforce" and _enforce_issue(info) is None
    return info


def describe(info: dict[str, Any]) -> str:
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
    if mode == "enforce" and (issue := _enforce_issue(info)):
        description += f". {issue}"
    return description


def _install(repo: Repository, mode: str) -> None:
    if mode != "enforce":
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
    issue = _enforce_issue(info) if mode == "enforce" else None
    if mode == "enforce" and issue is None and info["mode"] != mode:
        source = _git(repo.root, "config", "--local", "--includes", "--null", "--show-origin", "--get", _MODE_KEY)
        if source.returncode not in (0, 1):
            raise OSError(f"could not locate Git config key {_MODE_KEY}")
        origin = source.stdout.partition("\0")[0].removeprefix("file:")
        if origin and (repo.root / origin).resolve() != repo.common / "config":
            issue = f"Set {_MODE_KEY}=enforce in the included Git config {origin!r}."
        else:
            _write_mode(repo.root, mode)
            info = status(repo)
    print(describe(info))
    if not info["wired"] or not info["current"]:
        print("The effective hook was left in place. Add or update these lines after capturing pre-push input:")
        print(_snippet().decode("ascii"), end="")
        if info["hook_version"] == 1:
            print("The v1 hook allows the push even in enforce mode. Replace its marked block with v2.")
        print("For a Brigade content guard hook, refresh from the shipped templates/hooks/pre-push file.")
    if issue is not None:
        raise ValueError(issue)


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
        median = f"{info['median_ms']} ms" if info["median_ms"] is not None else "n/a"
        p95 = f"{info['p95_ms']} ms" if info["p95_ms"] is not None else "n/a"
        print(
            f"{label}  {counts} | would block {info['would_block_share']:.1%} | blocked rows {info['blocked']}"
            f" | gate errors {info['gate_errors']}"
            f" | median {median} | p95 {p95}"
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
            parser.add_argument("--mode", default="warn", metavar="{warn,enforce,off}")
        if name in {"status", "report"}:
            parser.add_argument("--json", action="store_true")
        if name == "report":
            parser.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD")


def dispatch(args: argparse.Namespace) -> int:
    command = args.push_gate_command
    if command == "run":
        from .push_gate_hook import run

        return run(hook_v2=args.hook_v2)
    if command == "install" and args.mode not in {"warn", "enforce", "off"}:
        print("push gate: expected mode warn, enforce, or off", file=sys.stderr)
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
        elif command == "prune":
            from .verification import _prune_verify_runs
            from .verify_push import _RECEIPT_LIMIT, _Indeterminate, _Receipts

            if config.resolve_verify_runs_keep(repo.root) > _RECEIPT_LIMIT:
                raise ValueError(f"Set verify_runs_keep in {config.CONFIG_REL_PATH} to at most {_RECEIPT_LIMIT}.")
            removed = _prune_verify_runs(repo.root)
            try:
                remaining = len(_Receipts(repo.root, time.monotonic() + 2).names)
            except _Indeterminate as exc:
                raise ValueError(str(exc)) from exc
            print(f"Pruned {removed} local verify run(s). Local receipts remaining: {remaining}.")
        else:
            _uninstall(repo)
            print(f"push gate: mode unset for {repo.root}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"push gate: {exc}", file=sys.stderr)
        return 1
    return 0
