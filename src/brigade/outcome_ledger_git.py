"""Read-only git access for the outcome ledger (#2589).

Every read goes through ``git --no-replace-objects`` against a commit id, so a
working tree edit never changes what the ledger sees. Nothing here writes.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TypeGuard

from . import outcome_discovery

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
GIT_TIMEOUT_SECONDS = 120


class GitError(Exception):
    """A git read failed (missing object, missing path, bad ref)."""


def is_sha(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and SHA_RE.match(value) is not None


class Repo:
    """A checkout the ledger reads commits from."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed git argv, no shell
                ["git", "--no-replace-objects", "-C", str(self.path), *args],
                capture_output=True,
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitError(f"git {args[0]}: {type(exc).__name__}") from exc
        if check and completed.returncode != 0:
            raise GitError(f"git {args[0]} failed: {completed.stderr.decode('utf-8', 'replace').strip()[:200]}")
        return completed

    def resolve(self, ref: str) -> str:
        sha = self._run("rev-parse", "--verify", f"{ref}^{{commit}}").stdout.decode("ascii").strip()
        if not is_sha(sha):
            raise GitError(f"not a commit: {ref}")
        return sha

    def show(self, commit: str, path: str) -> bytes | None:
        """Bytes of ``path`` at ``commit``, or None when the path is absent."""
        completed = self._run("cat-file", "blob", f"{commit}:{path}", check=False)
        return completed.stdout if completed.returncode == 0 else None

    def snapshot(self, commit: str, directory: str) -> dict[str, bytes]:
        """Every file under ``directory`` at ``commit``, keyed by path relative to it."""
        listing = self._run("ls-tree", "-r", "-z", "--full-tree", commit, "--", directory.rstrip("/") + "/")
        files: dict[str, bytes] = {}
        prefix = directory.rstrip("/") + "/"
        for entry in listing.stdout.split(b"\0"):
            if not entry:
                continue
            meta, _, raw_path = entry.partition(b"\t")
            mode, kind, _sha = meta.decode("ascii").split()
            path = raw_path.decode("utf-8")
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise GitError(f"unsupported entry in skill bundle: {path}")
            data = self.show(commit, path)
            if data is None:
                raise GitError(f"unreadable path: {path}")
            files[path.removeprefix(prefix)] = data
        return files

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self._run("merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0

    def commit_time(self, commit: str) -> str:
        """Committer time of ``commit`` as UTC ``YYYY-MM-DDTHH:MM:SSZ``."""
        epoch = int(self._run("show", "-s", "--format=%ct", commit).stdout.decode("ascii").strip())
        return _utc_text(epoch)

    def first_parent(self, since_exclusive: str, tip: str) -> list[str]:
        """Commits on the first-parent chain ``since..tip``, oldest first. ``tip`` is included."""
        listing = self._run("rev-list", "--first-parent", "--reverse", f"{since_exclusive}..{tip}").stdout
        return [line for line in listing.decode("ascii").split() if is_sha(line)]

    def changed_paths(self, base: str, head: str) -> list[str]:
        out = self._run("diff", "--name-only", "--no-renames", "-z", base, head).stdout
        return [part.decode("utf-8") for part in out.split(b"\0") if part]

    def merge_base(self, left: str, right: str) -> str:
        return self._run("merge-base", left, right).stdout.decode("ascii").strip()


def _utc_text(epoch: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bundle_fingerprint(files: dict[str, bytes]) -> str | None:
    """The skill bundle fingerprint over a snapshot, equal to ``SubjectRef.content_fingerprint``."""
    return outcome_discovery._bundle_fingerprint_from_files(
        {tuple(path.split("/")): data for path, data in files.items()}
    )
