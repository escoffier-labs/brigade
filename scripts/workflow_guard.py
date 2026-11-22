#!/usr/bin/env python3
"""Require maintainer approval for changes to CI machinery."""

import argparse
from fnmatch import fnmatchcase
import json
import subprocess
import sys

PROTECTED_PATTERNS = (
    ".github/workflows/*",  # Workflow and job definitions.
    ".github/actions/*",  # Local actions called by workflows.
    "scripts/ci_*",  # CI selection and execution helpers.
    "scripts/pr_test_tiers.json",  # Pull request test tier configuration.
    "scripts/pr_tier_selection.py",  # Pull request test tier selection.
    "scripts/workflow_guard.py",  # This approval check.
    "scripts/windows_affected_tests.py",  # Windows test selection.
    "verify/required-check.json",  # Required check declaration.
    "verify/required-tests.json",  # Required test declaration.
)
APPROVAL_LABEL = "workflow-change-approved"


def protected_paths(paths: list[str]) -> list[str]:
    """Return the unique protected paths in sorted order."""
    return sorted({path for path in paths if any(fnmatchcase(path, pattern) for pattern in PROTECTED_PATTERNS)})


def gh_api(endpoint: str, *flags: str) -> object:
    response = subprocess.run(
        ["gh", "api", endpoint, *flags], check=True, capture_output=True, encoding="utf-8", timeout=120
    )
    return json.loads(response.stdout)


def read_pull_request(repo: str, number: int) -> tuple[list[str], set[str]]:
    endpoint = f"repos/{repo}/pulls/{number}"
    pages = gh_api(f"{endpoint}/files?per_page=100", "--paginate", "--slurp")
    if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
        raise ValueError("Invalid changed-file response.")

    files = [entry for page in pages for entry in page]
    pull = gh_api(endpoint)
    if (
        not isinstance(pull, dict)
        or type(pull.get("changed_files")) is not int
        or not isinstance(pull.get("labels"), list)
    ):
        raise ValueError("Invalid pull request response.")
    # GitHub caps the file list at 3,000 entries even with pagination.
    if len(files) != pull["changed_files"]:
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

    labels = set()
    for label in pull["labels"]:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            raise ValueError("Invalid label entry.")
        labels.add(label["name"])
    return paths, labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--label", default=APPROVAL_LABEL)
    args = parser.parse_args(argv)

    try:
        paths, labels = read_pull_request(args.repo, args.pr)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Could not read the pull request's complete changed-file list and labels.", file=sys.stderr)
        return 2

    matches = protected_paths(paths)
    if not matches:
        print("No protected CI files changed.")
        return 0

    print("Protected CI files changed.")
    for path in matches:
        # Keep each untrusted path on one line so it cannot emit workflow commands.
        print(f"  {json.dumps(path)}")
    print(f"The {json.dumps(args.label)} label is required. A maintainer applies it after reading the change.")
    return 0 if args.label in labels else 1


if __name__ == "__main__":
    raise SystemExit(main())
