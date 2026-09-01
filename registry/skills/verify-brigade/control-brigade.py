#!/usr/bin/env python3
"""Drive the real Brigade CLI against a throwaway target and capture evidence.

Every subcommand prints one JSON object on stdout. Exit codes:

  0  the drive ran and the observation matched what a healthy rig produces
  1  the helper itself failed (brigade not found, unsafe path, bad target)
  2  usage error (argparse)
  3  the drive ran fine but the observation is a failure (doctor FAILs,
     verification did not complete)

Nothing here touches the operator workspace or the Brigade checkout: targets are
created under a state root (default $TMPDIR/verify-brigade) and only paths this
helper recorded are ever removed.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "verify-brigade.control.v1"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DEPTH = "workspace"
DEFAULT_HARNESSES = "claude,codex"
DEFAULT_PROBE = "python3 --version"
DEFAULT_TIMEOUT = 300

# Non-secret placeholder. The pack config check only proves the reference
# resolves; nothing authenticates with this value.
BEARER_ENV_NAME = "VERIFY_BRIGADE_PACK_BEARER"
BEARER_PLACEHOLDER = "not-a-real-bearer-verify-brigade"

FEED_SAMPLE = {
    "schema": "brigade.grokbot.feed.v1",
    "approved": True,
    "label": "verify-brigade sample feed",
    "entries": [
        {
            "idempotency_key": "verify-brigade-sample-001",
            "spec": {
                "label": "sample implementation worker job",
                "role": "implementation-worker",
                "repository": "example-org/example-repo",
                "base_ref": "main",
                "ownership_paths": ["docs"],
                "instructions": "Sample instructions used only for feed validation.",
                "verification_commands": ["python3 --version"],
                "artifact": {"kind": "branch"},
                "timeout_seconds": 600,
            },
        }
    ],
}


class HelperError(Exception):
    """The helper cannot proceed; report it instead of driving anything."""


# ---------------------------------------------------------------- primitives


def emit(payload: dict, code: int = 0) -> int:
    payload.setdefault("schema", SCHEMA)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return code


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def resolve_brigade(explicit: str | None) -> list[str]:
    """Resolve the brigade command: --brigade, $BRIGADE_BIN, .venv, PATH, module."""
    for candidate in (explicit, os.environ.get("BRIGADE_BIN")):
        if candidate:
            parts = shlex.split(candidate)
            if not parts:
                raise HelperError(f"empty brigade command: {candidate!r}")
            return parts
    venv = REPO_ROOT / ".venv" / "bin" / "brigade"
    if venv.exists():
        return [str(venv)]
    found = shutil.which("brigade")
    if found:
        return [found]
    return [sys.executable, "-m", "brigade"]


def state_root(args: argparse.Namespace) -> Path:
    root = Path(args.root).expanduser().resolve() if args.root else Path(tempfile.gettempdir()) / "verify-brigade"
    root.mkdir(parents=True, exist_ok=True)
    (root / "captures").mkdir(exist_ok=True)
    (root / "targets").mkdir(exist_ok=True)
    return root


def read_state(root: Path) -> dict:
    path = root / "state.json"
    if not path.exists():
        return {"schema": "verify-brigade.state.v1", "created_targets": []}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": "verify-brigade.state.v1", "created_targets": []}
    if not isinstance(loaded, dict):
        return {"schema": "verify-brigade.state.v1", "created_targets": []}
    loaded.setdefault("created_targets", [])
    return loaded


def write_state(root: Path, state: dict) -> None:
    path = root / "state.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def guard_target(path: Path) -> Path:
    """Refuse to drive Brigade against the operator home or this checkout."""
    resolved = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    forbidden = {home, Path("/"), REPO_ROOT}
    if resolved in forbidden:
        raise HelperError(f"refusing to use {resolved} as a target (home, root, or the Brigade checkout)")
    if resolved == REPO_ROOT or REPO_ROOT.is_relative_to(resolved):
        raise HelperError(f"refusing to use {resolved}: it contains the Brigade checkout")
    return resolved


def record_capture(root: Path, name: str, record: dict) -> Path:
    path = root / "captures" / f"{now_stamp()}-{uuid.uuid4().hex[:6]}-{name}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def run(
    argv: list[str],
    *,
    root: Path,
    name: str,
    env_extra: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Run one command, capture it to the state root, and return the record."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, check=False)
    except FileNotFoundError as exc:
        raise HelperError(f"command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired:
        record = {
            "argv": argv,
            "name": name,
            "returncode": None,
            "timed_out": True,
            "timeout_seconds": timeout,
            "stdout": "",
            "stderr": "",
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        record["capture"] = str(record_capture(root, name, record))
        return record
    record = {
        "argv": argv,
        "name": name,
        "returncode": proc.returncode,
        "timed_out": False,
        "timeout_seconds": timeout,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    try:
        record["json"] = json.loads(proc.stdout)
    except ValueError:
        record["json"] = None
    record["capture"] = str(record_capture(root, name, record))
    return record


def slim(record: dict) -> dict:
    """A record small enough to print without drowning the reader."""
    return {
        "argv": record["argv"],
        "returncode": record["returncode"],
        "timed_out": record["timed_out"],
        "duration_seconds": record["duration_seconds"],
        "stdout_tail": record["stdout"][-800:],
        "stderr_tail": record["stderr"][-800:],
        "capture": record["capture"],
    }


def brigade_args(args: argparse.Namespace) -> list[str]:
    return resolve_brigade(args.brigade)


# ------------------------------------------------------------- subcommands


def cmd_doctor(args: argparse.Namespace) -> int:
    """Read-only: is this rig worth driving?"""
    root = state_root(args)
    checks: list[dict] = []
    brigade = brigade_args(args)

    version = run(brigade + ["--version"], root=root, name="doctor-version", timeout=60)
    checks.append(
        {
            "name": "brigade cli",
            "status": "ok" if version["returncode"] == 0 else "fail",
            "detail": (version["stdout"] or version["stderr"]).strip() or "no output",
        }
    )

    git = shutil.which("git")
    checks.append(
        {
            "name": "git",
            "status": "ok" if git else "fail",
            "detail": git or "git is required to create a target",
        }
    )

    pyproject = REPO_ROOT / "pyproject.toml"
    checks.append(
        {
            "name": "brigade checkout",
            "status": "ok" if pyproject.exists() else "fail",
            "detail": str(pyproject),
        }
    )

    checks.append(
        {
            "name": "state root writable",
            "status": "ok" if os.access(root, os.W_OK) else "fail",
            "detail": str(root),
        }
    )

    failed = [check for check in checks if check["status"] == "fail"]
    return emit(
        {
            "action": "doctor",
            "ok": not failed,
            "brigade": brigade,
            "python": sys.version.split()[0],
            "repo_root": str(REPO_ROOT),
            "state_root": str(root),
            "checks": checks,
        },
        0 if not failed else 3,
    )


def cmd_new_target(args: argparse.Namespace) -> int:
    """git init + brigade init in a fresh temp dir; print the path."""
    root = state_root(args)
    brigade = brigade_args(args)
    target = Path(tempfile.mkdtemp(prefix=f"{now_stamp()}-", dir=root / "targets"))

    git = shutil.which("git")
    if not git:
        raise HelperError("git is required to create a target")
    git_record = run([git, "init", "-q", "-b", "main", str(target)], root=root, name="new-target-git", timeout=60)
    if git_record["returncode"] != 0:
        shutil.rmtree(target, ignore_errors=True)
        return emit({"action": "new-target", "ok": False, "stage": "git-init", "git": slim(git_record)}, 1)

    init_record = run(
        brigade + ["init", "--target", str(target), "--depth", args.depth, "--harnesses", args.harnesses],
        root=root,
        name="new-target-init",
        timeout=args.timeout,
    )
    if init_record["returncode"] != 0:
        return emit(
            {
                "action": "new-target",
                "ok": False,
                "stage": "brigade-init",
                "target": str(target),
                "init": slim(init_record),
            },
            3,
        )

    state = read_state(root)
    state["created_targets"] = sorted(set(state["created_targets"]) | {str(target)})
    write_state(root, state)

    return emit(
        {
            "action": "new-target",
            "ok": True,
            "target": str(target),
            "depth": args.depth,
            "harnesses": args.harnesses,
            "state_root": str(root),
            "brigade_dir": str(target / ".brigade"),
            "git": slim(git_record),
            "init": slim(init_record),
        }
    )


def cmd_init(args: argparse.Namespace) -> int:
    """Run `brigade init` against an existing directory you already own."""
    root = state_root(args)
    target = guard_target(Path(args.target))
    if not target.is_dir():
        raise HelperError(f"target is not a directory: {target}")
    record = run(
        brigade_args(args) + ["init", "--target", str(target), "--depth", args.depth, "--harnesses", args.harnesses],
        root=root,
        name="init",
        timeout=args.timeout,
    )
    ok = record["returncode"] == 0
    return emit(
        {
            "action": "init",
            "ok": ok,
            "target": str(target),
            "depth": args.depth,
            "harnesses": args.harnesses,
            "init": slim(record),
        },
        0 if ok else 3,
    )


def cmd_doctor_target(args: argparse.Namespace) -> int:
    """`brigade doctor --json` against the temp target. 0 failed is the signal."""
    root = state_root(args)
    target = guard_target(Path(args.target))
    record = run(
        brigade_args(args) + ["doctor", "--target", str(target), "--json"],
        root=root,
        name="doctor-target",
        timeout=args.timeout,
    )
    payload = record["json"] if isinstance(record["json"], dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    failed = summary.get("failed")
    ok = record["returncode"] == 0 and failed == 0
    return emit(
        {
            "action": "doctor-target",
            "ok": ok,
            "target": str(target),
            "ready": payload.get("ready"),
            "owner": payload.get("owner"),
            "depth": payload.get("depth"),
            "harnesses": payload.get("harnesses"),
            "summary": summary,
            "doctor": slim(record),
        },
        0 if ok else 3,
    )


def cmd_work_verify(args: argparse.Namespace) -> int:
    """Bounded `brigade work verify run` in the temp target; return receipt id."""
    root = state_root(args)
    target = guard_target(Path(args.target))
    argv = brigade_args(args) + [
        "work",
        "verify",
        "run",
        "--target",
        str(target),
        "--command",
        args.command,
        "--timeout",
        str(args.command_timeout),
        "--json",
    ]
    if args.capture:
        argv += ["--capture", args.capture]
    record = run(argv, root=root, name="work-verify", timeout=args.timeout)
    payload = record["json"] if isinstance(record["json"], dict) else {}
    status = payload.get("status")
    ok = record["returncode"] == 0 and status == "completed"
    return emit(
        {
            "action": "work-verify",
            "ok": ok,
            "target": str(target),
            "command": args.command,
            "run_id": payload.get("run_id"),
            "status": status,
            "receipt_path": payload.get("path"),
            "outcome_capture": payload.get("outcome_capture"),
            "verify": slim(record),
        },
        0 if ok else 3,
    )


def pack_env(args: argparse.Namespace) -> dict[str, str]:
    if args.bearer_env == BEARER_ENV_NAME and BEARER_ENV_NAME not in os.environ:
        return {BEARER_ENV_NAME: BEARER_PLACEHOLDER}
    return {}


def cmd_pack_setup(args: argparse.Namespace) -> int:
    """Preview (default) or apply non-secret pack instance config."""
    root = state_root(args)
    target = guard_target(Path(args.target))
    apply = args.apply and not args.dry_run
    argv = brigade_args(args) + [
        "run",
        "cloud",
        "grokbot",
        "pack",
        "setup",
        "--target",
        str(target),
        "--id",
        args.id,
        "--bearer-env",
        args.bearer_env,
        "--json",
    ]
    if apply:
        argv.append("--apply")
    record = run(argv, root=root, name="pack-setup", timeout=args.timeout)
    payload = record["json"] if isinstance(record["json"], dict) else {}
    ok = record["returncode"] == 0
    return emit(
        {
            "action": "grokbot-pack-setup",
            "ok": ok,
            "target": str(target),
            "pack_id": args.id,
            "applied": bool(payload.get("apply")),
            "dry_run": bool(args.dry_run),
            "bind": payload.get("bind"),
            "writes": payload.get("writes"),
            "setup": slim(record),
        },
        0 if ok else 3,
    )


def cmd_pack_doctor(args: argparse.Namespace) -> int:
    """Sanitized pack diagnostics. Without a listener, `endpoint` fails."""
    root = state_root(args)
    target = guard_target(Path(args.target))
    record = run(
        brigade_args(args)
        + ["run", "cloud", "grokbot", "pack", "doctor", "--target", str(target), "--id", args.id, "--json"],
        root=root,
        name="pack-doctor",
        env_extra=pack_env(args),
        timeout=args.timeout,
    )
    payload = record["json"] if isinstance(record["json"], dict) else {}
    checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    by_name = {check.get("check"): check.get("status") for check in checks if isinstance(check, dict)}
    return emit(
        {
            "action": "grokbot-pack-doctor",
            # The command answered with a sanitized check list. A failing
            # endpoint check is the expected reading with no listener up.
            "ok": bool(checks),
            "target": str(target),
            "pack_id": args.id,
            "checks": checks,
            "config_resolved": by_name.get("config") == "ok",
            "listener_running": by_name.get("endpoint") == "ok",
            "doctor": slim(record),
        },
        0 if checks else 3,
    )


def cmd_pack_canary(args: argparse.Namespace) -> int:
    """Bounded non-mutating auth + inventory check against the pack listener."""
    root = state_root(args)
    target = guard_target(Path(args.target))
    record = run(
        brigade_args(args)
        + ["run", "cloud", "grokbot", "pack", "canary", "--target", str(target), "--id", args.id, "--json"],
        root=root,
        name="pack-canary",
        env_extra=pack_env(args),
        timeout=args.timeout,
    )
    payload = record["json"] if isinstance(record["json"], dict) else {}
    reason = payload.get("reason")
    listener_running = bool(payload.get("ok"))
    # `reason: health` with no listener bound is the proof the command ran:
    # it resolved config, dialed the bind, and reported the miss.
    understood = listener_running or reason in {"health", "config", "dependency"}
    return emit(
        {
            "action": "grokbot-pack-canary",
            "ok": understood,
            "target": str(target),
            "pack_id": args.id,
            "listener_running": listener_running,
            "reason": reason,
            "expected_without_listener": reason == "health",
            "canary": slim(record),
        },
        0 if understood else 3,
    )


def cmd_pack_remove(args: argparse.Namespace) -> int:
    """Preview (default) or apply removal of pack config this run wrote."""
    root = state_root(args)
    target = guard_target(Path(args.target))
    apply = args.apply and not args.dry_run
    argv = brigade_args(args) + [
        "run",
        "cloud",
        "grokbot",
        "pack",
        "remove",
        "--target",
        str(target),
        "--id",
        args.id,
        "--json",
    ]
    if apply:
        argv.append("--apply")
    record = run(argv, root=root, name="pack-remove", timeout=args.timeout)
    payload = record["json"] if isinstance(record["json"], dict) else {}
    ok = record["returncode"] == 0
    return emit(
        {
            "action": "grokbot-pack-remove",
            "ok": ok,
            "target": str(target),
            "pack_id": args.id,
            "applied": bool(payload.get("apply")),
            "dry_run": bool(args.dry_run),
            "paths": payload.get("paths"),
            "remove": slim(record),
        },
        0 if ok else 3,
    )


def cmd_feed(args: argparse.Namespace) -> int:
    """Validate an approved feed manifest. Validation only; never enqueues."""
    root = state_root(args)
    target = guard_target(Path(args.target))
    if args.sample:
        manifest = root / "sample-feed-manifest.json"
        manifest.write_text(json.dumps(FEED_SAMPLE, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest.chmod(0o600)
    else:
        if not args.manifest:
            raise HelperError("pass --manifest PATH or --sample")
        manifest = Path(args.manifest).expanduser().resolve()
    record = run(
        brigade_args(args)
        + [
            "run",
            "cloud",
            "grokbot",
            "feed",
            "--target",
            str(target),
            "--manifest",
            str(manifest),
            "--limit",
            str(args.limit),
            "--json",
        ],
        root=root,
        name="feed",
        timeout=args.timeout,
    )
    payload = record["json"] if isinstance(record["json"], dict) else {}
    ok = record["returncode"] == 0
    return emit(
        {
            "action": "grokbot-feed",
            "ok": ok,
            "target": str(target),
            "manifest": str(manifest),
            "sample": bool(args.sample),
            "valid": payload.get("valid"),
            "known": payload.get("known"),
            "limit": payload.get("limit"),
            "feed": slim(record),
        },
        0 if ok else 3,
    )


def cmd_status(args: argparse.Namespace) -> int:
    """Safe Grok Bot job projections. Needs hub authority; reports why not."""
    root = state_root(args)
    target = guard_target(Path(args.target))
    record = run(
        brigade_args(args) + ["run", "cloud", "grokbot", "status", "--target", str(target), "--json"],
        root=root,
        name="grokbot-status",
        timeout=args.timeout,
    )
    payload = record["json"] if isinstance(record["json"], dict) else {}
    message = (record["stdout"] + record["stderr"]).strip()
    reason = message.removeprefix("error:").strip() if message.startswith("error:") else None
    ok = record["returncode"] == 0
    return emit(
        {
            "action": "grokbot-status",
            "ok": ok,
            "target": str(target),
            "reason": reason,
            "jobs": payload.get("jobs"),
            "status": slim(record),
        },
        0 if ok else 3,
    )


def cmd_handoff_lint(args: argparse.Namespace) -> int:
    """Lint the handoff inbox of the temp target."""
    root = state_root(args)
    target = guard_target(Path(args.target))
    argv = brigade_args(args) + ["handoff", "lint", "--target", str(target), "--json"]
    if args.content_guard:
        argv.append("--content-guard")
    argv += list(args.paths)
    record = run(argv, root=root, name="handoff-lint", timeout=args.timeout)
    payload = record["json"] if isinstance(record["json"], dict) else {}
    ok = record["returncode"] == 0 and payload.get("valid") is True
    return emit(
        {
            "action": "handoff-lint",
            "ok": ok,
            "target": str(target),
            "valid": payload.get("valid"),
            "count": payload.get("count"),
            "results": payload.get("results"),
            "lint": slim(record),
        },
        0 if ok else 3,
    )


def cmd_evidence(args: argparse.Namespace) -> int:
    """Copy captures and receipts somewhere cleanup never looks."""
    root = state_root(args)
    target = guard_target(Path(args.target)) if args.target else None
    base = (
        Path(args.evidence_root).expanduser().resolve()
        if args.evidence_root
        else REPO_ROOT / ".brigade" / "verification-evidence"
    )
    evidence = base / f"{now_stamp()}-{args.label}"
    (evidence / "captures").mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for capture in sorted((root / "captures").glob("*.json")):
        destination = evidence / "captures" / capture.name
        shutil.copy2(capture, destination)
        copied.append(str(destination.relative_to(evidence)))

    if target is not None:
        for relative in ("work/verify-runs", "grokbot", "work/sessions"):
            source = target / ".brigade" / relative
            if not source.exists():
                continue
            destination = evidence / "target-brigade" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination, dirs_exist_ok=True)
            copied.append(str(destination.relative_to(evidence)))

    manifest = {
        "schema": "verify-brigade.evidence.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "label": args.label,
        "evidence_dir": str(evidence),
        "state_root": str(root),
        "target": str(target) if target else None,
        "items": copied,
    }
    (evidence / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return emit({"action": "evidence", "ok": True, "evidence_dir": str(evidence), "items": copied})


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Remove only the targets this helper created. Evidence is never touched."""
    root = state_root(args)
    state = read_state(root)
    recorded = [Path(entry) for entry in state["created_targets"]]

    if args.target:
        wanted = Path(args.target).expanduser().resolve()
        recorded = [entry for entry in recorded if entry == wanted]
        if not recorded:
            raise HelperError(f"{wanted} was not created by this helper; refusing to remove it")

    removed: list[str] = []
    skipped: list[dict] = []
    for entry in recorded:
        if not entry.is_relative_to(root / "targets"):
            skipped.append({"path": str(entry), "reason": "outside the state root"})
            continue
        if not entry.exists():
            skipped.append({"path": str(entry), "reason": "already gone"})
            continue
        if args.dry_run:
            removed.append(str(entry))
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(str(entry))

    captures = sorted((root / "captures").glob("*.json"))
    if not args.dry_run:
        if not args.keep_captures:
            for capture in captures:
                capture.unlink(missing_ok=True)
        remaining = {entry for entry in state["created_targets"]} - set(removed)
        state["created_targets"] = sorted(remaining)
        write_state(root, state)

    return emit(
        {
            "action": "cleanup",
            "ok": True,
            "dry_run": bool(args.dry_run),
            "state_root": str(root),
            "removed" if not args.dry_run else "would_remove": removed,
            "captures_removed": 0 if (args.dry_run or args.keep_captures) else len(captures),
            "skipped": skipped,
            "note": "evidence directories are outside the state root and are never removed",
        }
    )


# ------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="control-brigade.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--brigade", default=None, help="Brigade command (default: .venv/bin/brigade, then PATH).")
    parser.add_argument("--root", default=None, help="State root for targets and captures.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Timeout for each brigade call.")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    def add_target(command: argparse.ArgumentParser) -> None:
        command.add_argument("--target", required=True, help="Temp target created by new-target.")

    def add_pack(command: argparse.ArgumentParser) -> None:
        add_target(command)
        command.add_argument("--id", required=True, help="Connector pack id, e.g. implementation-worker.")
        command.add_argument("--bearer-env", default=BEARER_ENV_NAME, help="Env var holding the pack bearer.")

    p = sub.add_parser("doctor", help="Read-only: is this rig worth driving?")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("new-target", help="git init + brigade init in a fresh temp dir.")
    p.add_argument("--depth", default=DEFAULT_DEPTH, choices=["repo", "workspace"])
    p.add_argument("--harnesses", default=DEFAULT_HARNESSES)
    p.set_defaults(func=cmd_new_target)

    p = sub.add_parser("init", help="Run brigade init against an existing directory.")
    add_target(p)
    p.add_argument("--depth", default=DEFAULT_DEPTH, choices=["repo", "workspace"])
    p.add_argument("--harnesses", default=DEFAULT_HARNESSES)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor-target", help="brigade doctor --json against the temp target.")
    add_target(p)
    p.set_defaults(func=cmd_doctor_target)

    p = sub.add_parser("work-verify", help="Bounded brigade work verify run; returns receipt id and status.")
    add_target(p)
    p.add_argument("--command", default=DEFAULT_PROBE, help="Verification command to run in the target.")
    p.add_argument("--capture", default=None, help="Artifact id for atomic --capture.")
    p.add_argument("--command-timeout", type=int, default=60, help="Per-command timeout inside Brigade.")
    p.set_defaults(func=cmd_work_verify)

    p = sub.add_parser("grokbot-pack-setup", help="Preview or apply pack instance config.")
    add_pack(p)
    p.add_argument("--apply", action="store_true", help="Write config. Default is preview only.")
    p.add_argument("--dry-run", action="store_true", help="Force preview even with --apply.")
    p.set_defaults(func=cmd_pack_setup)

    p = sub.add_parser("grokbot-pack-doctor", help="Sanitized pack diagnostics.")
    add_pack(p)
    p.set_defaults(func=cmd_pack_doctor)

    p = sub.add_parser("grokbot-pack-canary", help="Bounded non-mutating pack auth and inventory check.")
    add_pack(p)
    p.set_defaults(func=cmd_pack_canary)

    p = sub.add_parser("grokbot-pack-remove", help="Preview or apply removal of pack config.")
    add_pack(p)
    p.add_argument("--apply", action="store_true", help="Delete the files. Default is preview only.")
    p.add_argument("--dry-run", action="store_true", help="Force preview even with --apply.")
    p.set_defaults(func=cmd_pack_remove)

    p = sub.add_parser("grokbot-feed", help="Validate an approved feed manifest (never enqueues).")
    add_target(p)
    p.add_argument("--manifest", default=None, help="Path to an approved private feed manifest.")
    p.add_argument("--sample", action="store_true", help="Write and validate a bundled sample manifest.")
    p.add_argument("--limit", type=int, default=1)
    p.set_defaults(func=cmd_feed)

    p = sub.add_parser("grokbot-status", help="Safe Grok Bot job projections.")
    add_target(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("handoff-lint", help="Lint the handoff inbox of the temp target.")
    add_target(p)
    p.add_argument("--content-guard", action="store_true", help="Add the leak and injection scan.")
    p.add_argument("paths", nargs="*", help="Explicit handoff files. Defaults to the pending inbox.")
    p.set_defaults(func=cmd_handoff_lint)

    p = sub.add_parser("evidence", help="Copy captures and receipts to a named evidence dir.")
    p.add_argument("--target", default=None, help="Temp target whose receipts to copy.")
    p.add_argument("--label", default="run", help="Suffix for the evidence directory name.")
    p.add_argument("--evidence-root", default=None, help="Where evidence dirs live.")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("cleanup", help="Remove only the targets this helper created.")
    p.add_argument("--target", default=None, help="Remove one recorded target instead of all of them.")
    p.add_argument("--dry-run", action="store_true", help="Report what would be removed.")
    p.add_argument("--keep-captures", action="store_true", help="Leave capture files in the state root.")
    p.set_defaults(func=cmd_cleanup)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except HelperError as exc:
        return emit({"action": args.command, "ok": False, "error": str(exc)}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
