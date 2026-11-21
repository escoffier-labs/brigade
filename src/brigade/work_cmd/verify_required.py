"""Plan the local tests required by a repository diff."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from .. import localio
from .helpers import _git, _git_value

CONFIG_PATH = "verify/required-tests.json"
CONFIG_SCHEMA = "brigade.verify_required_config.v1"
PLAN_SCHEMA = "brigade.verify_required.v1"
PLANNER_TIMEOUT_SECONDS = 60
_PLACEHOLDERS = {"{base}", "{head}", "{output}"}


def _default_base(target: Path) -> str | None:
    upstream = _git_value(target, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if upstream is None and _git_value(target, "rev-parse", "--verify", "origin/main^{commit}") is not None:
        upstream = "origin/main"
    upstream = upstream or "HEAD"
    return _git_value(target, "merge-base", "HEAD", upstream)


def _config_at(target: Path, base: str) -> tuple[dict[str, Any] | None, str | None]:
    result = _git(target, "show", f"{base}:{CONFIG_PATH}")
    if result.returncode != 0:
        return None, None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, f"required-tests config is invalid JSON: {exc.msg}"
    if not isinstance(value, dict) or value.get("schema") != CONFIG_SCHEMA:
        return None, "required-tests config has an unsupported schema"
    for key in ("planner_argv", "run_argv"):
        argv = value.get(key)
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
            return None, f"required-tests config {key} must be a non-empty string array"
    planner = value["planner_argv"]
    if any("{" in item or "}" in item for item in planner if item not in _PLACEHOLDERS):
        return None, "planner placeholders must be whole argv elements"
    run = value["run_argv"]
    if any(("{" in item or "}" in item) and item != "{tests}" for item in run):
        return None, "run placeholders must be whole argv elements"
    for key in ("mode_key", "selected_key", "digest_key"):
        if not isinstance(value.get(key), str) or not value[key]:
            return None, f"required-tests config {key} must be a string"
    return value, None


def _record(target: Path, head_sha: str, payload: dict[str, Any]) -> None:
    path = target / ".brigade" / "work" / "verify-required" / f"{head_sha}.json"
    localio.write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _result(
    *,
    target: Path,
    base: str,
    head_sha: str,
    tree_id: str,
    mode: str,
    tests: list[str],
    planner_digest: str | None,
    local_requirement: str,
    reason: str,
) -> dict[str, Any]:
    payload = {
        "schema": PLAN_SCHEMA,
        "mode": mode,
        "tests": tests,
        "base": base,
        "head_sha": head_sha,
        "tree_id": tree_id,
        "planner_digest": planner_digest,
        "covers_worktree": localio.tree_fingerprint(target) == tree_id,
        "local_requirement": local_requirement,
        "reason": reason,
        "planned_at": localio.utc_now_iso_z(),
    }
    _record(target, head_sha, payload)
    return payload


def _render_argv(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _print_human(payload: dict[str, Any], run_argv: list[str] | None = None) -> None:
    mode = payload["mode"]
    if mode == "selective" and run_argv is not None:
        print(_render_argv(run_argv))
    elif mode == "full":
        print(f"Required CI on head commit {payload['head_sha']} is the check. Do not run the full suite locally.")
    elif mode == "unconfigured":
        print(f"Required-test planning is not configured: {payload['reason']}")
    else:
        print(f"Required-test planning failed: {payload['reason']}")
    if not payload["covers_worktree"]:
        print("Uncommitted changes are not covered by this plan.")


def required(*, target: Path, base: str | None = None, json_output: bool = False) -> int:
    """Run the base-pinned repository planner and publish its read-only plan."""
    target = target.resolve()
    head_sha = _git_value(target, "rev-parse", "HEAD^{commit}")
    tree_id = _git_value(target, "rev-parse", "HEAD^{tree}")
    resolved_base = _git_value(target, "rev-parse", f"{base}^{{commit}}") if base else _default_base(target)
    if head_sha is None or tree_id is None or resolved_base is None:
        print("Required-test planning failed: target or base is not a valid Git commit.")
        return 2

    config, config_error = _config_at(target, resolved_base)
    if config is None:
        mode = "error" if config_error else "unconfigured"
        payload = _result(
            target=target,
            base=resolved_base,
            head_sha=head_sha,
            tree_id=tree_id,
            mode=mode,
            tests=[],
            planner_digest=None,
            local_requirement="ci" if mode == "error" else "none",
            reason=config_error or f"{CONFIG_PATH} does not exist at the base commit",
        )
        print(json.dumps(payload, indent=2, sort_keys=True) if json_output else "", end="\n" if json_output else "")
        if not json_output:
            _print_human(payload)
        return 2 if mode == "error" else 0

    with tempfile.TemporaryDirectory(prefix="brigade-required-") as temp_dir:
        output = Path(temp_dir) / "plan.json"
        replacements = {"{base}": resolved_base, "{head}": head_sha, "{output}": str(output)}
        planner_argv = [replacements.get(item, item) for item in config["planner_argv"]]
        try:
            proc = subprocess.run(
                planner_argv,
                cwd=target,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=PLANNER_TIMEOUT_SECONDS,
                shell=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"planner exited with status {proc.returncode}")
            plan = json.loads(output.read_text(encoding="utf-8"))
            if not isinstance(plan, dict):
                raise ValueError("planner output must be a JSON object")
            planned_mode = plan.get(config["mode_key"])
            selected = plan.get(config["selected_key"])
            digest = plan.get(config["digest_key"])
            if not isinstance(planned_mode, str) or planned_mode not in {"selective", "full"}:
                raise ValueError("planner output mode must be selective or full")
            if not isinstance(selected, (list, dict)) or not all(isinstance(item, str) for item in selected):
                raise ValueError("planner output selected value must name test files")
            if any(
                not item or PurePosixPath(item).is_absolute() or ".." in PurePosixPath(item).parts or "\\" in item
                for item in selected
            ):
                raise ValueError("planner output test files must be repository-relative paths")
            if not isinstance(digest, str) or not digest:
                raise ValueError("planner output digest must be a string")
            tests = sorted(selected)
            reason = "planner selected repository test files" if planned_mode == "selective" else "planner requires CI"
            payload = _result(
                target=target,
                base=resolved_base,
                head_sha=head_sha,
                tree_id=tree_id,
                mode=planned_mode,
                tests=tests,
                planner_digest=digest,
                local_requirement="tests" if planned_mode == "selective" else "ci",
                reason=reason,
            )
        except subprocess.TimeoutExpired:
            payload = _result(
                target=target,
                base=resolved_base,
                head_sha=head_sha,
                tree_id=tree_id,
                mode="error",
                tests=[],
                planner_digest=None,
                local_requirement="ci",
                reason="planner timed out",
            )
        except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
            payload = _result(
                target=target,
                base=resolved_base,
                head_sha=head_sha,
                tree_id=tree_id,
                mode="error",
                tests=[],
                planner_digest=None,
                local_requirement="ci",
                reason=str(exc),
            )

    run_argv: list[str] | None = None
    if payload["mode"] == "selective":
        run_argv = []
        for item in config["run_argv"]:
            run_argv.extend(payload["tests"] if item == "{tests}" else [item])
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_human(payload, run_argv)
    return 2 if payload["mode"] == "error" else 0
