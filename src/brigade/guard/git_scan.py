from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .engine import scan_text
from .policy import Policy, load_policy
from .report import to_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="content-guard-git",
        description="Scan public Git repository content before commit or push.",
    )
    parser.add_argument("--policy", help="JSON policy file")
    parser.add_argument(
        "--allow-values-from",
        dest="allow_values_from",
        action="append",
        metavar="PATH",
        help=(
            "merge the allow_values list from another JSON policy file into the "
            "active policy. Use to apply a private allowlist (e.g. a known-public "
            "email or example port) to a scan without putting those literals in a "
            "shipped public policy. Repeatable."
        ),
    )
    parser.add_argument("--all-tracked", action="store_true", help="scan all tracked files")
    parser.add_argument("--staged", action="store_true", help="scan staged files, default mode")
    parser.add_argument("--include-git-config", action="store_true", help="also scan .git/config when present")
    parser.add_argument(
        "--history",
        action="store_true",
        help="scan content INTRODUCED across commit history (added lines), not just the current tip",
    )
    parser.add_argument("--range", dest="rev_range", help="revision range for --history, e.g. origin/main..HEAD")
    parser.add_argument("--all", action="store_true", help="with --history, scan all reachable commits")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    args = parser.parse_args(argv)

    policy = load_policy(args.policy) if args.policy else _default_repo_policy()
    _merge_allow_values_from(policy, args.allow_values_from or [])

    if args.history:
        return _scan_history(policy, args)

    paths = _tracked_paths(all_tracked=args.all_tracked)
    if args.include_git_config and Path(".git/config").is_file():
        paths.append(Path(".git/config"))

    results = []
    blocked = False
    for path in paths:
        text = _read_text(path)
        if text is None:
            continue
        result = scan_text(text, policy=policy)
        if result.findings:
            blocked = blocked or result.blocked
            results.append((path, result))

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not blocked,
                    "blocked": blocked,
                    "files_with_findings": len(results),
                    "files": [
                        {
                            "path": str(path),
                            "blocked": result.blocked,
                            "changed": result.changed,
                            "counts_by_action": result.counts_by_action(),
                            "counts_by_category": result.counts_by_category(),
                        }
                        for path, result in results
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif not results:
        print(f"Clean. {len(paths)} Git file(s) checked.")
    else:
        for path, result in results:
            print(to_text(result, path=str(path)))

    return 1 if blocked else 0


def _scan_history(policy: Policy, args: argparse.Namespace) -> int:
    """Scan the content INTRODUCED by each commit (added lines).

    Closes the forward-scrub gap: a later commit can clean the tip while the
    leak survives in the commit that originally introduced it. We scan added
    lines per commit, so a leak is caught at its point of introduction even if
    a subsequent commit removed it from the working tree.
    """
    revs = _history_revs(args)
    results = []
    blocked = False
    for rev in revs:
        text = _added_lines(rev)
        if not text:
            continue
        result = scan_text(text, policy=policy)
        if result.findings:
            blocked = blocked or result.blocked
            results.append((rev, result))

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not blocked,
                    "blocked": blocked,
                    "commits_scanned": len(revs),
                    "commits_with_findings": len(results),
                    "commits": [
                        {
                            "commit": rev,
                            "blocked": result.blocked,
                            "counts_by_action": result.counts_by_action(),
                            "counts_by_category": result.counts_by_category(),
                        }
                        for rev, result in results
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif not results:
        print(f"Clean. introduced content of {len(revs)} commit(s) checked.")
    else:
        for rev, result in results:
            print(to_text(result, path=f"commit {rev[:12]} (introduced content)"))

    return 1 if blocked else 0


def _history_revs(args: argparse.Namespace) -> list[str]:
    if args.all:
        cmd = ["git", "rev-list", "--all"]
    elif args.rev_range:
        cmd = ["git", "rev-list", args.rev_range]
    else:
        cmd = ["git", "rev-list", "HEAD"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print((proc.stderr or "git rev-list failed").strip(), file=sys.stderr)
        raise SystemExit(2)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _added_lines(rev: str) -> str:
    proc = subprocess.run(
        ["git", "show", "--no-color", "--first-parent", "--format=", "--unified=0", rev],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    out = []
    for line in proc.stdout.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return "\n".join(out)


def _merge_allow_values_from(policy: Policy, paths: list[str]) -> None:
    """Extend ``policy.allow_values`` with allow_values from extra policy files.

    Lets a private allowlist file (kept out of any shipped public policy) apply
    its known-public literals to a scan that uses a different main policy.
    Missing files are skipped quietly so an optional private file is never a
    hard error; malformed files surface through ``load_policy``.
    """
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            # Fail-safe: a missing allowlist file never weakens a scan (nothing
            # extra is allowed). Warn so an explicit CLI typo is not silent; the
            # pre-push hook only passes its private file when it exists.
            print(f"content-guard: allow-values file not found, skipping: {path}", file=sys.stderr)
            continue
        extra = load_policy(path)
        for value in extra.allow_values:
            if value not in policy.allow_values:
                policy.allow_values.append(value)


def _default_repo_policy() -> Policy:
    return Policy(
        name="public-repo-default",
        defaults={
            "infrastructure": "block",
            "secret": "block",
            "pii": "block",
            "personal": "block",
            "business": "block",
            "attribution": "block",
            "tooling": "warn",
        },
        rules={
            "loopback-ipv4": "warn",
            "localhost-port": "warn",
            "localhost-bare": "warn",
            "port-reference": "warn",
            "opf-pii": "warn",
        },
    )


def _tracked_paths(*, all_tracked: bool, cwd: Path | None = None) -> list[Path]:
    if all_tracked:
        cmd = ["git", "ls-files"]
    else:
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=cwd)
    if proc.returncode != 0:
        print((proc.stderr or proc.stdout or "git command failed").strip(), file=sys.stderr)
        raise SystemExit(2)

    return [Path(line) for line in proc.stdout.splitlines() if line.strip()]


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
