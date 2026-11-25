#!/usr/bin/env python3
"""Require maintainer approval for changes to CI machinery."""

import argparse
from fnmatch import fnmatchcase
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import NamedTuple

PROTECTED_PATTERNS = (
    ".content-guard-baseline.json",
    ".github/actions/*",
    ".github/workflows/*",
    "conftest.py",
    "*/conftest.py",
    "engines/hub/src/memory/scan.rs",
    "npm/feasibility/*",
    "pyproject.toml",
    "registry/skills/verify-brigade/control-brigade.py",
    "scripts/benchmark-guard-native.py",
    "scripts/changelog_fragments.py",
    "scripts/check_cli_upgrade.py",
    "scripts/check_size_ratchet_baseline.py",
    "scripts/ci_*",
    "scripts/download_metrics.py",
    "scripts/generate_component_manifest.py",
    "scripts/ledger_decide.py",
    "scripts/ledger_recompute.py",
    "scripts/managed_snapshot.py",
    "scripts/memory_scan_bundle.py",
    "scripts/memory_scan_unicode14_unassigned.ranges",
    "scripts/outcome_attest_plan.py",
    "scripts/pr_test_tiers.json",
    "scripts/pr_tier_selection.py",
    "scripts/pr_tier_shadow.py",
    "scripts/prepare_dev_wheel.py",
    "scripts/published-artifact-acceptance.py",
    "scripts/retired_engine_names.py",
    "scripts/shipped_file_history.py",
    "scripts/stamp_brigade_cli.py",
    "scripts/template_profile_snapshot.py",
    "scripts/verify-engine-rename-installer",
    "scripts/verify-focused",
    "scripts/verify-guard-native",
    "scripts/verify-rust-guard",
    "scripts/verify_component_manifest_provenance.py",
    "scripts/verify_env.py",
    "scripts/verify_version_check_endpoint.py",
    "scripts/version_sync.py",
    "scripts/windows-native-acceptance.ps1",
    "scripts/windows_affected_tests.py",
    "scripts/windows_job.py",
    "scripts/windows_pytest.py",
    "scripts/windows_pytest_diagnostics.py",
    "scripts/windows_sqlite_flush_probe.py",
    "scripts/windows_test_weights.json",
    "scripts/workflow_guard.py",
    "src/brigade/__init__.py",
    "src/brigade/guard/memory-rules.json",
    "src/brigade/memory_content_scan.py",
    "src/brigade/templates/components/manifest-v1.json",
    "src/brigade/templates/shipped-file-history.json",
    "src/brigade/templates/stations/managed-snapshot.json",
    "src/brigade/templates/template-profile-snapshot.json",
    "tests/_coverage_sidecar_guard.py",
    "tests/fleet_hub_container_smoke.py",
    "tests/fixtures/retired-engine-names-allowlist.json",
    "tests/support.py",
    "tests/test_ci_workflow.py",
    "tests/test_module_size_ratchet.py",
    "tests/test_publish_workflow.py",
    "tests/test_verify_brigade_guard_parity.py",
    "tests/test_workflow_guard.py",
    "tests/windows_pytest_allowlist.json",
    "verify/*",
)
APPROVAL_LABEL = "workflow-change-approved"
RELEASE_BRANCH = "release/0.32"
APPROVAL_DESCRIPTION = re.compile(
    r"pull request ([1-9][0-9]*) (approved|approval removed)(?: \[([0-9a-f]{64}):([1-9][0-9]*)\])?"
)
APPROVAL_INVALIDATORS = {"base_ref_changed", "automatic_base_change_succeeded", "reopened", "head_ref_force_pushed"}
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
GITHUB_ACTIONS_BOT_ID = 41898282
COMMIT_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


class PullEvent(NamedTuple):
    repo: str
    number: int
    base: str
    default_branch: str
    head: str
    action: str
    label: object
    approval_after: str | None


def object_field(value: object, field: str) -> object:
    return value.get(field) if isinstance(value, dict) else None


def read_event(repo: str, number: int) -> PullEvent:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    repository = object_field(event, "repository")
    pull = object_field(event, "pull_request")
    base = object_field(pull, "base")
    base_ref = object_field(base, "ref")
    default_branch = object_field(repository, "default_branch")
    action = object_field(event, "action")
    if (
        object_field(repository, "full_name") != repo
        or object_field(object_field(base, "repo"), "full_name") != repo
        or object_field(event, "number") != number
        or object_field(pull, "number") != number
        or object_field(pull, "state") != "open"
        or not isinstance(default_branch, str)
        or not default_branch
        or not isinstance(base_ref, str)
        or base_ref not in (default_branch, RELEASE_BRANCH)
        or not isinstance(action, str)
        or action not in ("opened", "synchronize", "reopened", "edited", "labeled", "unlabeled", "ready_for_review")
    ):
        raise ValueError("Invalid pull request event.")
    base_sha = object_field(base, "sha")
    head_sha = object_field(object_field(pull, "head"), "sha")
    if not isinstance(base_sha, str) or not COMMIT_SHA.fullmatch(base_sha):
        raise ValueError("Invalid event base commit.")
    if not isinstance(head_sha, str) or not COMMIT_SHA.fullmatch(head_sha):
        raise ValueError("Invalid event head commit.")
    approval_after = None
    changes = event.get("changes")
    if action == "reopened" or (action == "edited" and isinstance(changes, dict) and "base" in changes):
        stamp = object_field(pull, "updated_at")
        if not isinstance(stamp, str) or not TIMESTAMP.fullmatch(stamp):
            raise ValueError("Invalid pull request lifecycle timestamp.")
        approval_after = stamp
    return PullEvent(
        repo,
        number,
        base_ref,
        default_branch,
        head_sha,
        action,
        object_field(event.get("label"), "name"),
        approval_after,
    )


def protected_paths(paths: list[str]) -> list[str]:
    """Return the unique protected paths in sorted order."""
    return sorted({path for path in paths if any(fnmatchcase(path, pattern) for pattern in PROTECTED_PATTERNS)})


def gh_api(endpoint: str, *flags: str) -> object:
    response = subprocess.run(
        ["gh", "api", endpoint, *flags], check=True, capture_output=True, encoding="utf-8", timeout=120
    )
    return json.loads(response.stdout)


def paginated_entries(endpoint: str) -> list[object]:
    separator = "&" if "?" in endpoint else "?"
    pages = gh_api(f"{endpoint}{separator}per_page=100", "--paginate", "--slurp")
    if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
        raise ValueError("Invalid paginated response.")
    return [entry for page in pages for entry in page]


def read_pull_snapshot(endpoint: str, base_ref: str, event: PullEvent | None = None) -> tuple[str, str, int]:
    pull = gh_api(endpoint)
    if not isinstance(pull, dict) or type(pull.get("changed_files")) is not int:
        raise ValueError("Invalid pull request response.")
    head, base = pull.get("head"), pull.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict) or base.get("ref") != base_ref:
        raise ValueError("Invalid pull request head or base.")
    head_sha, base_sha = head.get("sha"), base.get("sha")
    if not isinstance(head_sha, str) or not COMMIT_SHA.fullmatch(head_sha):
        raise ValueError("Invalid pull request head commit.")
    if not isinstance(base_sha, str) or not COMMIT_SHA.fullmatch(base_sha):
        raise ValueError("Invalid pull request base commit.")
    if event is not None and (
        head_sha != event.head
        or pull.get("number") != event.number
        or pull.get("state") != "open"
        or object_field(base.get("repo"), "full_name") != event.repo
    ):
        raise ValueError("Live pull request does not match the event.")
    return head_sha, base_sha, pull["changed_files"]


def read_pull_files(
    endpoint: str, base_ref: str, event: PullEvent | None = None
) -> tuple[list[str], tuple[str, str, int]]:
    snapshot = read_pull_snapshot(endpoint, base_ref, event)
    files = paginated_entries(f"{endpoint}/files")
    # A base advance before the first read is normal. Any movement during the check fails closed.
    if snapshot != read_pull_snapshot(endpoint, base_ref, event):
        raise ValueError("Pull request changed while listing files.")
    _, _, changed_files = snapshot
    # GitHub caps the file list at 3,000 entries even with pagination.
    if len(files) != changed_files:
        raise ValueError("Incomplete changed-file list.")

    paths = []
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("filename"), str) or not entry["filename"]:
            raise ValueError("Invalid changed-file entry.")
        paths.append(entry["filename"])
        if entry.get("status") == "renamed" or "previous_filename" in entry:
            previous = entry.get("previous_filename")
            if not isinstance(previous, str) or not previous:
                raise ValueError("Missing previous filename.")
            paths.append(previous)

    return paths, snapshot


def read_pull_request(repo: str, number: int, base_ref: str) -> tuple[list[str], str]:
    paths, snapshot = read_pull_files(f"repos/{repo}/pulls/{number}", base_ref)
    return paths, snapshot[0]


def base_scope(base: str) -> str:
    return sha256(base.encode("utf-8")).hexdigest()


def approval_history(repo: str, number: int) -> tuple[tuple[str, int, str] | None, bool]:
    """Read the latest approval label transition and whether the PR changed since opening."""
    entries = paginated_entries(f"repos/{repo}/issues/{number}/events")
    latest_label = None
    latest_invalidation = None
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(action := entry.get("event"), str):
            raise ValueError("Invalid pull request event history.")
        if action in ("labeled", "unlabeled"):
            label = object_field(entry.get("label"), "name")
            if not isinstance(label, str):
                raise ValueError("Invalid label in pull request event history.")
            if label != APPROVAL_LABEL:
                continue
        elif action not in APPROVAL_INVALIDATORS:
            continue
        stamp, event_id = entry.get("created_at"), entry.get("id")
        if not isinstance(stamp, str) or not TIMESTAMP.fullmatch(stamp) or type(event_id) is not int or event_id < 1:
            raise ValueError("Invalid approval event history.")
        transition = stamp, event_id, action
        if action in APPROVAL_INVALIDATORS:
            latest_invalidation = max(latest_invalidation or transition, transition)
        else:
            latest_label = max(latest_label or transition, transition)
    # Timestamps have second precision. Require the label to follow any lifecycle change
    # unambiguously, regardless of the ordering of pages or event IDs.
    if latest_label is not None and latest_invalidation is not None and latest_label[0] <= latest_invalidation[0]:
        latest_label = None
    return latest_label, latest_invalidation is not None


def matching_approval_status(
    repo: str,
    head_sha: str,
    number: int,
    *,
    scope: str | None = None,
    label_id: int | None = None,
    allow_legacy: bool = True,
) -> dict[str, object] | None:
    statuses = paginated_entries(f"repos/{repo}/commits/{head_sha}/statuses")
    # The API returns statuses newest first. Only Actions can record approval.
    for status in statuses:
        if not isinstance(status, dict) or not isinstance(status.get("context"), str):
            raise ValueError("Invalid commit status.")
        if status["context"] != APPROVAL_LABEL:
            continue
        description = status.get("description")
        match = APPROVAL_DESCRIPTION.fullmatch(description) if isinstance(description, str) else None
        if match is None or int(match[1]) != number:
            continue
        if match[3] is None:
            if not allow_legacy:
                continue
        elif (scope is not None and match[3] != scope) or (label_id is not None and match[4] != str(label_id)):
            continue
        creator = status.get("creator")
        if not isinstance(creator, dict) or any(not isinstance(creator.get(key), str) for key in ("login", "type")):
            raise ValueError("Invalid commit status creator.")
        if type(creator.get("id")) is not int:
            raise ValueError("Invalid commit status creator id.")
        if (
            creator["login"] != "github-actions[bot]"
            or creator["type"] != "Bot"
            or creator["id"] != GITHUB_ACTIONS_BOT_ID
        ):
            continue
        state = status.get("state")
        if state not in ("success", "failure", "pending", "error"):
            raise ValueError("Invalid commit status state.")
        return status
    return None


def is_success_approval(status: dict[str, object]) -> bool:
    description = status.get("description")
    match = APPROVAL_DESCRIPTION.fullmatch(description) if isinstance(description, str) else None
    return status.get("state") == "success" and match is not None and match[2] == "approved"


def has_approval(event: PullEvent) -> bool:
    history = approval_history(event.repo, event.number)
    label, invalidated = history
    if (
        label is None
        or label[2] != "labeled"
        or (event.approval_after is not None and label[0] <= event.approval_after)
    ):
        return False
    stamp, label_id, _ = label
    status = matching_approval_status(
        event.repo,
        event.head,
        event.number,
        scope=base_scope(event.base),
        label_id=label_id,
        allow_legacy=event.base == event.default_branch and not invalidated and event.approval_after is None,
    )
    if status is None or not is_success_approval(status):
        return False
    description = status["description"]
    if description == f"pull request {event.number} approved":
        written_at = status.get("created_at")
        if not isinstance(written_at, str) or not TIMESTAMP.fullmatch(written_at) or written_at <= stamp:
            return False
    return history == approval_history(event.repo, event.number)


def record_approval(endpoint: str, event: PullEvent) -> None:
    if event.action not in ("labeled", "unlabeled") or event.label != APPROVAL_LABEL:
        raise ValueError("Invalid approval event action.")
    snapshot = read_pull_snapshot(endpoint, event.base, event)
    history = approval_history(event.repo, event.number)
    label, _ = history
    entries = paginated_entries(f"repos/{event.repo}/issues/{event.number}/labels")
    names = [object_field(entry, "name") for entry in entries]
    if any(not isinstance(name, str) for name in names):
        raise ValueError("Invalid pull request label.")
    if label is None:
        return
    present = APPROVAL_LABEL in names
    # Apply only the live transition. An out-of-order removal cannot overwrite a re-add.
    if present != (label[2] == "labeled"):
        raise ValueError("Pull request label does not match its event history.")
    if present and event.action != "labeled":
        return
    if history != approval_history(event.repo, event.number) or snapshot != read_pull_snapshot(
        endpoint, event.base, event
    ):
        raise ValueError("Pull request changed while recording approval.")
    state, description = ("success", "approved") if present else ("failure", "approval removed")
    gh_api(
        f"repos/{event.repo}/statuses/{event.head}",
        "-X",
        "POST",
        "-f",
        f"context={APPROVAL_LABEL}",
        "-f",
        f"state={state}",
        "-f",
        f"description=pull request {event.number} {description} [{base_scope(event.base)}:{label[1]}]",
    )


def clear_approval(endpoint: str, event: PullEvent) -> None:
    if event.action != "synchronize":
        raise ValueError("Invalid label cleanup event.")
    read_pull_snapshot(endpoint, event.base, event)
    result = subprocess.run(
        [
            "gh",
            "api",
            "--include",
            "--silent",
            "-X",
            "DELETE",
            f"repos/{event.repo}/issues/{event.number}/labels/{APPROVAL_LABEL}",
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )
    if result.returncode and not re.match(r"HTTP/\S+\s+404(?:\s|$)", result.stdout):
        raise ValueError("Could not clear the workflow approval label.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    parser.add_argument("--pr", required=True, type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record-approval", action="store_true")
    mode.add_argument("--clear-approval", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not REPOSITORY.fullmatch(args.repo) or args.pr < 1:
            raise ValueError("Invalid repository or pull request number.")
        event = read_event(args.repo, args.pr)
    except (KeyError, OSError, ValueError):
        print(
            "Invalid pull request event. Expected this repository and its default branch or release/0.32.",
            file=sys.stderr,
        )
        return 2

    endpoint = f"repos/{event.repo}/pulls/{event.number}"
    if args.record_approval or args.clear_approval:
        try:
            if args.record_approval:
                record_approval(endpoint, event)
            else:
                clear_approval(endpoint, event)
            return 0
        except (OSError, ValueError, subprocess.SubprocessError):
            print("Could not update approval for the pull request event.", file=sys.stderr)
            return 2

    if os.environ.get("APPROVAL_RECORD_RESULT") not in ("success", "skipped"):
        print("Could not record the workflow approval status.", file=sys.stderr)
        return 2

    try:
        paths, snapshot = read_pull_files(endpoint, event.base, event)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Could not read a complete changed-file list for the pull request head.", file=sys.stderr)
        return 2

    matches = protected_paths(paths)
    if not matches:
        print("No protected CI files changed.")
        return 0

    print("Protected CI files changed.")
    for path in matches:
        # Keep each untrusted path on one line so it cannot emit workflow commands.
        print(f"  {json.dumps(path)}")
    try:
        if has_approval(event):
            if snapshot != read_pull_snapshot(endpoint, event.base, event):
                raise ValueError("Pull request changed while checking approval.")
            print("A maintainer approved this pull request at its current head.")
            return 0
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Could not read the workflow approval status for the pull request head.", file=sys.stderr)
        return 2

    print(f"A maintainer must apply the {json.dumps(APPROVAL_LABEL)} label to this pull request at its current head.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
