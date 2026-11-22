#!/usr/bin/env python3
"""Print a weekly audit of merged pull requests with workflow approvals."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import re
import subprocess
import sys
from typing import Any
from urllib.parse import quote

from scripts import workflow_guard

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def safe_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).replace("|", "\\u007c").replace("`", "\\u0060")


def parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError("Invalid timestamp.")
    return parsed.astimezone(timezone.utc)


def merged_pull_requests(repo: str, default_branch: str) -> list[dict[str, Any]]:
    pulls = workflow_guard.paginated_entries(
        f"repos/{repo}/pulls?state=closed&base={quote(default_branch, safe='')}&sort=updated&direction=desc"
    )
    result = []
    for pull in pulls:
        if not isinstance(pull, dict):
            raise ValueError("Invalid pull request entry.")
        number, title, merged_at, base, labels = (
            pull.get("number"),
            pull.get("title"),
            pull.get("merged_at"),
            pull.get("base"),
            pull.get("labels"),
        )
        if type(number) is not int or not isinstance(title, str) or not isinstance(base, dict):
            raise ValueError("Invalid pull request entry.")
        if base.get("ref") != default_branch or not isinstance(labels, list):
            raise ValueError("Invalid pull request entry.")
        if merged_at is not None:
            parse_timestamp(merged_at)
        if any(not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in labels):
            raise ValueError("Invalid pull request labels.")
        result.append(pull)
    return result


def approval_record(repo: str, head_sha: str, number: int) -> tuple[str, str] | None:
    status = workflow_guard.matching_approval_status(repo, head_sha, number)
    if status is None:
        return None
    created_at = status.get("created_at")
    written_at = parse_timestamp(created_at).isoformat().replace("+00:00", "Z")
    is_success = status.get("state") == "success" and status.get("description") == f"pull request {number} approved"
    return ("yes" if is_success else "no", written_at)


def table(rows: list[tuple[int, str, str, str, str, str, str]]) -> str:
    lines = [
        "| # | Pull request | Merged (UTC) | Protected files (JSON) | Success approval | Status written (UTC) | Label present |",
        "| ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for number, title, merged_at, files, approval, written_at, has_label in rows:
        # JSON strings keep untrusted titles and paths on one line and safe in a table cell.
        lines.append(
            f"| {number} | {safe_json(title)} | {merged_at} | {files} | {approval} | {written_at} | {has_label} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--label", default=workflow_guard.APPROVAL_LABEL)
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo) or args.days < 1 or not args.label:
        print("Invalid repository, day range, or label.", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=args.days)
    try:
        repository = workflow_guard.gh_api(f"repos/{args.repo}")
        if not isinstance(repository, dict) or not isinstance(repository.get("default_branch"), str):
            raise ValueError("Invalid repository response.")
        default_branch = repository["default_branch"]
        pulls = merged_pull_requests(args.repo, default_branch)
        status_rows = []
        missing_rows = []
        for pull in pulls:
            merged_value = pull.get("merged_at")
            if merged_value is None:
                continue
            merged_at = parse_timestamp(merged_value)
            if not start <= merged_at <= now:
                continue
            number = pull["number"]
            paths, head_sha = workflow_guard.read_pull_request(args.repo, number, default_branch)
            protected = workflow_guard.protected_paths(paths)
            if not protected:
                continue
            label_names = {item["name"] for item in pull["labels"]}
            approval = approval_record(args.repo, head_sha, number)
            row = (
                number,
                pull["title"],
                merged_at.isoformat().replace("+00:00", "Z"),
                safe_json(protected),
                approval[0] if approval is not None else "no",
                approval[1] if approval is not None else "none",
                "yes" if args.label in label_names else "no",
            )
            (status_rows if approval is not None else missing_rows).append(row)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("Could not build a complete workflow approval digest.", file=sys.stderr)
        return 2

    begin = start.date().isoformat()
    end = now.date().isoformat()
    print(f"# Workflow approval digest: {begin} to {end} UTC")
    success_count = sum(row[4] == "yes" for row in status_rows)
    print(
        f"\n{success_count} with a success approval status; {len(status_rows) - success_count} with a non-success status; "
        f"{len(missing_rows)} with no approval status."
    )
    print("\n## Protected pull requests with an approval status\n")
    print(table(status_rows))
    print("\n## Merged protected pull requests with no approval status\n")
    print(table(missing_rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
