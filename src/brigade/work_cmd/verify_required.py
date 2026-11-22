"""Plan the local tests required by a repository diff."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from time import monotonic
from typing import Any

from .. import localio, shell_quote, verify_manifest_git

CONFIG_PATH = "verify/required-tests.json"
CONFIG_SCHEMA = "brigade.verify_required_config.v1"
PLAN_SCHEMA = "brigade.verify_required.v1"
PLANNER_TIMEOUT_SECONDS = 60
_MAX_HINT_TESTS = 40
_MAX_HINT_CHARS = 4000
_PLANNER_PREFIX = "{planner_dir}/"
_PLANNER_TOKENS = {"{base}", "{head}", "{output}", "{planner_dir}", "{repo}", "{python}", "{head_dir}"}
_GIT_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_PLANNER_ENV = {"PATH", "HOME", "USERPROFILE", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP"}
_WINDOWS_PLANNER_ENV = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE"}


def _planner_env() -> dict[str, str]:
    """Pass only the OS settings needed by the planner and its Git commands."""
    if os.name == "nt":
        allowed = _PLANNER_ENV | _WINDOWS_PLANNER_ENV
        return {key.upper(): value for key, value in os.environ.items() if key.upper() in allowed}
    return {key: value for key, value in os.environ.items() if key in _PLANNER_ENV}


def _remaining(deadline: float | None, limit: float) -> float:
    remaining = limit if deadline is None else min(limit, deadline - monotonic())
    if remaining <= 0:
        raise ValueError("required-test planning timed out")
    return remaining


def _git(target: Path, *args: str, deadline: float | None = None) -> bytes:
    try:
        proc = subprocess.run(
            ["git", "--no-replace-objects", "--literal-pathspecs", *args],
            cwd=target,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_remaining(deadline, 30),
            env=_planner_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"git {args[0]} timed out") from exc
    if proc.returncode:
        raise ValueError(f"git {args[0]} failed (exit {proc.returncode})")
    return proc.stdout


def _commit_id(raw: bytes) -> str:
    value = raw.decode("ascii").strip()
    if not _GIT_OID.fullmatch(value):
        raise ValueError("Git did not return an object ID")
    return value


def _tree_entries(
    target: Path, commit: str, path: str | None = None, *, deadline: float | None = None
) -> dict[str, tuple[bytes, bytes]]:
    args = ("-r",) if path is None else ("--", path)
    raw = _git(target, "ls-tree", "-z", "--full-tree", commit, *args, deadline=deadline)
    entries = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        attrs, name = entry.split(b"\t", 1)
        mode, kind, oid = attrs.split()
        # Preserve byte-level identity even for unrelated paths that are not UTF-8.
        entries[name.decode("utf-8", "surrogateescape")] = (mode if kind == b"blob" else b"", oid)
    return entries


def _regular(mode: bytes) -> bool:
    return mode in {b"100644", b"100755"}


def _read_blob(target: Path, entry: tuple[bytes, bytes], path: str, deadline: float | None) -> bytes:
    mode, oid = entry
    if not _regular(mode):
        raise ValueError(f"{path} is not a regular file")
    return _git(target, "cat-file", "blob", _commit_id(oid), deadline=deadline)


def _repo_path(path: str) -> bool:
    return (
        not path.startswith("-")
        and not any(char in path for char in "\\:\0\r\n")
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


def _materialize_file(directory: Path, name: str, blob: bytes) -> Path:
    parts = name.split("/")
    if (
        not _repo_path(name)
        or any(part.casefold() == ".git" for part in parts)
        or (os.name == "nt" and any(part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved() for part in parts))
    ):
        raise ValueError(f"cannot materialize repository path {name!r}")
    path = directory.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also detects names that alias on a case-insensitive filesystem.
    with path.open("xb") as stream:
        stream.write(blob)
    return path


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a JSON object")
    return data


def _config_at(target: Path, base: str, deadline: float | None) -> dict[str, Any] | None:
    entry = _tree_entries(target, base, CONFIG_PATH, deadline=deadline).get(CONFIG_PATH)
    if entry is None:
        return None
    config = _json_object(_read_blob(target, entry, CONFIG_PATH, deadline), "required-tests config")
    if config.get("schema") != CONFIG_SCHEMA:
        raise ValueError("required-tests config has an unsupported schema")
    for key in ("planner_files", "planner_argv", "run_argv"):
        items = config.get(key)
        if not isinstance(items, list) or not items or not all(isinstance(item, str) and item for item in items):
            raise ValueError(f"required-tests config {key} must be a non-empty string array")
    files = config["planner_files"]
    if any(not _repo_path(path) for path in files) or len(files) != len(set(files)):
        raise ValueError("planner_files must list distinct repository-relative paths")
    for item in config["planner_argv"]:
        if item in _PLANNER_TOKENS:
            continue
        suffix = item.removeprefix(_PLANNER_PREFIX)
        if "{" in suffix or "}" in suffix or (suffix != item and not _repo_path(suffix)):
            raise ValueError("planner placeholders must be whole argv elements or start with {planner_dir}/")
    run = config["run_argv"]
    if run.count("{tests}") != 1 or any(("{" in item or "}" in item) and item != "{tests}" for item in run):
        raise ValueError("run_argv must contain one whole {tests} element")
    for key in ("mode_key", "selected_key"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError(f"required-tests config {key} must be a string")
    if "digest_key" in config and (not isinstance(config["digest_key"], str) or not config["digest_key"]):
        raise ValueError("required-tests config digest_key must be a string")
    return config


def _copy_planner(target: Path, base: str, files: list[str], directory: Path, deadline: float | None) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        entry = _tree_entries(target, base, name, deadline=deadline).get(name)
        if entry is None:
            raise ValueError(f"planner file is missing at the base: {name}")
        blob = _read_blob(target, entry, name, deadline)
        path = _materialize_file(directory, name, blob)
        if entry[0] == b"100755" and os.name != "nt":
            path.chmod(0o700)
        digest.update(name.encode("utf-8") + b"\0" + str(len(blob)).encode("ascii") + b"\0" + blob)
    return digest.hexdigest()


def _copy_head(
    target: Path, entries: dict[str, tuple[bytes, bytes]], directory: Path, deadline: float | None, *, snapshot: bool
) -> None:
    # Read raw blobs so checkout filters or archive attributes cannot alter or hide planner input.
    by_oid: dict[bytes, list[str]] = {}
    for name, (mode, oid) in entries.items():
        if not _regular(mode):
            if snapshot:
                continue
            raise ValueError(f"HEAD snapshot entry is not a regular file: {name!r}")
        by_oid.setdefault(_commit_id(oid).encode("ascii"), []).append(name)
    with tempfile.TemporaryFile() as blobs:
        try:
            proc = subprocess.run(
                ["git", "--no-replace-objects", "cat-file", "--batch"],
                cwd=target,
                input=b"".join(oid + b"\n" for oid in by_oid),
                stdout=blobs,
                stderr=subprocess.DEVNULL,
                timeout=_remaining(deadline, 30),
                env=_planner_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("git cat-file timed out copying HEAD") from exc
        if proc.returncode:
            raise ValueError(f"git cat-file failed copying HEAD (exit {proc.returncode})")
        blobs.seek(0)
        for oid, paths in by_oid.items():
            _remaining(deadline, 30)
            header = blobs.readline().split()
            if len(header) != 3 or header[:2] != [oid, b"blob"]:
                raise ValueError("HEAD snapshot blob is unreadable")
            size = int(header[2])
            data = blobs.read(size)
            if len(data) != size or blobs.read(1) != b"\n":
                raise ValueError("HEAD snapshot blob is incomplete")
            for name in paths:
                path = _materialize_file(directory, name, data)
                if snapshot and entries[name][0] == b"100755" and os.name != "nt":
                    path.chmod(0o700)


def _run_planner(
    target: Path,
    base: str,
    head: str,
    config: dict[str, Any],
    entries: dict[str, tuple[bytes, bytes]],
    *,
    snapshot: bool,
    deadline: float | None,
) -> tuple[dict[str, Any], str]:
    with tempfile.TemporaryDirectory(prefix="brigade-required-") as temp_dir:
        scratch = Path(temp_dir).resolve()
        directory = scratch / "planner"
        output = scratch / "plan.json"
        digest = _copy_planner(target, base, config["planner_files"], directory, deadline)
        replacements = {
            "{base}": base,
            "{head}": head,
            "{output}": str(output),
            "{planner_dir}": str(directory),
            "{repo}": str(target),
            "{python}": sys.executable,
        }
        tokens: list[str] = config["planner_argv"]
        env = _planner_env()
        if snapshot or "{head_dir}" in tokens:
            head_dir = scratch / "head"
            head_dir.mkdir()
            _copy_head(target, entries, head_dir, deadline, snapshot=snapshot)
            git_dir = os.fsdecode(_git(target, "rev-parse", "--absolute-git-dir", deadline=deadline).rstrip(b"\n"))
            env.update(GIT_DIR=git_dir, GIT_WORK_TREE=str(head_dir), GIT_NO_REPLACE_OBJECTS="1")
            replacements["{head_dir}"] = str(head_dir)
            if snapshot:
                replacements["{repo}"] = str(head_dir)
        argv = [
            str(directory / item.removeprefix(_PLANNER_PREFIX))
            if item.startswith(_PLANNER_PREFIX)
            else replacements.get(item, item)
            for item in tokens
        ]
        if tokens[0] == "{python}":
            # Keep imports beside the copied script while ignoring PYTHONPATH and site startup code.
            argv[1:1] = ["-E", "-S"]
        try:
            proc = subprocess.run(
                argv,
                cwd=directory,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_remaining(deadline, PLANNER_TIMEOUT_SECONDS),
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("planner timed out") from exc
        if proc.returncode:
            raise ValueError(f"planner exited with status {proc.returncode}")
        return _json_object(output.read_bytes(), "planner output"), digest


def _run_argv(template: list[str], tests: list[str]) -> list[str]:
    at = template.index("{tests}")
    # Pytest expands leading @ into argument files even after the option separator.
    paths = [f"./{name}" if name.startswith("@") else name for name in tests]
    return [*template[:at], *paths, *template[at + 1 :]]


def _plan_fields(
    plan: dict[str, Any], config: dict[str, Any], entries: dict[str, tuple[bytes, bytes]], digest: str
) -> dict[str, Any]:
    mode = plan.get(config["mode_key"])
    selected = plan.get(config["selected_key"])
    if not isinstance(mode, str) or mode not in {"selective", "full"}:
        raise ValueError("planner output mode must be selective or full")
    if not isinstance(selected, (list, dict)) or not all(isinstance(item, str) for item in selected):
        raise ValueError("planner output selected value must name test files")
    if mode == "selective" and not selected:
        raise ValueError("selective plan selected no tests")
    for name in selected:
        if not _repo_path(name):
            raise ValueError(f"invalid repository-relative test path: {name!r}")
        entry = entries.get(name)
        if entry is None:
            raise ValueError(f"selected test is missing at HEAD: {name!r}")
        if not _regular(entry[0]):
            raise ValueError(f"selected test is not a regular file at HEAD: {name!r}")
    selective = mode == "selective"
    reasons = [] if selective else plan.get("full_triggers", [])
    if not isinstance(reasons, list):
        raise ValueError("planner output full_triggers must be a list")
    tests = sorted(set(selected)) if selective else []
    result = {
        "mode": mode,
        "tests": tests,
        "run_argv": _run_argv(config["run_argv"], tests) if selective else [],
        "planner_digest": digest,
        "local_requirement": "tests" if selective else "ci",
        "reason": "planner selected repository test files" if selective else "planner requires CI",
        "reasons": reasons,
    }
    reported = plan.get(config["digest_key"]) if "digest_key" in config else None
    if isinstance(reported, str):
        result["planner_reported_digest"] = reported
    return result


def _plan_at(
    target: Path,
    base: str,
    head: str,
    *,
    snapshot: bool,
    deadline: float | None,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, tuple[bytes, bytes]]]:
    config = _config_at(target, base, deadline)
    if config is None:
        return (
            {
                "mode": "unconfigured",
                "local_requirement": "unknown",
                "reason": f"{CONFIG_PATH} does not exist at the base commit",
            },
            None,
            {},
        )
    entries = _tree_entries(target, head, deadline=deadline)
    plan, digest = _run_planner(target, base, head, config, entries, snapshot=snapshot, deadline=deadline)
    if snapshot and "error" in plan:
        detail = plan["error"]
        reason = " ".join(detail.split())[:160] if isinstance(detail, str) else ""
        raise ValueError(f"planner reported an error ({reason})" if reason else "planner reported an error")
    fields = _plan_fields(plan, config, entries, digest)
    if snapshot and "test_files_total" in plan:
        total = plan["test_files_total"]
        if type(total) is not int or total < len(fields["tests"]):
            raise ValueError("planner output has an invalid test_files_total")
        fields["test_files_total"] = total
    _remaining(deadline, PLANNER_TIMEOUT_SECONDS)
    return fields, config, entries


def plan_snapshot(
    target: Path, base: str, tree: str, *, deadline: float
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, tuple[bytes, bytes]]]:
    """Plan from a verified tree object and the base-pinned planner."""
    base = _commit_id(base.encode("ascii"))
    tree = _commit_id(tree.encode("ascii"))
    return _plan_at(target, base, tree, snapshot=True, deadline=deadline)


def _record_path(target: Path, head: str | None, base: str | None, *, worktree: bool = False) -> Path:
    suffix = ".worktree" if worktree else ""
    name = f"{head or 'unknown'}.{base[:12] if base else 'unknown'}{suffix}.json"
    path = target / ".brigade" / "work" / "verify-required" / name
    if path.parent.resolve() != path.parent:
        raise ValueError("verification record directory resolves through a symbolic link")
    return path


def _print_hint(template: list[str], tests: list[str], record_path: str) -> None:
    shown = min(len(tests), _MAX_HINT_TESTS)
    while shown:
        command = " ".join(shell_quote.quote_argument(arg, posix=True) for arg in _run_argv(template, tests[:shown]))
        if len(command) <= _MAX_HINT_CHARS:
            print(command)
            shell = "Git Bash" if shell_quote.is_windows() else "Bash"
            print(f"Run from the repository root in {shell}.")
            break
        shown -= 1
    if shown < len(tests):
        print(f"{len(tests) - shown} more test files. See the full command in {record_path}.")


def _print_human(payload: dict[str, Any], config: dict[str, Any] | None) -> None:
    mode = payload["mode"]
    if mode == "selective" and config is not None:
        _print_hint(config["run_argv"], payload["tests"], payload["record_path"])
    elif mode == "full":
        print(f"Required CI on head commit {payload['head_sha']} is the check. Do not run the full suite locally.")
    elif mode == "unconfigured":
        print(f"Required-test planning is not configured: {payload['reason']}")
    else:
        print(f"Required-test planning failed: {payload['reason']}. CI is required.")
    if payload["tree_id"] is not None and not payload["covers_worktree"]:
        print("Uncommitted changes are not covered by this plan.")
    if payload["base_source"] == "no_remote":
        print(
            "The plan was computed from local HEAD because the repository has no remote. It is not an independent check."
        )


def _resolve_base(target: Path, deadline: float | None = None) -> tuple[str, str]:
    resolved = verify_manifest_git.resolve_base(target, deadline=deadline, git_env=_planner_env())
    if resolved.status != "resolved" or resolved.commit is None or resolved.source is None:
        raise ValueError(resolved.message or "default base is unresolved")
    return _commit_id(resolved.commit.encode("ascii")), resolved.source


def required(*, target: Path, json_output: bool = False, worktree: bool = False) -> int:
    """Run the base-pinned repository planner and record its plan."""
    deadline = monotonic() + PLANNER_TIMEOUT_SECONDS if worktree else None
    config = None
    record_root = None
    failure_fields: dict[str, Any] = {
        "mode": "error",
        "local_requirement": "ci",
        "tests": [],
        "run_argv": [],
        "reasons": [],
        "planner_digest": None,
    }
    payload: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "base": None,
        "base_source": None,
        "base_trusted": False,
        "head_sha": None,
        "tree_id": None,
        "covers_worktree": False,
        "planned_at": localio.utc_now_iso_z(),
        "record_path": None,
        **failure_fields,
    }
    try:
        target = target.resolve()
        if not target.is_dir():
            raise ValueError("target must be an existing repository root")
        record_root = target
        try:
            root = Path(
                os.fsdecode(_git(target, "rev-parse", "--show-toplevel", deadline=deadline).rstrip(b"\n"))
            ).resolve()
        except ValueError as exc:
            raise ValueError(f"target must be a Git repository root ({exc})") from exc
        record_root = root
        head = payload["head_sha"] = _commit_id(_git(root, "rev-parse", "--verify", "HEAD^{commit}", deadline=deadline))
        if target != root:
            raise ValueError("target must be the repository root, not a subdirectory")
        if deadline is not None:
            tree, snapshot_head = localio.tree_fingerprint_with_head(root, git_env=_planner_env(), deadline=deadline)
            if tree is None or snapshot_head != head:
                _remaining(deadline, PLANNER_TIMEOUT_SECONDS)
                raise ValueError("could not snapshot the working tree at HEAD")
            payload.update(tree_id=tree, covers_worktree=True)
        else:
            tree = payload["tree_id"] = _commit_id(_git(root, "rev-parse", "--verify", f"{head}^{{tree}}"))
            payload["covers_worktree"] = localio.tree_fingerprint(root, git_env=_planner_env()) == tree
        resolved_base, source = _resolve_base(root, deadline)
        payload.update(base=resolved_base, base_source=source, base_trusted=source == "default_branch")
        if deadline is not None:
            fields, config, _ = plan_snapshot(root, resolved_base, tree, deadline=deadline)
        else:
            fields, config, _ = _plan_at(root, resolved_base, head, snapshot=False, deadline=None)
        payload.update(fields)
    except subprocess.TimeoutExpired:
        payload["reason"] = "Git timed out resolving the planning base"
    except (OSError, RuntimeError, ValueError) as exc:
        payload["reason"] = str(exc)

    if record_root is not None:
        try:
            path = _record_path(
                record_root, payload["tree_id"] if worktree else payload["head_sha"], payload["base"], worktree=worktree
            )
            payload["record_path"] = str(path)
            localio.write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        except (OSError, RuntimeError, ValueError) as exc:
            payload.pop("planner_reported_digest", None)
            payload.update(failure_fields, record_path=None, reason=f"cannot write verification record: {exc}")
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_human(payload, config)
    return {"error": 1, "unconfigured": 3}.get(payload["mode"], 0)
