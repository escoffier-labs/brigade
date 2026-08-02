from __future__ import annotations

import argparse
import json
import subprocess
import sys

from .engine import scan_text
from .policy import Policy, load_policy
from .report import to_text
from .rev_range import git_has_head, validate_rev_range_operand


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="content-guard-commits",
        description="Scan Git commit messages before publishing or pushing.",
    )
    parser.add_argument("--policy", help="JSON policy file")
    history_source = parser.add_mutually_exclusive_group()
    history_source.add_argument(
        "--range", dest="rev_range", help="revision range to scan, for example origin/main..HEAD"
    )
    history_source.add_argument("--all", action="store_true", help="scan all reachable commits")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    args = parser.parse_args(argv)

    policy = load_policy(args.policy) if args.policy else _default_commit_policy()
    revs = _commit_revs(args)

    results = []
    blocked = False
    for rev in revs:
        message = _git(["log", "-1", "--format=%B", rev])
        result = scan_text(message, policy=policy)
        if result.findings:
            blocked = blocked or result.blocked
            results.append((rev, _subject(result.redacted_text), result))

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
                            "subject": subject,
                            "blocked": result.blocked,
                            "changed": result.changed,
                            "counts_by_action": result.counts_by_action(),
                            "counts_by_category": result.counts_by_category(),
                            "findings": [
                                {
                                    "rule_id": finding.rule_id,
                                    "category": finding.category,
                                    "action": finding.action,
                                    "line": finding.line,
                                    "column": finding.column,
                                    "source": finding.source,
                                    "message": finding.message,
                                }
                                for finding in result.findings
                            ],
                        }
                        for rev, subject, result in results
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif not results:
        print(f"Clean. {len(revs)} commit message(s) checked.")
    else:
        for rev, subject, result in results:
            label = f"commit {rev[:12]} {subject}".strip()
            print(to_text(result, path=label))

    return 1 if blocked else 0


def _default_commit_policy() -> Policy:
    return Policy(
        name="public-commit-default",
        defaults={
            "infrastructure": "block",
            "secret": "block",
            "pii": "block",
            "personal": "block",
            "business": "block",
            "attribution": "block",
            "tooling": "warn",
        },
        rules={"opf-pii": "warn"},
    )


def _commit_revs(args: argparse.Namespace) -> list[str]:
    if args.rev_range is not None:
        validate_rev_range_operand(args.rev_range)
    if args.all:
        cmd = ["rev-list", "--reverse", "--all"]
    else:
        if args.rev_range is not None:
            rev_range = args.rev_range
        else:
            if not git_has_head():
                return []
            rev_range = _default_range()
        cmd = ["rev-list", "--reverse", rev_range]

    bounded_error = "git rev-list failed" if args.rev_range is not None else None
    output = _git(cmd, bounded_error=bounded_error)
    return [line for line in output.splitlines() if line.strip()]


def _default_range() -> str:
    try:
        upstream = subprocess.run(
            ["git", "rev-parse", "--symbolic-full-name", "@{upstream}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if upstream.returncode != 0 or not upstream.stdout.strip():
            return "HEAD"

        resolved = subprocess.run(
            ["git", "rev-parse", "--verify", "@{upstream}^{commit}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        print("git command failed", file=sys.stderr)
        raise SystemExit(2) from None

    upstream_commit = resolved.stdout.strip()
    if resolved.returncode != 0 or not upstream_commit:
        print("git rev-parse failed", file=sys.stderr)
        raise SystemExit(2)
    return f"{upstream_commit}..HEAD"


def _git(args: list[str], *, bounded_error: str | None = None) -> str:
    try:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    except OSError:
        print("git command failed", file=sys.stderr)
        raise SystemExit(2) from None
    if proc.returncode != 0:
        if bounded_error is not None:
            print(bounded_error, file=sys.stderr)
            raise SystemExit(2)
        print((proc.stderr or proc.stdout or "git command failed").strip(), file=sys.stderr)
        raise SystemExit(2)
    return proc.stdout


def _subject(message: str) -> str:
    for line in message.splitlines():
        if line.strip():
            return line.strip()
    return "(empty subject)"


if __name__ == "__main__":
    raise SystemExit(main())
