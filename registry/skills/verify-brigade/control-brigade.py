#!/usr/bin/env python3
"""Drive the real Brigade CLI against a throwaway target and capture evidence.

Every subcommand prints one JSON object on stdout. Exit codes:

  0  the drive ran and the observation matched what a healthy rig produces
  1  the helper itself failed (brigade not found, unsafe path, bad target)
  2  usage error (argparse)
  3  the drive ran fine but the observation is a failure (doctor FAILs,
     verification did not complete)

No drive touches the operator workspace. A target is accepted only when it
resolves under `<state-root>/targets`, so the operator home, this checkout, and
every path outside the state root are refused before a command runs.

`--root` and the default state root get the same treatment: both are resolved,
both are refused if they resolve to or contain the home directory or the
checkout, and neither may be a symlink at the root or at `targets/` and
`captures/`. Those refusals run before the temp base allowance, so a `$TMPDIR`
pointed at the home or the checkout cannot launder a path back into either; the
one carve-out is a temp base that sits inside the home directory and outside the
checkout. A relative `$TMPDIR` is refused outright, because it would resolve
against the working directory. The temp base itself is refused too, because a
state root there would chmod the shared scratch tree.

Two deliberate exceptions to "nothing under the checkout":

  * evidence. The default evidence root is `<checkout>/.brigade/verification-evidence`,
    which is inside this checkout - gitignored, and the point is that `cleanup`
    cannot reach it. The default and `--evidence-root` are both guarded like
    `--root`, symlink check included, and `--label` must be a simple
    `[A-Za-z0-9._-]` name so it cannot steer the directory anywhere.
  * `grokbot-feed --manifest`. That path is a caller's own manifest, read for
    validation only, and is the one flag outside the target contract.

Only paths this helper recorded are ever removed.

`audit` is read-only. It reads the feature map and the live CLI command tree,
writes nothing, and needs no target or state root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "verify-brigade.control.v1"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / ".brigade" / "verification-evidence"
DEFAULT_DEPTH = "workspace"
DEFAULT_HARNESSES = "claude,codex"
DEFAULT_PROBE = "python3 --version"
DEFAULT_TIMEOUT = 300
LABEL_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")

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

# Guard parity fixture. Every trigger token is split into fragments so this
# tracked file never holds a contiguous match for the content-guard job; the
# drive joins them at run time and writes the result into the temp target.
# All values are fake: "example-user" and "Example User" are not accounts,
# 555 is the guard's own example-number prefix, and the 012 area code cannot
# exist (NANP area codes never start with 0 or 1).
_GP_LH = "local" + "host"
_GP_HOME = "/ho" + "me"
_GP_ROOT = "/ro" + "ot"
_GP_WIN = "C:\\" + "Users"
_GP_HOST = "build.inter" + "nal"
_GP_TAIL = "-67" + "89"
GUARD_PARITY_FIXTURE_LINES = (
    "guard parity fixture: synthetic values only",
    # example-phone-555: (?<!\w) lookbehind, positive
    "desk 555-010-0199 ext",
    # example-phone-555: a word character before the token, and one after it
    "id x555-010-0199 and 555-010-01990",
    # localhost-bare: (?<![\w.-]) lookbehind, positive
    "serve on " + _GP_LH + " today",
    # localhost-bare: word character before, hyphen after
    "no x" + _GP_LH + " or " + _GP_LH + "-dev",
    # home-path: (?<![\w.-]) lookbehind, positive
    "notes at " + _GP_HOME + "/example-user/notes.txt",
    # home-path: word character before /home, and the tool-dir lookahead
    "no /srv" + _GP_HOME + "/example-user or " + _GP_HOME + "/.cache/tool",
    # us-phone: (?<!\w) lookbehind, dashed and parenthesized positives
    "dial 012-345" + _GP_TAIL + " or (012) 345" + _GP_TAIL,
    # us-phone: underscore is a word character before the token
    "ref_012-345" + _GP_TAIL + " stays",
    # private-hostname: (?<![\w.-]) lookbehind, positive in prose
    "reach " + _GP_HOST + " for docs",
    # private-hostname: hyphen before, and a further dotted label after
    "no -" + _GP_HOST + " or " + _GP_HOST + ".example",
    # windows-home-path: matched quotes, (?P=windows_quote) bounds a spaced path
    'open "' + _GP_WIN + '\\Example User\\My Docs\\a b.txt" now',
    # windows-home-path: unquoted branch of the (?(windows_quote)...) conditional
    "open " + _GP_WIN + "\\example-user\\notes.txt now",
    # windows-home-path: mismatched quotes (observed: no finding, see feature doc)
    'open "' + _GP_WIN + "\\Example User\\a b.txt' now",
    # windows-home-path: word character before the drive letter
    "no X" + _GP_WIN + "\\example-user",
    # root-path: matched quotes, (?P=root_quote) bounds a spaced path
    "edit '" + _GP_ROOT + "/My Files/a b.txt' now",
    # root-path: unquoted branch
    "edit " + _GP_ROOT + "/.config/tool.json now",
    # root-path: mismatched quotes fall back to the unquoted branch
    'edit "' + _GP_ROOT + "/My Files/a b.txt' now",
    # root-path: word character before /root
    "no /srv" + _GP_ROOT + "/project",
)
GUARD_PARITY_FIXTURE = "\n".join(GUARD_PARITY_FIXTURE_LINES) + "\n"

# Every category redacts, so no finding blocks and every match is replaced in
# the redact output. The 555 rule carries a built-in warn override, so it is
# set explicitly.
GUARD_PARITY_POLICY = {
    "name": "verify-brigade-guard-parity",
    "defaults": {
        category: "redact"
        for category in ("infrastructure", "secret", "pii", "personal", "business", "attribution", "tooling")
    },
    "rules": {"example-phone-555": "redact"},
}

# Golden findings, pasted from `brigade guard scan --json` on main b18e6f51d:
# (line, column, start, end, rule_id, action). `start`/`end` are 0-based
# offsets into the fixture with `end` exclusive; `line`/`column` are 1-based.
# The drive also checks each reported `match` equals fixture[start:end].
GUARD_PARITY_GOLDEN_FINDINGS = (
    (2, 6, 49, 61, "example-phone-555", "redact"),
    (4, 10, 110, 119, "localhost-bare", "redact"),
    (6, 10, 166, 194, "home-path", "redact"),
    (8, 6, 247, 259, "us-phone", "redact"),
    (8, 22, 263, 277, "us-phone", "redact"),
    (10, 7, 307, 321, "private-hostname", "redact"),
    (12, 7, 382, 419, "windows-home-path", "redact"),
    (13, 6, 430, 461, "windows-home-path", "redact"),
    (16, 7, 539, 561, "root-path", "redact"),
    (17, 6, 572, 595, "root-path", "redact"),
    (18, 7, 606, 614, "root-path", "redact"),
)

# Golden redact output, pasted from `brigade guard redact` on main b18e6f51d.
# Lines the guard leaves alone reuse the fixture fragments so this file stays
# clean under content-guard.
GUARD_PARITY_GOLDEN_REDACTED = (
    "\n".join(
        (
            "guard parity fixture: synthetic values only",
            "desk <EXAMPLE_PHONE> ext",
            "id x555-010-0199 and 555-010-01990",
            "serve on [redacted-service] today",
            "no x" + _GP_LH + " or " + _GP_LH + "-dev",
            "notes at <PRIVATE_HOME_PATH>",
            "no /srv" + _GP_HOME + "/example-user or " + _GP_HOME + "/.cache/tool",
            "dial <PRIVATE_PHONE> or <PRIVATE_PHONE>",
            "ref_012-345" + _GP_TAIL + " stays",
            "reach [redacted-host] for docs",
            "no -" + _GP_HOST + " or " + _GP_HOST + ".example",
            'open "<PRIVATE_HOME_PATH>" now',
            "open <PRIVATE_HOME_PATH> now",
            'open "' + _GP_WIN + "\\Example User\\a b.txt' now",
            "no X" + _GP_WIN + "\\example-user",
            "edit '[redacted-root-path]' now",
            "edit [redacted-root-path] now",
            "edit \"[redacted-root-path] Files/a b.txt' now",
            "no /srv" + _GP_ROOT + "/project",
        )
    )
    + "\n"
)


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


def temp_base() -> Path:
    """The scratch tree, which must be an absolute path.

    `tempfile.gettempdir()` returns a relative `$TMPDIR` verbatim, and
    `resolve()` would anchor it to the working directory - the Brigade checkout
    in every documented invocation. A temp base that moves with the cwd is not
    a temp base, so refuse it instead of measuring anything against it.
    """
    raw = Path(tempfile.gettempdir())
    if not raw.is_absolute():
        raise HelperError(f"refusing a relative temp base {raw}: $TMPDIR must be an absolute path")
    return raw.resolve()


def guard_root(raw: str, *, flag: str, extra_allowed: tuple[Path, ...] = ()) -> Path:
    """Refuse a caller-supplied root that lands in the operator home or this checkout.

    Order matters. The home and checkout refusals run *before* the temp base
    allowance, so a `$TMPDIR` pointed at either one cannot launder a path back
    into them. The single carve-out is a temp base that sits inside the home
    directory and outside the checkout: `$TMPDIR` is the scratch tree by
    definition, so a path under it is allowed even though it is also under the
    home. Nothing excuses the checkout. The temp base itself is not a state
    root either: accepting it would chmod the shared scratch tree to `0700` and
    scatter `targets/`, `captures/`, and `state.json` across it.
    """
    resolved = Path(raw).expanduser().resolve()
    base = temp_base()
    home = Path.home().expanduser().resolve()
    if resolved == base:
        raise HelperError(f"refusing {flag} {resolved}: it is the temp base itself; use a directory under it")
    for allowed in extra_allowed:
        if resolved.is_relative_to(allowed):
            return resolved
    under_base = resolved.is_relative_to(base)
    home_excused = under_base and base.is_relative_to(home) and not base.is_relative_to(REPO_ROOT)
    for forbidden, label in ((REPO_ROOT, "the Brigade checkout"), (home, "the operator home")):
        if resolved.is_relative_to(forbidden):
            if forbidden == home and home_excused:
                continue
            raise HelperError(f"refusing {flag} {resolved}: it is inside {label}")
        if forbidden.is_relative_to(resolved):
            raise HelperError(f"refusing {flag} {resolved}: it contains {label}")
    return resolved


def guard_label(label: str) -> str:
    """Refuse a `--label` that is path input rather than a name.

    The label becomes the last component of the evidence directory name, so a
    label carrying a separator or `..` would place the evidence - and its
    `mkdir(parents=True)` - anywhere the caller wants.
    """
    if any(bad in label for bad in ("/", "\\", "..")) or not LABEL_PATTERN.fullmatch(label):
        raise HelperError(f"refusing --label {label!r}: use a simple [A-Za-z0-9._-] name of 1-64 characters")
    return label


def guard_no_symlink(path: Path, *, flag: str) -> None:
    """Refuse a state-root path that is a symlink.

    `mkdir(exist_ok=True)` follows an existing symlink, so a link planted at
    the state root - or at one of its subdirectories - would redirect every
    drive, and `cleanup`'s removal, wherever it points. The temp base is
    world-writable on most hosts, which makes the *default* root as reachable
    a plant as an explicit `--root`.
    """
    if path.is_symlink():
        raise HelperError(f"refusing {flag} {path}: it is a symlink")


def prepare_state_root(root: Path, *, flag: str) -> Path:
    """Create the state root and its subdirectories without following a symlink."""
    guard_no_symlink(root, flag=flag)
    try:
        root.mkdir(parents=True, exist_ok=True)
        # A link planted between the check and the mkdir would have been
        # followed silently; the created path must still resolve to itself.
        guard_no_symlink(root, flag=flag)
        if root.resolve() != root:
            raise HelperError(f"refusing {flag} {root}: it resolves to {root.resolve()}")
        root.chmod(0o700)
        for name in ("captures", "targets"):
            sub = root / name
            guard_no_symlink(sub, flag=f"{flag} subdirectory")
            sub.mkdir(exist_ok=True)
            guard_no_symlink(sub, flag=f"{flag} subdirectory")
            if sub.resolve() != sub:
                raise HelperError(f"refusing {flag} subdirectory {sub}: it resolves to {sub.resolve()}")
            sub.chmod(0o700)
    except OSError as exc:  # PermissionError included: report it, never traceback
        raise HelperError(f"cannot prepare {flag} {root}: {exc}") from exc
    return root


def evidence_root(args: argparse.Namespace) -> Path:
    """Resolve and guard the evidence root - default and `--evidence-root` alike.

    Same treatment as the state root: the symlink check runs on the unresolved
    path, because `resolve()` follows a link planted at
    `<checkout>/.brigade/verification-evidence` as happily as one planted in a
    world-writable temp dir.
    """
    raw = args.evidence_root or str(DEFAULT_EVIDENCE_ROOT)
    flag = "--evidence-root" if args.evidence_root else "the default evidence root"
    guard_no_symlink(Path(raw).expanduser(), flag=flag)
    return guard_root(raw, flag=flag, extra_allowed=(DEFAULT_EVIDENCE_ROOT,))


def prepare_evidence_root(base: Path, *, flag: str) -> Path:
    """Create the evidence root without following a symlink planted mid-flight."""
    guard_no_symlink(base, flag=flag)
    try:
        base.mkdir(parents=True, exist_ok=True)
        guard_no_symlink(base, flag=flag)
        if base.resolve() != base:
            raise HelperError(f"refusing {flag} {base}: it resolves to {base.resolve()}")
    except OSError as exc:  # PermissionError included: report it, never traceback
        raise HelperError(f"cannot prepare {flag} {base}: {exc}") from exc
    return base


def state_root(args: argparse.Namespace) -> Path:
    """Resolve, guard, and create the state root - default and `--root` alike."""
    raw = args.root or str(temp_base() / "verify-brigade")
    flag = "--root" if args.root else "the default state root"
    # The symlink check runs on the unresolved path: `resolve()` follows a
    # planted link, so the guard has to look before it resolves.
    guard_no_symlink(Path(raw).expanduser(), flag=flag)
    return prepare_state_root(guard_root(raw, flag=flag), flag=flag)


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
    tmp.chmod(0o600)
    tmp.replace(path)


def record_target(root: Path, target: Path) -> None:
    """Record a created target before anything else can fail on it."""
    state = read_state(root)
    state["created_targets"] = sorted(set(state["created_targets"]) | {str(target)})
    write_state(root, state)


def guard_target(path: Path, root: Path) -> Path:
    """Accept a target only when it lives under `<state-root>/targets`.

    Everything else is refused, which is what keeps a drive off the operator
    home, off this checkout, and off any path the helper did not make.
    """
    resolved = Path(path).expanduser().resolve()
    targets = (root / "targets").resolve()
    if resolved != targets and resolved.is_relative_to(targets):
        return resolved
    if resolved.is_relative_to(REPO_ROOT):
        detail = "it is inside the Brigade checkout"
    elif resolved.is_relative_to(Path.home().resolve()):
        detail = "it is inside the operator home"
    else:
        detail = "it is outside the state root"
    raise HelperError(f"refusing to use {resolved} as a target: {detail}; targets must live under {targets}")


def record_capture(root: Path, name: str, record: dict) -> Path:
    path = root / "captures" / f"{now_stamp()}-{uuid.uuid4().hex[:6]}-{name}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def run(
    argv: list[str],
    *,
    root: Path,
    name: str,
    env_extra: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    stdin_text: str | None = None,
) -> dict:
    """Run one command, capture it to the state root, and return the record.

    `stdin_text` is fed to the command; without it stdin is inherited, as before.
    """
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
            input=stdin_text,
        )
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

    git = shutil.which("git")
    if not git:
        raise HelperError("git is required to create a target")

    if args.dry_run:
        return emit(
            {
                "action": "new-target",
                "ok": True,
                "dry_run": True,
                "state_root": str(root),
                "would_create_under": str(root / "targets"),
                "would_run": [
                    [git, "init", "-q", "-b", "main", "<target>"],
                    brigade + ["init", "--target", "<target>", "--depth", args.depth, "--harnesses", args.harnesses],
                ],
            }
        )

    target = Path(tempfile.mkdtemp(prefix=f"{now_stamp()}-", dir=root / "targets"))
    # Record before driving anything: a target the helper made must stay
    # reclaimable by `cleanup`, including when the drive below fails.
    record_target(root, target)

    git_record = run([git, "init", "-q", "-b", "main", str(target)], root=root, name="new-target-git", timeout=60)
    if git_record["returncode"] != 0:
        shutil.rmtree(target, ignore_errors=True)
        return emit(
            {"action": "new-target", "ok": False, "stage": "git-init", "target": str(target), "git": slim(git_record)},
            1,
        )

    init_record = run(
        brigade + ["init", "--target", str(target), "--depth", args.depth, "--harnesses", args.harnesses],
        root=root,
        name="new-target-init",
        timeout=args.timeout,
    )
    if init_record["returncode"] != 0:
        shutil.rmtree(target, ignore_errors=True)
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

    return emit(
        {
            "action": "new-target",
            "ok": True,
            "dry_run": False,
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
    target = guard_target(Path(args.target), root)
    if not target.is_dir():
        raise HelperError(f"target is not a directory: {target}")
    argv = brigade_args(args) + [
        "init",
        "--target",
        str(target),
        "--depth",
        args.depth,
        "--harnesses",
        args.harnesses,
    ]
    if args.dry_run:
        return emit(
            {
                "action": "init",
                "ok": True,
                "dry_run": True,
                "target": str(target),
                "depth": args.depth,
                "harnesses": args.harnesses,
                "would_run": argv,
            }
        )
    record = run(argv, root=root, name="init", timeout=args.timeout)
    ok = record["returncode"] == 0
    return emit(
        {
            "action": "init",
            "ok": ok,
            "dry_run": False,
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
    target = guard_target(Path(args.target), root)
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
    target = guard_target(Path(args.target), root)
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
    if args.dry_run:
        return emit(
            {
                "action": "work-verify",
                "ok": True,
                "dry_run": True,
                "target": str(target),
                "command": args.command,
                "would_run": argv,
            }
        )
    record = run(argv, root=root, name="work-verify", timeout=args.timeout)
    payload = record["json"] if isinstance(record["json"], dict) else {}
    status = payload.get("status")
    ok = record["returncode"] == 0 and status == "completed"
    return emit(
        {
            "action": "work-verify",
            "ok": ok,
            "dry_run": False,
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
    target = guard_target(Path(args.target), root)
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
    target = guard_target(Path(args.target), root)
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
    # A failing `endpoint` check is the expected reading with no listener up, so
    # it does not sink the drive. Any other failing check does: a broken pack
    # config must not read green at the top level.
    failed = sorted(name for name, status in by_name.items() if name != "endpoint" and status == "fail")
    ok = bool(checks) and not failed
    return emit(
        {
            "action": "grokbot-pack-doctor",
            "ok": ok,
            "target": str(target),
            "pack_id": args.id,
            "checks": checks,
            "failed_checks": failed,
            "config_resolved": by_name.get("config") == "ok",
            "listener_running": by_name.get("endpoint") == "ok",
            "doctor": slim(record),
        },
        0 if ok else 3,
    )


def cmd_pack_canary(args: argparse.Namespace) -> int:
    """Bounded non-mutating auth + inventory check against the pack listener."""
    root = state_root(args)
    target = guard_target(Path(args.target), root)
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
    target = guard_target(Path(args.target), root)
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
    target = guard_target(Path(args.target), root)
    if args.sample:
        manifest = root / "sample-feed-manifest.json"
        if not args.dry_run:
            manifest.write_text(json.dumps(FEED_SAMPLE, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            manifest.chmod(0o600)
    else:
        if not args.manifest:
            raise HelperError("pass --manifest PATH or --sample")
        manifest = Path(args.manifest).expanduser().resolve()
    argv = brigade_args(args) + [
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
    ]
    if args.dry_run:
        return emit(
            {
                "action": "grokbot-feed",
                "ok": True,
                "dry_run": True,
                "target": str(target),
                "manifest": str(manifest),
                "sample": bool(args.sample),
                "would_write_manifest": bool(args.sample),
                "would_run": argv,
            }
        )
    record = run(argv, root=root, name="feed", timeout=args.timeout)
    payload = record["json"] if isinstance(record["json"], dict) else {}
    ok = record["returncode"] == 0
    return emit(
        {
            "action": "grokbot-feed",
            "ok": ok,
            "dry_run": False,
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
    target = guard_target(Path(args.target), root)
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
    target = guard_target(Path(args.target), root)
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


def guard_parity_findings(payload: dict) -> tuple[list[list], list[dict]]:
    """Normalize `guard scan --json` findings; also return any whose match disagrees with its offsets."""
    findings = payload.get("findings") if isinstance(payload.get("findings"), list) else []
    observed: list[list] = []
    inconsistent: list[dict] = []
    for finding in findings:
        if not isinstance(finding, dict):
            inconsistent.append({"finding": finding, "reason": "not an object"})
            continue
        row = [finding.get(key) for key in ("line", "column", "start", "end", "rule_id", "action")]
        observed.append(row)
        start, end = finding.get("start"), finding.get("end")
        sliced = GUARD_PARITY_FIXTURE[start:end] if isinstance(start, int) and isinstance(end, int) else None
        if finding.get("match") != sliced:
            inconsistent.append({"finding": row, "reason": "match is not fixture[start:end]"})
    return observed, inconsistent


def guard_parity_workdir(target: Path) -> Path:
    """Create `<target>/guard-parity`, refusing any path that could redirect a write.

    `guard_target` already resolved the target itself, so only the work
    directory and the three files the drive writes are left to check.
    """
    work = target / "guard-parity"
    if work.is_symlink() or (work.exists() and not work.is_dir()):
        raise HelperError(f"refusing to write through {work}: it is a symlink or not a directory")
    work.mkdir(exist_ok=True)
    for name in ("fixture.txt", "policy.json", "in-place.txt"):
        path = work / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise HelperError(f"refusing to write through {path}: it is a symlink or not a regular file")
    return work


def cmd_guard_parity(args: argparse.Namespace) -> int:
    """Scan and redact the bundled lookbehind/backreference fixture; compare to the goldens."""
    root = state_root(args)
    target = guard_target(Path(args.target), root)
    if not target.is_dir():
        raise HelperError(f"target {target} is not an existing directory; create one with new-target first")
    work = target / "guard-parity"
    fixture, policy, in_place = work / "fixture.txt", work / "policy.json", work / "in-place.txt"
    brigade = brigade_args(args)
    scan_argv = brigade + ["guard", "scan", "--policy", str(policy), "--json", str(fixture)]
    redact_argv = brigade + ["guard", "redact", "--policy", str(policy), str(fixture)]
    stdin_argv = brigade + ["guard", "redact", "--policy", str(policy)]
    in_place_argv = brigade + ["guard", "redact", "--policy", str(policy), "--in-place", str(in_place)]
    if args.dry_run:
        return emit(
            {
                "action": "guard-parity",
                "ok": True,
                "dry_run": True,
                "target": str(target),
                "would_write": [str(fixture), str(policy), str(in_place)],
                "would_run": [scan_argv, redact_argv, stdin_argv, in_place_argv],
            }
        )
    guard_parity_workdir(target)
    fixture.write_bytes(GUARD_PARITY_FIXTURE.encode("utf-8"))
    in_place.write_bytes(GUARD_PARITY_FIXTURE.encode("utf-8"))
    policy.write_text(json.dumps(GUARD_PARITY_POLICY, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    scan = run(scan_argv, root=root, name="guard-scan", timeout=args.timeout)
    redact = run(redact_argv, root=root, name="guard-redact", timeout=args.timeout)
    stdin = run(stdin_argv, root=root, name="guard-redact-stdin", timeout=args.timeout, stdin_text=GUARD_PARITY_FIXTURE)
    rewrite = run(in_place_argv, root=root, name="guard-redact-in-place", timeout=args.timeout)

    # A timed-out record carries no "json" key; its exit code is reported below.
    payload = scan.get("json") if isinstance(scan.get("json"), dict) else {}
    observed, inconsistent = guard_parity_findings(payload)
    golden = [list(row) for row in GUARD_PARITY_GOLDEN_FINDINGS]
    mismatches: list[dict] = []
    for name, record in (("scan", scan), ("redact", redact), ("redact-stdin", stdin), ("redact-in-place", rewrite)):
        if record["returncode"] != 0:
            mismatches.append({"check": f"{name} exit code", "expected": 0, "observed": record["returncode"]})
    if observed != golden:
        mismatches.append(
            {
                "check": "scan findings",
                "missing": [row for row in golden if row not in observed],
                "unexpected": [row for row in observed if row not in golden],
            }
        )
    mismatches += [{"check": "scan match", **item} for item in inconsistent]
    for name, text in (("redact stdout", redact["stdout"]), ("redact stdin stdout", stdin["stdout"])):
        if text != GUARD_PARITY_GOLDEN_REDACTED:
            mismatches.append({"check": name, "expected": GUARD_PARITY_GOLDEN_REDACTED, "observed": text})
    # stdout is decoded text, so the on-disk rewrite is the byte-for-byte check.
    # `--in-place` writes with Path.write_text, which emits the platform newline
    # (CRLF on Windows), so the expected bytes follow os.linesep.
    written = in_place.read_bytes()
    if written != GUARD_PARITY_GOLDEN_REDACTED.replace("\n", os.linesep).encode("utf-8"):
        mismatches.append(
            {
                "check": "redact --in-place bytes",
                "expected": GUARD_PARITY_GOLDEN_REDACTED,
                "observed": written.decode("utf-8", "replace"),
            }
        )
    ok = not mismatches
    return emit(
        {
            "action": "guard-parity",
            "ok": ok,
            "dry_run": False,
            "target": str(target),
            "fixture": str(fixture),
            "policy": str(policy),
            "rule_ids": sorted({row[4] for row in observed if isinstance(row[4], str)}),
            "findings": observed,
            "redacted": redact["stdout"],
            "mismatches": mismatches,
            "scan": slim(scan),
            "redact": slim(redact),
            "redact_stdin": slim(stdin),
            "redact_in_place": slim(rewrite),
        },
        0 if ok else 3,
    )


# ------------------------------------------------------------------ audit
#
# `audit` compares the feature map (features/*.md headers plus the known-gap
# list in features/MAINTENANCE.md) against the live CLI command tree. It is
# read-only: it runs `brigade completions bash`, `brigade --version`, and
# `brigade <surface> --help`, reads the map, and writes nothing anywhere.

FEATURE_STATUSES = ("active", "paused", "unreachable")
FEATURE_PROOFS = ("drive", "stand-in")
FEATURE_KEYS = frozenset({"status", "proof", "surfaces", "drive", "last-drive", "reason", "unpause"})
MAP_DOCS = frozenset({"README.md", "MAINTENANCE.md"})
DEFAULT_MAX_AGE_DAYS = 45
LAST_DRIVE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2}) ([0-9a-f]{7,40})")
UNPAUSE_PATTERN = re.compile(r"#\d+|https://\S+|pending: \S.*")
SURFACE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*( [a-z0-9][a-z0-9-]*)*")
TREE_LINE = re.compile(r'^\s*\["brigade(?: ([^"]*))?"\]="([^"]*)"\s*$')
FLAG_PATTERN = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
GAP_FENCE = "```audit-gaps"


def feature_header(text: str) -> dict[str, str] | None:
    """The `key: value` block between a leading `---` line and the next one."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    header: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return header
        key, sep, value = line.partition(":")
        if not sep:
            header.setdefault("__malformed__", line)
            continue
        header[key.strip()] = value.strip()
    return None


def split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def helper_subcommands() -> set[str]:
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def load_feature(path: Path, drives: set[str], today: datetime) -> tuple[dict, list[str]]:
    """Parse and validate one feature file. Returns (feature, errors)."""
    name = path.name
    text = path.read_text(encoding="utf-8")
    header = feature_header(text)
    if header is None:
        return {"file": name}, [f"{name}: missing the --- status header"]
    errors: list[str] = []
    if "__malformed__" in header:
        errors.append(f"{name}: header line is not `key: value`: {header.pop('__malformed__')!r}")
    for key in sorted(set(header) - FEATURE_KEYS):
        errors.append(f"{name}: unknown header key {key!r}")
    status = header.get("status", "")
    if status not in FEATURE_STATUSES:
        errors.append(f"{name}: status must be one of {', '.join(FEATURE_STATUSES)}, got {status!r}")
    proof = header.get("proof", "")
    if proof not in FEATURE_PROOFS:
        errors.append(f"{name}: proof must be one of {', '.join(FEATURE_PROOFS)}, got {proof!r}")
    surfaces = split_list(header.get("surfaces", ""))
    if not surfaces:
        errors.append(f"{name}: surfaces must name at least one CLI command path")
    for surface in surfaces:
        if not SURFACE_PATTERN.fullmatch(surface):
            errors.append(f"{name}: surface {surface!r} is not a command path like `work verify run`")
    drive = split_list(header.get("drive", ""))
    if status == "active" and proof == "drive" and not drive:
        errors.append(f"{name}: an active drive needs at least one helper subcommand in `drive`")
    for step in drive:
        if step not in drives:
            errors.append(f"{name}: drive step {step!r} is not a control-brigade.py subcommand")
    reason = header.get("reason", "")
    if status in ("paused", "unreachable") and not reason:
        errors.append(f"{name}: a {status} feature needs a reason")
    unpause = header.get("unpause", "")
    if status == "paused" and not UNPAUSE_PATTERN.fullmatch(unpause):
        errors.append(f"{name}: a paused feature needs `unpause: #<issue>`, a URL, or `pending: <what unblocks it>`")
    if status != "paused" and unpause:
        errors.append(f"{name}: only a paused feature carries `unpause`")
    last_drive = None
    raw_last = header.get("last-drive", "")
    if raw_last:
        match = LAST_DRIVE_PATTERN.fullmatch(raw_last)
        if not match:
            errors.append(f"{name}: last-drive must be `YYYY-MM-DD <commit>`, got {raw_last!r}")
        else:
            try:
                when = datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                errors.append(f"{name}: last-drive date {match.group(1)!r} is not a date")
            else:
                last_drive = {
                    "date": match.group(1),
                    "commit": match.group(2),
                    "age_days": (today - when).days,
                }
    feature = {
        "file": name,
        "status": status,
        "proof": proof,
        "surfaces": surfaces,
        "drive": drive,
        "last_drive": last_drive,
        "reason": reason or None,
        "unpause": unpause or None,
        "text": text,
    }
    return feature, errors


def load_gaps(path: Path) -> tuple[list[dict], list[str]]:
    """Known gaps: lines `<command path>[ *]  <note>` in an audit-gaps fence."""
    if not path.is_file():
        return [], [f"{path.name}: missing (the known-gap list lives there)"]
    gaps: list[dict] = []
    errors: list[str] = []
    inside = False
    found = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped == GAP_FENCE:
            inside = found = True
            continue
        if inside and stripped == "```":
            inside = False
            continue
        if not inside or not stripped:
            continue
        surface, _, note = stripped.partition("  ")
        surface = surface.strip()
        wildcard = surface.endswith(" *")
        base = surface[:-2] if wildcard else surface
        if not SURFACE_PATTERN.fullmatch(base) or not note.strip():
            errors.append(f"{path.name}:{number}: gap line must be `<command path>[ *]  <note>`")
            continue
        gaps.append({"surface": base, "wildcard": wildcard, "note": note.strip()})
    if not found:
        errors.append(f"{path.name}: no {GAP_FENCE} block")
    return gaps, errors


def command_tree(brigade: list[str], timeout: int) -> dict[str, list[str]]:
    """The live CLI tree from `brigade completions bash`: path -> child commands."""
    try:
        proc = subprocess.run(
            brigade + ["completions", "bash"], capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise HelperError(f"command not found: {brigade[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HelperError("brigade completions bash timed out") from exc
    if proc.returncode != 0:
        raise HelperError(f"brigade completions bash exited {proc.returncode}: {proc.stderr.strip()[-300:]}")
    tree: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        match = TREE_LINE.match(line)
        if match:
            tree[match.group(1) or ""] = match.group(2).split()
    if not tree.get(""):
        raise HelperError("brigade completions bash printed no command tree")
    return tree


HELP_CHILD = re.compile(r"^  ([a-z0-9][a-z0-9-]*)\s{2,}\S")


def help_children(brigade: list[str], family: str, timeout: int) -> list[str]:
    """Child commands listed under `commands:` in `brigade <family> --help`.

    Families such as `guard` hand their arguments to their own parser, so the
    completion tree stops at the family name. Their `--help` still lists the
    verbs, one per line, two spaces in.
    """
    try:
        proc = subprocess.run(
            brigade + family.split() + ["--help"], capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return []
    children: list[str] = []
    inside = False
    for line in proc.stdout.splitlines():
        if line.strip() == "commands:":
            inside = True
            continue
        if inside:
            match = HELP_CHILD.match(line)
            if match:
                children.append(match.group(1))
            elif not line.strip() and children:
                break
    return children


def in_tree(tree: dict[str, list[str]], surface: str) -> bool:
    parent, _, leaf = surface.rpartition(" ")
    return leaf in tree.get(parent, [])


def surface_help(brigade: list[str], surface: str, timeout: int) -> dict:
    try:
        proc = subprocess.run(
            brigade + surface.split() + ["--help"], capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return {"surface": surface, "returncode": None, "flags": [], "detail": "--help timed out"}
    # Only the usage block: it lists every option once, while the prose below
    # it can quote flags that belong to some other command.
    usage = proc.stdout.split("\n\n", 1)[0]
    flags = sorted(set(FLAG_PATTERN.findall(usage)) - {"--help"})
    detail = "" if proc.returncode == 0 else (proc.stderr or proc.stdout).strip()[-300:]
    return {"surface": surface, "returncode": proc.returncode, "flags": flags, "detail": detail}


def cmd_audit(args: argparse.Namespace) -> int:
    """Read-only: does the feature map still match the live CLI?"""
    features_dir = (
        Path(args.features_dir).resolve() if args.features_dir else Path(__file__).resolve().parent / "features"
    )
    if not features_dir.is_dir():
        raise HelperError(f"features directory not found: {features_dir}")
    brigade = brigade_args(args)
    today = datetime.now(timezone.utc)
    drives = helper_subcommands()

    errors: list[str] = []
    features: list[dict] = []
    for path in sorted(features_dir.glob("*.md")):
        if path.name in MAP_DOCS:
            continue
        feature, problems = load_feature(path, drives, today)
        features.append(feature)
        errors.extend(problems)
    gaps, problems = load_gaps(features_dir / "MAINTENANCE.md")
    errors.extend(problems)

    tree = command_tree(brigade, args.timeout)
    version = subprocess.run(brigade + ["--version"], capture_output=True, text=True, timeout=args.timeout, check=False)

    valid = [f for f in features if f.get("status") in FEATURE_STATUSES]

    # Fill in families the completion tree cannot see into, but only along
    # paths a feature walks: that keeps the audit to a few `--help` calls.
    help_families: list[str] = []
    for surface in sorted({s for f in valid for s in f["surfaces"]}):
        words = surface.split()
        for depth in range(1, len(words)):
            family = " ".join(words[:depth])
            if family not in tree and in_tree(tree, family):
                children = help_children(brigade, family, args.timeout)
                if children:
                    tree[family] = children
                    help_families.append(family)

    active = [f for f in valid if f["status"] == "active"]
    paused = [f for f in valid if f["status"] == "paused"]
    unreachable = [f for f in valid if f["status"] == "unreachable"]

    # Coverage. A stand-in proof covers nothing: it is listed, never counted.
    covered = {s for f in valid if f["proof"] == "drive" for s in f["surfaces"] if in_tree(tree, s)}
    touched = set(covered)
    walked = {""}
    for surface in covered:
        words = surface.split()
        for depth in range(1, len(words) + 1):
            prefix = " ".join(words[:depth])
            touched.add(prefix)
            walked.add(prefix)
    uncovered_paths = sorted(
        {
            f"{parent} {child}".strip()
            for parent in walked
            for child in tree.get(parent, [])
            if f"{parent} {child}".strip() not in touched
        }
    )

    def gap_for(surface: str) -> dict | None:
        for gap in gaps:
            if not gap["wildcard"] and gap["surface"] == surface:
                return gap
        matches = [gap for gap in gaps if gap["wildcard"] and surface.startswith(gap["surface"] + " ")]
        return max(matches, key=lambda gap: len(gap["surface"]), default=None)

    uncovered = []
    used_gaps: set[int] = set()
    for surface in uncovered_paths:
        gap = gap_for(surface)
        if gap is not None:
            used_gaps.add(id(gap))
        uncovered.append({"surface": surface, "known_gap": gap is not None, "note": gap["note"] if gap else None})
    stale_gaps = []
    for gap in gaps:
        if id(gap) in used_gaps:
            continue
        label = gap["surface"] + (" *" if gap["wildcard"] else "")
        if not in_tree(tree, gap["surface"]):
            why = "not in the CLI command tree"
        elif gap["surface"] in touched and not gap["wildcard"]:
            why = "a feature covers it now"
        else:
            why = "matches no uncovered surface"
        stale_gaps.append({"gap": label, "reason": why})

    # Active features: every surface must still exist and answer --help.
    checks = [(f, s) for f in active for s in f["surfaces"]]
    broken: list[dict] = []
    uncovered_flags: list[dict] = []
    known = sorted({surface for _, surface in checks if in_tree(tree, surface)})
    with ThreadPoolExecutor(max_workers=8) as pool:
        helps = dict(
            zip(known, pool.map(lambda surface: surface_help(brigade, surface, args.timeout), known), strict=True)
        )
    for feature, surface in checks:
        if surface not in helps:
            broken.append({"feature": feature["file"], "surface": surface, "detail": "not in the CLI command tree"})
            continue
        result = helps[surface]
        if result["returncode"] != 0:
            broken.append(
                {"feature": feature["file"], "surface": surface, "detail": result["detail"] or "--help failed"}
            )
            continue
        missing = [flag for flag in result["flags"] if flag not in feature["text"]]
        if missing:
            uncovered_flags.append({"feature": feature["file"], "surface": surface, "flags": missing})

    stale = []
    for feature in active:
        last = feature["last_drive"]
        if last is None:
            stale.append({"feature": feature["file"], "last_drive": None, "reason": "no drive recorded"})
        elif last["age_days"] > args.max_age_days:
            stale.append(
                {"feature": feature["file"], "last_drive": last, "reason": f"older than {args.max_age_days} days"}
            )

    def listing(feature: dict) -> dict:
        return {
            "feature": feature["file"],
            "surfaces": feature["surfaces"],
            "reason": feature["reason"],
            **({"unpause": feature["unpause"]} if feature["status"] == "paused" else {}),
        }

    undocumented = [item for item in uncovered if not item["known_gap"]]
    failures = (
        [f"map error: {e}" for e in errors]
        + [f"broken surface: {b['feature']} -> {b['surface']} ({b['detail']})" for b in broken]
        + [f"uncovered surface with no feature and no known gap: {u['surface']}" for u in undocumented]
        + [f"stale known gap: {g['gap']} ({g['reason']})" for g in stale_gaps]
    )
    ok = not failures
    return emit(
        {
            "action": "audit",
            "ok": ok,
            "cli": {
                "brigade": brigade,
                "version": (version.stdout or version.stderr).strip(),
                "commands": len(tree.get("", [])),
                "help_families": help_families,
            },
            "features_dir": str(features_dir),
            "features": [
                {key: f.get(key) for key in ("file", "status", "proof", "surfaces", "drive", "last_drive")}
                for f in features
            ],
            "paused": [listing(f) for f in paused],
            "unreachable": [listing(f) for f in unreachable],
            "stand_ins": [f["file"] for f in valid if f["proof"] == "stand-in"],
            "uncovered": uncovered,
            "uncovered_flags": uncovered_flags,
            "stale": stale,
            "stale_gaps": stale_gaps,
            "broken": broken,
            "errors": errors,
            "failures": failures,
            "summary": {
                "features": len(features),
                "active": len(active),
                "paused": len(paused),
                "unreachable": len(unreachable),
                "uncovered": len(uncovered),
                "uncovered_known_gaps": len(uncovered) - len(undocumented),
                "uncovered_undocumented": len(undocumented),
                "uncovered_flags": sum(len(item["flags"]) for item in uncovered_flags),
                "stale": len(stale),
                "broken": len(broken),
                "errors": len(errors),
            },
        },
        0 if ok else 3,
    )


def cmd_evidence(args: argparse.Namespace) -> int:
    """Copy captures and receipts somewhere cleanup never looks."""
    root = state_root(args)
    target = guard_target(Path(args.target), root) if args.target else None
    label = guard_label(args.label)
    flag = "--evidence-root" if args.evidence_root else "the default evidence root"
    base = evidence_root(args)
    evidence = base / f"{now_stamp()}-{label}"
    if not args.dry_run:
        prepare_evidence_root(base, flag=flag)
        (evidence / "captures").mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for capture in sorted((root / "captures").glob("*.json")):
        destination = evidence / "captures" / capture.name
        if not args.dry_run:
            shutil.copy2(capture, destination)
        copied.append(str(destination.relative_to(evidence)))

    if target is not None:
        for relative in ("work/verify-runs", "grokbot", "work/sessions"):
            source = target / ".brigade" / relative
            if not source.exists():
                continue
            destination = evidence / "target-brigade" / relative
            if not args.dry_run:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, destination, dirs_exist_ok=True)
            copied.append(str(destination.relative_to(evidence)))

    if args.dry_run:
        return emit(
            {
                "action": "evidence",
                "ok": True,
                "dry_run": True,
                "evidence_dir": str(evidence),
                "would_copy": copied,
            }
        )

    manifest = {
        "schema": "verify-brigade.evidence.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "evidence_dir": str(evidence),
        "state_root": str(root),
        "target": str(target) if target else None,
        "items": copied,
    }
    (evidence / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return emit({"action": "evidence", "ok": True, "dry_run": False, "evidence_dir": str(evidence), "items": copied})


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
    # A target that a failed drive already deleted is still reclaimed: drop it
    # from state.json so it stops being reported on every later cleanup.
    reclaimed: set[str] = set()
    for entry in recorded:
        if not entry.is_relative_to(root / "targets"):
            skipped.append({"path": str(entry), "reason": "outside the state root"})
            continue
        # Inspect the recorded path without following a replacement symlink.
        # Only FileNotFoundError proves it is absent; other errors retain it.
        try:
            mode = entry.lstat().st_mode
        except FileNotFoundError:
            skipped.append({"path": str(entry), "reason": "already gone"})
            reclaimed.add(str(entry))
            continue
        except OSError:
            skipped.append({"path": str(entry), "reason": "removal-not-confirmed"})
            continue
        if stat.S_ISLNK(mode):
            skipped.append({"path": str(entry), "reason": "skipped-symlink"})
            continue
        if not stat.S_ISDIR(mode):
            skipped.append({"path": str(entry), "reason": "skipped-not-directory"})
            continue
        if args.dry_run:
            removed.append(str(entry))
            continue
        shutil.rmtree(entry, ignore_errors=True)
        try:
            entry.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            skipped.append({"path": str(entry), "reason": "removal-not-confirmed"})
            continue
        else:
            skipped.append({"path": str(entry), "reason": "removal-not-confirmed"})
            continue
        removed.append(str(entry))
        reclaimed.add(str(entry))

    captures = sorted((root / "captures").glob("*.json"))
    if not args.dry_run:
        if not args.keep_captures:
            for capture in captures:
                capture.unlink(missing_ok=True)
        state["created_targets"] = sorted(set(state["created_targets"]) - reclaimed)
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
    # `subcommand`, not `command`: `work-verify --command` would otherwise
    # overwrite the name of the subcommand in the failure envelope.
    sub = parser.add_subparsers(dest="subcommand", metavar="<command>")
    sub.required = True

    def add_target(command: argparse.ArgumentParser) -> None:
        command.add_argument("--target", required=True, help="Temp target created by new-target.")

    def add_dry_run(command: argparse.ArgumentParser, help: str) -> None:
        command.add_argument("--dry-run", action="store_true", help=help)

    def add_pack(command: argparse.ArgumentParser) -> None:
        add_target(command)
        command.add_argument("--id", required=True, help="Connector pack id, e.g. implementation-worker.")
        command.add_argument("--bearer-env", default=BEARER_ENV_NAME, help="Env var holding the pack bearer.")

    p = sub.add_parser("doctor", help="Read-only: is this rig worth driving?")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("new-target", help="git init + brigade init in a fresh temp dir.")
    p.add_argument("--depth", default=DEFAULT_DEPTH, choices=["repo", "workspace"])
    p.add_argument("--harnesses", default=DEFAULT_HARNESSES)
    add_dry_run(p, "Report the commands without creating a target.")
    p.set_defaults(func=cmd_new_target)

    p = sub.add_parser("init", help="Run brigade init against an existing directory.")
    add_target(p)
    p.add_argument("--depth", default=DEFAULT_DEPTH, choices=["repo", "workspace"])
    p.add_argument("--harnesses", default=DEFAULT_HARNESSES)
    add_dry_run(p, "Report the command without writing into the target.")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor-target", help="brigade doctor --json against the temp target.")
    add_target(p)
    p.set_defaults(func=cmd_doctor_target)

    p = sub.add_parser("work-verify", help="Bounded brigade work verify run; returns receipt id and status.")
    add_target(p)
    p.add_argument("--command", default=DEFAULT_PROBE, help="Verification command to run in the target.")
    p.add_argument("--capture", default=None, help="Artifact id for atomic --capture.")
    p.add_argument("--command-timeout", type=int, default=60, help="Per-command timeout inside Brigade.")
    add_dry_run(p, "Report the command without running a verification.")
    p.set_defaults(func=cmd_work_verify)

    p = sub.add_parser("grokbot-pack-setup", help="Preview or apply pack instance config.")
    add_pack(p)
    p.add_argument("--apply", action="store_true", help="Write config. Default is preview only.")
    add_dry_run(p, "Force preview even with --apply.")
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
    add_dry_run(p, "Force preview even with --apply.")
    p.set_defaults(func=cmd_pack_remove)

    p = sub.add_parser("grokbot-feed", help="Validate an approved feed manifest (never enqueues).")
    add_target(p)
    p.add_argument("--manifest", default=None, help="Path to an approved private feed manifest.")
    p.add_argument("--sample", action="store_true", help="Write and validate a bundled sample manifest.")
    p.add_argument("--limit", type=int, default=1)
    add_dry_run(p, "Report the command without writing the sample manifest.")
    p.set_defaults(func=cmd_feed)

    p = sub.add_parser("grokbot-status", help="Safe Grok Bot job projections.")
    add_target(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("handoff-lint", help="Lint the handoff inbox of the temp target.")
    add_target(p)
    p.add_argument("--content-guard", action="store_true", help="Add the leak and injection scan.")
    p.add_argument("paths", nargs="*", help="Explicit handoff files. Defaults to the pending inbox.")
    p.set_defaults(func=cmd_handoff_lint)

    p = sub.add_parser("guard-parity", help="Scan and redact the guard parity fixture against the goldens.")
    add_target(p)
    add_dry_run(p, "Report the commands without writing the fixture.")
    p.set_defaults(func=cmd_guard_parity)

    p = sub.add_parser("audit", help="Read-only: compare the feature map against the live CLI command tree.")
    p.add_argument("--features-dir", default=None, help="Feature map to audit (default: this skill's features/).")
    p.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help="Report an active feature as stale when its last drive is older than this.",
    )
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("evidence", help="Copy captures and receipts to a named evidence dir.")
    p.add_argument("--target", default=None, help="Temp target whose receipts to copy.")
    p.add_argument(
        "--label",
        default="run",
        help="Suffix for the evidence directory name ([A-Za-z0-9._-], 1-64 characters).",
    )
    p.add_argument("--evidence-root", default=None, help="Where evidence dirs live.")
    add_dry_run(p, "Report what would be copied without creating the evidence dir.")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("cleanup", help="Remove only the targets this helper created.")
    p.add_argument("--target", default=None, help="Remove one recorded target instead of all of them.")
    add_dry_run(p, "Report what would be removed.")
    p.add_argument("--keep-captures", action="store_true", help="Leave capture files in the state root.")
    p.set_defaults(func=cmd_cleanup)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except HelperError as exc:
        return emit({"action": args.subcommand, "ok": False, "error": str(exc)}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
