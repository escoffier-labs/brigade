#!/usr/bin/env python3
"""Fail when the Git index tracks a file that the repository's .gitignore rules ignore.

``git add -f`` (or a rule added after the file was tracked) lets a path that the
repository has declared private (local handoffs, model ratings, raw review
notes) ride along into a public push. Only ``.gitignore`` files inside the
repository count: the global ``core.excludesFile`` and ``.git/info/exclude``
are machine-local and must not be able to hide or invent a finding.

Exit status: 0 clean, 1 ignored files are tracked, 2 git could not answer.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys


class GitError(RuntimeError):
    """git failed or produced output this script cannot trust."""


def _git(root: pathlib.Path, *args: str, stdin: bytes = b"", ok: tuple[int, ...] = (0,)) -> bytes:
    cmd = ["git", "-c", f"core.excludesFile={os.devnull}", *args]
    try:
        proc = subprocess.run(cmd, cwd=root, input=stdin, capture_output=True, check=False)
    except OSError as exc:  # git missing or not executable
        raise GitError(f"{' '.join(cmd[:1] + list(args))}: {exc}") from exc
    if proc.returncode not in ok:
        detail = proc.stderr.decode("utf-8", "replace").strip() or f"exit status {proc.returncode}"
        raise GitError(f"git {' '.join(args)}: {detail}")
    return proc.stdout


def _is_repo_gitignore(source: str) -> bool:
    # Per-directory sources are reported relative to the repository root; the
    # global excludes file and .git/info/exclude never end in ``/.gitignore``
    # at a relative path, and ``-c core.excludesFile`` above already blanks
    # the former.
    return not os.path.isabs(source) and (source == ".gitignore" or source.endswith("/.gitignore"))


def tracked_ignored_files(root: pathlib.Path) -> tuple[int, list[str]]:
    """Return (tracked file count, sorted tracked paths matched by a repo .gitignore rule)."""
    tracked = _git(root, "ls-files", "-z", "--cached")
    if not tracked:
        return 0, []
    # ``-v`` reports the deciding pattern, so a trailing ``!`` re-include can be
    # told apart from a real ignore; exit 1 just means nothing matched.
    report = _git(root, "check-ignore", "-z", "-v", "--no-index", "--stdin", stdin=tracked, ok=(0, 1))
    fields = report.split(b"\0")
    if fields.pop() != b"" or len(fields) % 4:
        raise GitError("git check-ignore: malformed -z output")
    offenders: set[str] = set()
    for i in range(0, len(fields), 4):
        source, _line, pattern, path = (f.decode("utf-8", "surrogateescape") for f in fields[i : i + 4])
        if _is_repo_gitignore(source) and not pattern.startswith("!"):
            offenders.add(path)
    return tracked.count(b"\0"), sorted(offenders)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        count, offenders = tracked_ignored_files(args.root)
    except GitError as exc:
        print(f"check_public_files: cannot inspect the index; failing closed: {exc}", file=sys.stderr)
        return 2
    if not offenders:
        print(f"check_public_files: {count} tracked files, none ignored by repository .gitignore rules")
        return 0
    print(
        f"check_public_files: {len(offenders)} tracked file(s) are ignored by repository .gitignore rules:",
        file=sys.stderr,
    )
    for path in offenders:
        print(f"  {path}", file=sys.stderr)
    print(
        "Untrack them while keeping the local copies, then commit:\n"
        "  git rm --cached -- <path>...\n"
        "If a path is meant to be public, add a `!` re-include rule to .gitignore instead.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
