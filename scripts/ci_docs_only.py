#!/usr/bin/env python3
"""Conservatively classify the complete GitHub CI event diff, without dependencies.

In a merge group, ci.yml loads this file from the tip of main and passes
CI_TRUSTED_MAIN (that main commit) and CI_MERGE_GROUP_START: the merge base of
main and the group head, which the trusted hub selector (scripts/ci_hub_select.py)
validated. `merge_group.base_sha` is the previous queue entry's head, so
`base_sha..head_sha` would miss every entry queued ahead of this one. This file
re-checks the start itself: it must be in main's history and below `base_sha`,
so the range holds every unmerged entry. Pull requests and pushes diff from
their own base as before.
"""

import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess


# These are prose entry points, not harness/policy configuration. Other root
# Markdown (notably AGENTS.md and CLAUDE.md) deliberately takes the full path.
ROOT_DOCS = {
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "GOVERNANCE.md",
    "ROADMAP.md",
    "QUICKSTART.md",
    "RELEASE.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
}
# SVG can contain executable content. Only raster images are documentation here.
DOC_SUFFIXES = {".md", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
POLICY_NAMES = {"AGENTS.md", "CLAUDE.md", "SKILL.md"}
SHA = re.compile(r"[0-9a-f]{40}")
TARGET_REF = "refs/heads/main"


def git(*args: str) -> bytes:
    return subprocess.run(
        ["git", "--no-replace-objects", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60
    ).stdout


def commit_sha(value: object) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value) or value == "0" * 40:
        raise ValueError("missing-or-invalid-commit")
    return value


def is_ancestor(older: str, newer: str) -> bool:
    try:
        git("merge-base", "--is-ancestor", older, newer)
    except subprocess.CalledProcessError as error:
        if error.returncode == 1:
            return False
        raise
    return True


def ensure_commit(sha: str) -> None:
    try:
        git("cat-file", "-e", f"{sha}^{{commit}}")
    except subprocess.CalledProcessError:
        # Only a validated object id reaches fetch, never an event-supplied ref
        # or shell expression. No history walk or unbounded deepening loop.
        git("fetch", "--no-tags", "--no-recurse-submodules", "--depth=1", "origin", sha)
        git("cat-file", "-e", f"{sha}^{{commit}}")


def documentation_path(path: bytes) -> bool:
    name = path.decode("utf-8")
    if name in ROOT_DOCS:
        return True
    parts = PurePosixPath(name).parts
    # A changelog fragment is inert prose that every PR must add, so it must not
    # turn a documentation PR into a full CI run. Exactly one level, no dotfiles.
    if len(parts) == 2 and parts[0] == "changelog.d" and parts[1].endswith(".md") and not parts[1].startswith("."):
        return True
    return (
        len(parts) > 1
        and parts[0] == "docs"
        and all(not part.startswith(".") for part in parts)
        and parts[-1] not in POLICY_NAMES
        and PurePosixPath(name).suffix in DOC_SUFFIXES
    )


def classify() -> tuple[bool, bool, str]:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        head = commit_sha(os.environ["GITHUB_SHA"])
        if git("rev-parse", "HEAD").decode().strip() != head:
            raise ValueError("checkout-head-mismatch")
        event_name = os.environ["GITHUB_EVENT_NAME"]
        if event_name == "pull_request":
            pr_head = commit_sha(event["pull_request"]["head"]["sha"])
            # checkout fetch-depth: 2 supplies both parents of the test merge.
            parents = git("show", "-s", "--format=%P", head).decode().split()
            if len(parents) != 2 or parents[1] != pr_head:
                raise ValueError("not-the-pull-request-test-merge")
            base = commit_sha(parents[0])
        elif event_name == "merge_group":
            group = event["merge_group"]
            if group["base_ref"] != TARGET_REF:
                raise ValueError("unexpected-target-branch")
            if commit_sha(group["head_sha"]) != head:
                raise ValueError("merge-group-head-mismatch")
            group_base = commit_sha(group["base_sha"])
            trusted_main = os.environ.get("CI_TRUSTED_MAIN", "")
            if not SHA.fullmatch(trusted_main):
                raise ValueError("missing-trusted-main")
            trusted_main = commit_sha(trusted_main)
            start = os.environ.get("CI_MERGE_GROUP_START", "")
            if not SHA.fullmatch(start):
                raise ValueError("missing-cumulative-range")
            base = commit_sha(start)
            ensure_commit(trusted_main)
            ensure_commit(group_base)
            ensure_commit(base)
            # A start in main's history leaves every unmerged entry ahead of this
            # one inside start..head; a start at or below base_sha keeps this
            # entry's own change in it. `start == base_sha` with base_sha not yet
            # on main (the original non-cumulative range) fails the first check.
            if not (
                is_ancestor(base, trusted_main) and is_ancestor(base, group_base) and is_ancestor(group_base, head)
            ):
                raise ValueError("cumulative-range-not-below-group-base")
        elif event_name == "push":
            base = commit_sha(event["before"])
            if commit_sha(event["after"]) != head:
                raise ValueError("push-head-mismatch")
        else:
            raise ValueError("unsupported-event")
        ensure_commit(base)
        ensure_commit(head)
        # Disabling rename detection exposes both old and new paths as D/A.
        # Raw NUL records retain whitespace and file modes, including deletions
        # of symlinks/executables. No path is passed back to a Git command.
        raw = git(
            "diff",
            "--raw",
            "-z",
            "--no-abbrev",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
            base,
            head,
            "--",
        )
        if not raw or not raw.endswith(b"\0"):
            raise ValueError("empty-or-unreadable-diff")
        records = raw[:-1].split(b"\0")
        if len(records) % 2:
            raise ValueError("malformed-diff")
        docs_only = True
        for header, path in zip(records[::2], records[1::2], strict=True):
            match = re.fullmatch(rb":([0-7]{6}) ([0-7]{6}) [0-9a-f]{40} [0-9a-f]{40} ([A-Z])", header)
            if not match or not path:
                raise ValueError("malformed-diff")
            old_mode, new_mode, status = match.groups()
            if status not in {b"A", b"D", b"M"} or {old_mode, new_mode} - {b"000000", b"100644"}:
                docs_only = False
            if not documentation_path(path):
                docs_only = False
        return docs_only, False, "documentation-only" if docs_only else "non-documentation-change"
    except ValueError as error:
        # Reasons are fixed strings, never untrusted paths/metadata in output.
        reason = (
            str(error)
            if str(error)
            in {
                "missing-or-invalid-commit",
                "checkout-head-mismatch",
                "not-the-pull-request-test-merge",
                "merge-group-head-mismatch",
                "unexpected-target-branch",
                "missing-trusted-main",
                "missing-cumulative-range",
                "cumulative-range-not-below-group-base",
                "push-head-mismatch",
                "unsupported-event",
                "empty-or-unreadable-diff",
                "malformed-diff",
            }
            else "invalid-event-or-path"
        )
        return False, True, reason
    except (KeyError, TypeError, OSError, subprocess.SubprocessError):
        return False, True, "unavailable-event-or-git-diff"


def main() -> None:
    docs_only, uncertain, reason = classify()
    output = f"docs_only={str(docs_only).lower()}\nuncertain={str(uncertain).lower()}\nreason={reason}\n"
    print(output, end="")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        stream.write(output)


if __name__ == "__main__":
    main()
