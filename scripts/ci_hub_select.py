#!/usr/bin/env python3
"""Decide whether a merge group needs the Rust hub suite, from trusted inputs only.

ci.yml loads this file from the tip of main, never from the proposal, and runs
it with `python3 -I -S`. It reads the hub path list from that same main commit
and diffs the group head against its merge base with main. That range covers
every speculative queue entry ahead of this one, not only this entry's change:
`merge_group.base_sha` is the previous entry's head, so `base_sha..head_sha`
alone would miss a hub change made by an earlier entry. Any doubt selects the
suite. No dependencies beyond the standard library and git.

It also reports the start of that range (`start`), once validated, so the
documentation-only classifier in the merge group classifies the same
cumulative range. An empty `start` makes that classifier take full CI.
"""

import json
import os
from pathlib import Path
import re
import subprocess


TARGET_REF = "refs/heads/main"
PATTERN_FILE = "scripts/ci_hub_paths.yml"
PATTERN_FILE_MAX_BYTES = 16384
# Queue heads sit at most (build concurrency x group size) commits above main.
# A merge base deeper than this is not found and the suite is selected.
HISTORY_DEPTH = 256
# A change to any of these can change how the hub suite is selected, so the
# suite runs whatever the path list says.
SELECTION_SOURCES = frozenset(
    {
        ".github/workflows/ci.yml",
        ".github/workflows/hub.yml",
        ".github/workflows/hub-nightly.yml",
        "scripts/ci_hub_paths.yml",
        "scripts/ci_hub_select.py",
    }
)
SHA = re.compile(r"[0-9a-f]{40}")
PATTERN = re.compile(r"[A-Za-z0-9_./*-]+")
PATTERN_LINE = re.compile(r"  - '([^']*)'")
RAW_HEADER = re.compile(rb":[0-7]{6} [0-7]{6} [0-9a-f]{40} [0-9a-f]{40} [A-Z]")
REASONS = frozenset(
    {
        "unsupported-event",
        "unexpected-target-branch",
        "missing-or-invalid-commit",
        "checkout-head-mismatch",
        "merge-group-head-mismatch",
        "base-not-in-head",
        "no-merge-base",
        "ambiguous-merge-base",
        "merge-base-above-group-base",
        "missing-or-unsafe-path-list",
        "invalid-path-list",
        "empty-or-unreadable-diff",
        "malformed-diff",
    }
)


class Fail(ValueError):
    """Fail closed with a fixed reason."""


def git(*args: str) -> bytes:
    return subprocess.run(
        ["git", "--no-replace-objects", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120
    ).stdout


def commit_sha(value: object) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value) or value == "0" * 40:
        raise Fail("missing-or-invalid-commit")
    return value


def is_ancestor(older: str, newer: str) -> bool:
    try:
        git("merge-base", "--is-ancestor", older, newer)
    except subprocess.CalledProcessError as error:
        if error.returncode == 1:
            return False
        raise
    return True


def parse_patterns(text: str) -> tuple[str, ...]:
    """Parse the one supported shape of ci_hub_paths.yml, or refuse it."""
    patterns: list[str] = []
    seen_key = False
    for line in text.split("\n"):
        if not line.strip() or (line.startswith("#") and not seen_key):
            continue
        if not seen_key:
            if line != "hub:":
                raise Fail("invalid-path-list")
            seen_key = True
            continue
        match = PATTERN_LINE.fullmatch(line)
        if not match:
            raise Fail("invalid-path-list")
        pattern = match[1]
        segments = pattern.split("/")
        if (
            not PATTERN.fullmatch(pattern)
            or any(segment in {"", ".", ".."} for segment in segments)
            or any("**" in segment and segment != "**" for segment in segments)
        ):
            raise Fail("invalid-path-list")
        patterns.append(pattern)
    if not patterns:
        raise Fail("invalid-path-list")
    return tuple(patterns)


def _segment_matches(pattern: str, name: str) -> bool:
    # `*` stays inside one path segment and matches dot files, as picomatch
    # does with `dot: true` (the option dorny/paths-filter uses).
    return re.fullmatch(".*".join(re.escape(part) for part in pattern.split("*")), name, re.DOTALL) is not None


def _segments_match(pattern: list[str], path: list[str]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        # Zero or more whole segments. Matching the directory itself as well
        # is broader than picomatch, which only ever selects more.
        return any(_segments_match(pattern[1:], path[index:]) for index in range(len(path) + 1))
    return bool(path) and _segment_matches(pattern[0], path[0]) and _segments_match(pattern[1:], path[1:])


def matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(_segments_match(pattern.split("/"), path.split("/")) for pattern in patterns)


def load_patterns(target: str) -> tuple[str, ...]:
    entry = git("ls-tree", "-z", target, "--", PATTERN_FILE)
    match = re.fullmatch(rb"100644 blob ([0-9a-f]{40})\t" + re.escape(PATTERN_FILE.encode()) + rb"\x00", entry)
    if not match:
        raise Fail("missing-or-unsafe-path-list")
    blob = match[1].decode("ascii")
    if not 0 < int(git("cat-file", "-s", blob)) <= PATTERN_FILE_MAX_BYTES:
        raise Fail("missing-or-unsafe-path-list")
    return parse_patterns(git("cat-file", "blob", blob).decode("utf-8"))


def changed_paths(start: str, head: str) -> set[str]:
    # Same raw, rename-free diff as ci_docs_only.py: a rename shows both names.
    raw = git(
        "diff",
        "--raw",
        "-z",
        "--no-abbrev",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=none",
        start,
        head,
        "--",
    )
    if not raw or not raw.endswith(b"\0"):
        raise Fail("empty-or-unreadable-diff")
    records = raw[:-1].split(b"\0")
    if len(records) % 2:
        raise Fail("malformed-diff")
    paths = set()
    for header, path in zip(records[::2], records[1::2], strict=True):
        if not RAW_HEADER.fullmatch(header) or not path:
            raise Fail("malformed-diff")
        paths.add(path.decode("utf-8"))
    return paths


def merge_base(target: str, head: str) -> str:
    try:
        bases = git("merge-base", "--all", target, head).decode().split()
    except subprocess.CalledProcessError as error:
        # Exit 1 with no output: no common history within the fetched depth.
        if error.returncode == 1 and not error.stdout:
            raise Fail("no-merge-base") from error
        raise
    if not bases:
        raise Fail("no-merge-base")
    if len(bases) != 1:
        raise Fail("ambiguous-merge-base")
    return commit_sha(bases[0])


def select() -> tuple[bool, str, str]:
    if os.environ["GITHUB_EVENT_NAME"] != "merge_group":
        raise Fail("unsupported-event")
    group = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))["merge_group"]
    if group["base_ref"] != TARGET_REF:
        raise Fail("unexpected-target-branch")
    head = commit_sha(os.environ["GITHUB_SHA"])
    if git("rev-parse", "HEAD").decode().strip() != head:
        raise Fail("checkout-head-mismatch")
    if commit_sha(group["head_sha"]) != head:
        raise Fail("merge-group-head-mismatch")
    base = commit_sha(group["base_sha"])
    # The caller fetched refs/heads/main itself and passes the resolved commit.
    target = commit_sha(os.environ["CI_HUB_TARGET"])
    # Only validated object ids reach fetch. Deepen both tips so the merge
    # base is reachable from the shallow checkout.
    git("fetch", "--no-tags", "--no-recurse-submodules", f"--depth={HISTORY_DEPTH}", "origin", target, head, base)
    for sha in (target, head, base):
        git("cat-file", "-e", f"{sha}^{{commit}}")
    if not is_ancestor(base, head):
        raise Fail("base-not-in-head")
    start = merge_base(target, head)
    # The trusted start must sit at or below the group's own base, so the
    # range always contains base_sha..head_sha and every entry ahead of it.
    if not is_ancestor(start, base):
        raise Fail("merge-base-above-group-base")
    patterns = load_patterns(target)
    paths = changed_paths(start, head)
    print(f"hub selection: main {target}, merge base {start}, head {head}, {len(paths)} changed paths")
    if paths & SELECTION_SOURCES:
        return True, "hub-selection-source-changed", start
    if any(matches(path, patterns) for path in paths):
        return True, "hub-path-changed", start
    return False, "no-hub-path-changed", start


def main() -> None:
    # Every failure leaves `start` empty, which also fails the docs classifier closed.
    try:
        hub, reason, start = select()
    except Fail as error:
        hub, reason, start = True, str(error) if str(error) in REASONS else "trusted-hub-selection-failed", ""
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        hub, reason, start = True, "trusted-hub-selection-failed", ""
    output = f"hub={str(hub).lower()}\nreason={reason}\nstart={start}\n"
    print(output, end="")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        stream.write(output)


if __name__ == "__main__":
    main()
