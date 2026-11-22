"""Fetch and read per-commit GitHub check evidence."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .. import localio
from . import helpers

DEFAULT_CHECK_NAME = "ci-gate"
_SCHEMA = "brigade.verify_ci.v1"
_RECORD_DIR = ".brigade/work/verify-ci"
# Brigade runtime state is never the work product, so it does not make a tree dirty.
_STATE_DIR = ".brigade/work"
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_EXIT_CODES = {"passed": 0, "failed": 1, "pending": 2, "unknown": 2}


def _head_identity(target: Path) -> tuple[str, str] | None:
    try:
        head = helpers._git_value(target, "rev-parse", "--verify", "HEAD")
        if head is None or _SHA.fullmatch(head) is None:
            return None
        tree = helpers._git_value(target, "rev-parse", "--verify", f"{head}^{{tree}}")
    except (OSError, UnicodeError):
        return None
    return (head, tree) if tree and _SHA.fullmatch(tree) else None


def _github_repository(target: Path) -> str | None:
    try:
        remote = helpers._git_value(target, "remote", "get-url", "origin") or ""
        if remote.startswith("git@github.com:"):
            path = remote.removeprefix("git@github.com:")
        else:
            url = urlsplit(remote)
            if url.scheme not in {"https", "ssh", "git"} or url.hostname != "github.com":
                return None
            path = url.path.removeprefix("/")
    except (OSError, UnicodeError, ValueError):
        return None
    repository = path.rstrip("/").removesuffix(".git")
    if re.fullmatch(r"[a-zA-Z0-9-]+/[a-zA-Z0-9_.-]+", repository) is None or repository.split("/")[1] in {".", ".."}:
        return None
    return repository


def _named_runs(output: str, check_name: str) -> list[dict[str, Any]]:
    pages = json.loads(output)
    if not isinstance(pages, list) or not pages:
        raise ValueError("expected a list of check run pages")
    runs = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("check_runs"), list):
            raise ValueError("expected a check run list")
        for run in page["check_runs"]:
            if not isinstance(run, dict) or not isinstance(run.get("name"), str):
                raise ValueError("expected a named check run")
            if run["name"] == check_name:
                if type(run.get("id")) is not int:
                    raise ValueError("expected a check run id")
                runs.append(run)
    return runs


def _fetch_check(target: Path, repository: str, head: str, check_name: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repository}/commits/{head}/check-runs",
                "--hostname",
                "github.com",
                "--method",
                "GET",
                "--paginate",
                "--slurp",
                "-f",
                f"check_name={check_name}",
                "-f",
                "filter=all",
                "-F",
                "per_page=100",
            ],
            cwd=target,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )
    except OSError:
        return {"reason": "gh is unavailable."}
    except subprocess.TimeoutExpired:
        return {"reason": "The gh request timed out."}
    except UnicodeError:
        return {"reason": "GitHub returned an unreadable check-run response."}
    if result.returncode:
        # gh's diagnostics can include authentication details. Keep them private.
        return {"reason": "gh could not read check runs. Check authentication and connectivity."}
    try:
        runs = _named_runs(result.stdout, check_name)
    except (ValueError, RecursionError):
        return {"reason": "GitHub returned an unreadable check-run response."}
    if not runs:
        return {"reason": "No check run with that name was found for HEAD."}

    # A cancelled run says nothing about the commit. A push to main, for example,
    # often starts a second run for a commit the merge queue already tested, and
    # the next merge cancels it. Decide from the runs that were not cancelled.
    decisive = [
        item for item in runs if not (item.get("status") == "completed" and item.get("conclusion") == "cancelled")
    ]
    if not decisive:
        return {"reason": "Every check run with that name for HEAD was cancelled."}
    # Run IDs increase with creation order, including for runs still queued.
    run = max(decisive, key=lambda item: item["id"])
    if run.get("head_sha") != head:
        return {"reason": "The newest check run belongs to a different head."}
    status, conclusion, url = run.get("status"), run.get("conclusion"), run.get("html_url")
    if (
        not isinstance(status, str)
        or "conclusion" not in run
        or (conclusion is not None and not isinstance(conclusion, str))
        or not isinstance(url, str)
    ):
        return {"reason": "GitHub returned an unreadable check-run response."}
    fields = {"check_run_id": run["id"], "status": status, "conclusion": conclusion, "html_url": url}
    if status == "completed":
        return {**fields, "state": "passed" if conclusion == "success" else "failed"}
    if status in {"queued", "in_progress", "waiting", "requested", "pending"}:
        return {**fields, "state": "pending"}
    return {**fields, "reason": "GitHub returned an unrecognized check status."}


def _record_path(target: Path, head: str, check_name: str = DEFAULT_CHECK_NAME) -> Path:
    # One record per check, so a run for another check never replaces the required one.
    if check_name == DEFAULT_CHECK_NAME:
        return target / _RECORD_DIR / f"{head}.json"
    digest = hashlib.sha256(check_name.encode("utf-8")).hexdigest()[:12]
    return target / _RECORD_DIR / f"{head}.{digest}.json"


def _tree_state(target: Path, head: str, tree: str) -> str:
    """Return ``clean``, ``dirty`` or ``unreadable`` for the working tree against HEAD."""
    live = localio.tree_fingerprint_with_head(target, extra_evidence_paths=(_STATE_DIR,))
    if None in live:
        return "unreadable"
    return "clean" if live == (tree, head) else "dirty"


def verify_ci(*, target: Path, check_name: str = DEFAULT_CHECK_NAME, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    identity = _head_identity(target)
    if identity is None:
        print("error: cannot resolve a committed HEAD and tree for --target", file=sys.stderr)
        return 2
    head, tree = identity
    tree_state = _tree_state(target, head, tree)
    if tree_state == "unreadable":
        print("error: could not read the working tree state from git. Nothing was recorded.", file=sys.stderr)
        return 2
    if tree_state == "dirty":
        print("error: the working tree differs from HEAD. Commit and push your changes first.", file=sys.stderr)
        return 2

    repository = _github_repository(target)
    fetched = (
        _fetch_check(target, repository, head, check_name)
        if repository is not None
        else {"reason": "No GitHub origin remote was found."}
    )
    record = {
        "schema": _SCHEMA,
        "head_sha": head,
        "tree_id": tree,
        "repository": repository,
        "check_name": check_name,
        "check_run_id": None,
        "status": None,
        "conclusion": None,
        "html_url": None,
        "state": "unknown",
        "fetched_at": localio.utc_now_iso(),
        **fetched,
    }
    try:
        localio.write_json(_record_path(target, head, check_name), record)
    except OSError:
        print("error: could not write the CI evidence record", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(f"CI on HEAD: {record['state']} ({check_name}, {head[:12]})")
        if reason := record.get("reason"):
            print(reason)
    return _EXIT_CODES[record["state"]]


def closeout_evidence(target: Path) -> dict[str, Any]:
    """Return the stored record for the required check, or ``unknown``.

    Closeout reads only the default required check. A record for another
    check name, another commit, or a tree that has changed since is not
    evidence for the work being closed.
    """
    identity = _head_identity(target)
    head, tree = identity or (None, None)
    unknown = {"head_sha": head, "tree_id": tree, "state": "unknown", "hint": "brigade work verify ci"}
    record = localio.read_json_dict(_record_path(target, head)) if head else None
    if not (
        record
        and record.get("schema") == _SCHEMA
        and record.get("check_name") == DEFAULT_CHECK_NAME
        and record.get("head_sha") == head
        and record.get("tree_id") == tree
        and isinstance(record.get("state"), str)
        and record["state"] in _EXIT_CODES
    ):
        return unknown
    try:
        tree_state = _tree_state(target, head, tree) if head and tree else "unreadable"
    except OSError:
        tree_state = "unreadable"
    if tree_state == "dirty":
        return {**unknown, "reason": "The working tree differs from HEAD, so the saved CI result does not cover it."}
    if tree_state != "clean":
        return {**unknown, "reason": "The working tree state could not be read from git."}
    return record


def closeout_summary(evidence: dict[str, Any]) -> str:
    summary = f"CI on HEAD: {evidence['state']}"
    if evidence.get("check_name"):
        summary += f" ({evidence['check_name']}, {evidence['head_sha'][:12]}"
        summary += f", fetched {evidence['fetched_at']})" if evidence.get("fetched_at") else ")"
    for detail in (evidence.get("reason"), evidence.get("hint")):
        if detail:
            summary = f"{summary.rstrip('.')}. {detail}"
    return summary
