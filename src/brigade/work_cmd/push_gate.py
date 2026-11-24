"""Manage the clone-local, advisory Git pre-push verification gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .. import __version__
from . import push_gate_log

_MODE_KEY = "brigade.pushGate"
_HOOKS_KEY = "core.hooksPath"
_MARKER = b"# Brigade managed push gate "
_GUARD_START = b"# Brigade content guard follows\n"
_SHEBANG = b"#!/usr/bin/env bash\n"


def _git(target: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True, timeout=2, check=False)


def _config(target: Path, key: str, *, local: bool = False) -> str | None:
    result = _git(target, "config", *(["--local"] if local else []), "--get", key)
    if result.returncode not in (0, 1):
        raise OSError(f"could not read Git config key {key}")
    return result.stdout.removesuffix("\n") if result.returncode == 0 else None


def _write_config(target: Path, key: str, value: str | None) -> None:
    args = ("--unset-all", key) if value is None else ("--replace-all", key, value)
    result = _git(target, "config", "--local", *args)
    if result.returncode != 0 and not (value is None and result.returncode == 5):
        raise OSError(f"could not update Git config key {key}")


@dataclass(frozen=True)
class Repository:
    root: Path
    common: Path
    hook: Path
    hooks_setting: str | None
    outside: bool

    @classmethod
    def locate(cls, target: Path) -> Repository | None:
        result = _git(
            target, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir", "--git-path", "hooks"
        )
        if result.returncode != 0:
            return None
        root, common, hooks = (Path(line).resolve() for line in result.stdout.splitlines())
        configured = _config(root, _HOOKS_KEY)
        allowed = [root, common]
        if common.name == ".git":
            allowed.append(common.parent)
        return cls(root, common, hooks / "pre-push", configured, not any(hooks.is_relative_to(p) for p in allowed))


def _guard_bytes() -> bytes:
    return files("brigade").joinpath("templates", "hooks", "pre-push").read_bytes()


@dataclass(frozen=True)
class Hook:
    installed: bool = False
    guard: bytes = b""
    guard_mode: int = 0o755
    pinned: bool = False
    prior_local: str | None = None
    raw: bytes = b""
    problem: str | None = None

    @classmethod
    def read(cls, repo: Repository) -> Hook:
        if repo.outside:
            return cls(problem="outside core.hooksPath")
        try:
            info = repo.hook.lstat()
        except FileNotFoundError:
            return cls()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024:
            return cls(problem="foreign hook")
        raw = repo.hook.read_bytes()
        if raw == _guard_bytes():
            return cls(guard=raw, guard_mode=stat.S_IMODE(info.st_mode))
        first, _, rest = raw.partition(b"\n")
        header, _, body = rest.partition(b"\n")
        if first + b"\n" == _SHEBANG and header.startswith(_MARKER):
            try:
                digest, pinned, prior_local, guard_mode = json.loads(header[len(_MARKER) :])
                if digest == hashlib.sha256(body).hexdigest():
                    _, separator, guard = body.partition(_GUARD_START)
                    if not separator or guard.startswith(_SHEBANG):
                        return cls(True, guard, guard_mode, pinned, prior_local, raw)
            except (TypeError, ValueError, RecursionError):
                pass
        return cls(problem="foreign hook")

    def render(self, common: Path) -> bytes:
        python = shlex.quote(Path(sys.executable).as_posix())
        common_arg = shlex.quote(common.as_posix())
        guard_active = bool(self.guard and self.guard_mode & 0o111)
        body = (
            f"# Brigade version {__version__}\n"
            'case "$(git config --local --get brigade.pushGate 2>/dev/null)" in\n'
            # Add enforcement here when the operator enables that mode.
            "  warn|enforce)\n"
            '    if refs="$(cat)"; then\n'
            f'      {python} -I -m brigade.work_cmd.push_gate_hook {common_arg} < <(printf "%s" "$refs") || '
            'echo "push gate: advisory hook failed" >&2\n'
        )
        if guard_active:
            body += '      exec 0< <(printf "%s\\n" "$refs")\n'
        body += "    fi\n    ;;\nesac\n"
        if not guard_active and self.guard:
            body += "exit 0\n"
        payload = body.encode("utf-8") + (_GUARD_START + self.guard if self.guard else b"exit 0\n")
        metadata = json.dumps([hashlib.sha256(payload).hexdigest(), self.pinned, self.prior_local, self.guard_mode])
        return _SHEBANG + _MARKER + metadata.encode("ascii") + b"\n" + payload

    def current(self, repo: Repository) -> bool:
        return self.installed and self.raw == self.render(repo.common) and os.access(repo.hook, os.X_OK)


def _replace(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            with stream:
                stream.write(content)
            temp.chmod(mode)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def _install(repo: Repository, hook: Hook, mode: str) -> None:
    prior_mode = _config(repo.root, _MODE_KEY, local=True)
    pin = repo.hooks_setting is not None and not Path(repo.hooks_setting).is_absolute()
    if pin:
        hook = Hook(
            guard=hook.guard,
            guard_mode=hook.guard_mode,
            pinned=True,
            prior_local=_config(repo.root, _HOOKS_KEY, local=True),
        )
        # Relative hooksPath is interpreted per worktree. Pin the resolved
        # directory so all Git worktrees reach the same installed hook.
    try:
        if pin:
            _write_config(repo.root, _HOOKS_KEY, str(repo.hook.parent))
            if _config(repo.root, _HOOKS_KEY) != str(repo.hook.parent):
                raise OSError("worktree-specific core.hooksPath prevents a shared hook")
        _write_config(repo.root, _MODE_KEY, mode)
        _replace(repo.hook, hook.render(repo.common), 0o755)
    except (OSError, subprocess.TimeoutExpired):
        _write_config(repo.root, _MODE_KEY, prior_mode)
        if pin:
            _write_config(repo.root, _HOOKS_KEY, hook.prior_local)
        raise


def _uninstall(repo: Repository, hook: Hook) -> None:
    _write_config(repo.root, _MODE_KEY, None)
    if not hook.installed:
        return
    if hook.guard:
        _replace(repo.hook, hook.guard, hook.guard_mode)
    else:
        repo.hook.unlink()
    if hook.pinned and _config(repo.root, _HOOKS_KEY, local=True) == str(repo.hook.parent):
        _write_config(repo.root, _HOOKS_KEY, hook.prior_local)


def status(repo: Repository, hook: Hook) -> dict[str, Any]:
    mode = _config(repo.root, _MODE_KEY, local=True)
    rows = push_gate_log.read(push_gate_log.log_path(repo.common))
    recent = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    return {
        "installed": hook.installed,
        "mode": "warn" if mode in {"warn", "enforce"} else "off",
        "configured_mode": mode,
        "hook_path": str(repo.hook),
        "current": hook.current(repo),
        "content_guard": bool(hook.guard),
        "problem": hook.problem,
        "last_push": rows[-1]["time"] if rows else None,
        "recent_gate_errors": sum(row["result"] == "gate_error" and row["time"] >= recent for row in rows),
    }


def describe(info: dict[str, Any]) -> str:
    installed = "installed" if info["installed"] else "absent"
    version = "current" if info["current"] else "outdated or inactive" if info["installed"] else "unavailable"
    configured = info["configured_mode"]
    mode = info["mode"]
    if configured == "enforce":
        mode += " (enforce does not exist yet)"
    elif configured not in {None, "off", "warn"}:
        mode += " (unrecognized configuration)"
    detail = f"{installed}, mode={mode}, hook={info['hook_path']}, version={version}"
    if info["problem"]:
        detail += f", {info['problem']}"
    return f"{detail}, last push={info['last_push'] or 'none'}, last 7 days gate errors={info['recent_gate_errors']}"


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
            return 0

        hook = Hook.read(repo)
        if command == "status":
            info = status(repo, hook)
            print(json.dumps(info) if args.json else describe(info))
        else:
            if hook.problem:
                raise OSError(f"{hook.problem} at {repo.hook}; existing hooks were preserved")
            if command == "install":
                _install(repo, hook, args.mode)
            else:
                _uninstall(repo, hook)
            print(f"push gate: {command} complete for {repo.hook}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"push gate: {exc}", file=sys.stderr)
        return 1
    return 0
