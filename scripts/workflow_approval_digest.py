#!/usr/bin/env python3
"""Print an audit digest of merged pull requests with workflow approvals."""

import argparse
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
import unicodedata
from urllib.parse import quote

# Support execution by path from outside the repository as well as package imports.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import workflow_guard

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
GUARD_START = datetime(2026, 11, 22, 3, 47, 17, tzinfo=timezone.utc)


def code_span(value: str) -> str:
    cleaned = []
    for char in value:
        if char in "\n\r\t":
            cleaned.append(" ")
        elif unicodedata.category(char) != "Cc":
            cleaned.append(char)
    text = "".join(cleaned)
    runs = re.findall(r"`+", text)
    delimiter = "`" * (max(map(len, runs), default=0) + 1)
    padding = " " if runs or text.startswith(" ") or text.endswith(" ") else ""
    text = text.replace("|", "\\|")
    return f"{delimiter}{padding}{text}{padding}{delimiter}"


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


def parse_since(value: str) -> datetime:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.combine(date.fromisoformat(value), time.min, timezone.utc)
    return parse_timestamp(value)


def merged_pull_requests(repo: str, base_branch: str) -> list[dict[str, Any]]:
    pulls = workflow_guard.paginated_entries(
        f"repos/{repo}/pulls?state=closed&base={quote(base_branch, safe='')}&sort=updated&direction=desc"
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
        if base.get("ref") != base_branch or not isinstance(labels, list):
            raise ValueError("Invalid pull request entry.")
        if merged_at is not None:
            parse_timestamp(merged_at)
        if any(not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in labels):
            raise ValueError("Invalid pull request labels.")
        result.append(pull)
    return result


def approval_record(repo: str, head_sha: str, number: int, base: str, *, allow_legacy: bool) -> tuple[str, str] | None:
    status = workflow_guard.matching_approval_status(
        repo, head_sha, number, scope=workflow_guard.base_scope(base), allow_legacy=allow_legacy
    )
    if status is None:
        return None
    created_at = status.get("created_at")
    written_at = parse_timestamp(created_at).isoformat().replace("+00:00", "Z")
    is_success = workflow_guard.is_success_approval(status)
    return ("yes" if is_success else "no", written_at)


def table(rows: list[tuple[int, str, str, str, str, str, str, str]]) -> str:
    lines = [
        "| # | Pull request | Base | Merged (UTC) | Protected files | Success approval | Status written (UTC) | Label present |",
        "| ---: | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for number, title, base, merged_at, files, approval, written_at, has_label in rows:
        # Code spans display pull request titles literally in the table.
        lines.append(
            f"| {number} | {code_span(title)} | {code_span(base)} | {merged_at} | {files} | {approval} | {written_at} | {has_label} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--since", default=GUARD_START.isoformat().replace("+00:00", "Z"), metavar="ISO-DATE")
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo) or args.days < 1:
        print("Invalid repository or day range.", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=args.days)
    try:
        since = parse_since(args.since)
        repository = workflow_guard.gh_api(f"repos/{args.repo}")
        if not isinstance(repository, dict) or not isinstance(repository.get("default_branch"), str):
            raise ValueError("Invalid repository response.")
        default_branch = repository["default_branch"]
        branches = dict.fromkeys((default_branch, workflow_guard.RELEASE_BRANCH))
        pulls = [pull for branch in branches for pull in merged_pull_requests(args.repo, branch)]
        status_rows = []
        missing_rows = []
        excluded_before_cutoff = 0
        for pull in pulls:
            merged_value = pull.get("merged_at")
            if merged_value is None:
                continue
            merged_at = parse_timestamp(merged_value)
            if not start <= merged_at <= now:
                continue
            number = pull["number"]
            base = pull["base"]["ref"]
            paths, head_sha = workflow_guard.read_pull_request(args.repo, number, base)
            protected = workflow_guard.protected_paths(paths)
            if not protected:
                continue
            if merged_at <= since:
                excluded_before_cutoff += 1
                continue
            label_names = {item["name"] for item in pull["labels"]}
            approval = approval_record(args.repo, head_sha, number, base, allow_legacy=base == default_branch)
            row = (
                number,
                pull["title"],
                base,
                merged_at.isoformat().replace("+00:00", "Z"),
                ", ".join(code_span(path) for path in protected),
                approval[0] if approval is not None else "no",
                approval[1] if approval is not None else "none",
                "yes" if workflow_guard.APPROVAL_LABEL in label_names else "no",
            )
            (status_rows if approval is not None else missing_rows).append(row)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("Could not build a complete workflow approval digest.", file=sys.stderr)
        return 2

    begin = start.date().isoformat()
    end = now.date().isoformat()
    print(f"# Workflow approval digest: {begin} to {end} UTC")
    cutoff = since.isoformat().replace("+00:00", "Z")
    print(
        f"Cutoff: {cutoff}. Excluded {excluded_before_cutoff} merged protected pull request(s) in this date range because they merged at or before the cutoff."
    )
    success_count = sum(row[5] == "yes" for row in status_rows)
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
