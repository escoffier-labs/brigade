#!/usr/bin/env python3
"""Require maintainer approval for changes to CI machinery."""

import argparse
from fnmatch import fnmatchcase
import json
import os
import re
import subprocess
import sys

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
    "scripts/check_size_ratchet_baseline.py",
    "scripts/ci_*",
    "scripts/download_metrics.py",
    "scripts/generate_component_manifest.py",
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
COMMIT_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


def protected_paths(paths: list[str]) -> list[str]:
    """Return the unique protected paths in sorted order."""
    return sorted({path for path in paths if any(fnmatchcase(path, pattern) for pattern in PROTECTED_PATTERNS)})


def gh_api(endpoint: str, *flags: str) -> object:
    response = subprocess.run(
        ["gh", "api", endpoint, *flags], check=True, capture_output=True, encoding="utf-8", timeout=120
    )
    return json.loads(response.stdout)


def paginated_entries(endpoint: str) -> list[object]:
    pages = gh_api(f"{endpoint}?per_page=100", "--paginate", "--slurp")
    if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
        raise ValueError("Invalid paginated response.")
    return [entry for page in pages for entry in page]


def read_pull_snapshot(endpoint: str, default_branch: str) -> tuple[str, str, int]:
    pull = gh_api(endpoint)
    if not isinstance(pull, dict) or type(pull.get("changed_files")) is not int:
        raise ValueError("Invalid pull request response.")
    head, base = pull.get("head"), pull.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict) or base.get("ref") != default_branch:
        raise ValueError("Invalid pull request head or base.")
    head_sha, base_sha = head.get("sha"), base.get("sha")
    if any(not isinstance(sha, str) or not COMMIT_SHA.fullmatch(sha) for sha in (head_sha, base_sha)):
        raise ValueError("Invalid pull request commit.")
    return head_sha, base_sha, pull["changed_files"]


def read_pull_request(repo: str, number: int, default_branch: str) -> tuple[list[str], str]:
    endpoint = f"repos/{repo}/pulls/{number}"
    snapshot = read_pull_snapshot(endpoint, default_branch)
    files = paginated_entries(f"{endpoint}/files")
    # The file endpoint uses the live PR. Reject a head or base change during pagination.
    if snapshot != read_pull_snapshot(endpoint, default_branch):
        raise ValueError("Pull request changed while listing files.")
    head_sha, _, changed_files = snapshot
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

    return paths, head_sha


def has_approval(repo: str, head_sha: str) -> bool:
    statuses = paginated_entries(f"repos/{repo}/commits/{head_sha}/statuses")
    # The API returns statuses newest first. Only Actions can record approval.
    for status in statuses:
        if not isinstance(status, dict) or not isinstance(status.get("context"), str):
            raise ValueError("Invalid commit status.")
        if status["context"] != APPROVAL_LABEL:
            continue
        creator = status.get("creator")
        if not isinstance(creator, dict) or any(not isinstance(creator.get(key), str) for key in ("login", "type")):
            raise ValueError("Invalid commit status creator.")
        if creator["login"] != "github-actions[bot]" or creator["type"] != "Bot":
            continue
        state = status.get("state")
        if state not in ("success", "failure", "pending", "error"):
            raise ValueError("Invalid commit status state.")
        return state == "success"
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    parser.add_argument("--pr", required=True, type=int)
    args = parser.parse_args(argv)

    default_branch = os.environ.get("REPO_DEFAULT_BRANCH")
    if not default_branch or os.environ.get("PR_BASE_REF") != default_branch:
        print("The pull request must target the repository's default branch.", file=sys.stderr)
        return 2

    if os.environ.get("APPROVAL_RECORD_RESULT") not in ("success", "skipped"):
        print("Could not record the workflow approval status.", file=sys.stderr)
        return 2

    try:
        paths, head_sha = read_pull_request(args.repo, args.pr, default_branch)
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
        if has_approval(args.repo, head_sha):
            print("A maintainer approved this commit.")
            return 0
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Could not read the workflow approval status for the pull request head.", file=sys.stderr)
        return 2

    print(f"A maintainer must apply the {json.dumps(APPROVAL_LABEL)} label to this commit.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
