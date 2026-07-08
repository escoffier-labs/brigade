from __future__ import annotations

import argparse
import json
import sys
from importlib.resources.abc import Traversable
from os import PathLike
from pathlib import Path
from typing import Any

from .git_commits import _commit_revs, _git, _subject
from .git_scan import _read_text, _tracked_paths
from .engine import scan_text
from .policy import default_policy, load_policy
from .pr_prepare import prepare_pr_body
from .report import to_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="content-guard-publish-check",
        description="Run the local publish guards for PR bodies, staged files, and commit messages.",
    )
    parser.add_argument("--pr-body", help="PR body markdown file to prepare for publishing")
    parser.add_argument("--pr-policy", help="PR policy file, default: policies/pr-draft.json near project root")
    parser.add_argument("--repo-policy", help="repo policy file, default: policies/public-repo.json near project root")
    parser.add_argument("--out-dir", help="PR body output directory, default: .content-guard/pr-drafts")
    parser.add_argument("--name", help="PR body output basename, default: PR body stem")
    parser.add_argument("--commit-range", dest="rev_range", help="commit revision range to scan")
    parser.add_argument("--all-commits", action="store_true", help="scan all reachable commit messages")
    parser.add_argument("--all-tracked", action="store_true", help="also scan all tracked files")
    parser.add_argument(
        "--include-git-config", action="store_true", help="include .git/config in file scans when present"
    )
    parser.add_argument(
        "--advisory-only", action="store_true", help="always exit zero while reporting would-fail checks"
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable combined report")
    parser.add_argument("--opf", action="store_true", help="run optional OPF backend for PR body preparation")
    parser.add_argument("--opf-bin", help="path to opf binary for PR body preparation")
    parser.add_argument("--opf-device", help="OPF device for PR body preparation")
    args = parser.parse_args(argv)

    payload, text_lines = run_publish_check(args)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("\n".join(text_lines))

    return 0 if args.advisory_only or payload["ok"] else 1


def run_publish_check(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    pr_policy_path = Path(args.pr_policy) if args.pr_policy else _default_policy_path("pr-draft.json")
    repo_policy_path = Path(args.repo_policy) if args.repo_policy else _default_policy_path("public-repo.json")
    repo_policy = load_policy(repo_policy_path)

    checks: dict[str, Any] = {}
    lines = ["Content Guard publish check"]
    publish_body_file: str | None = None

    if args.pr_body:
        pr_payload, pr_text = _prepare_pr_check(args, pr_policy_path)
        checks["pr_body"] = pr_payload
        publish_body_file = pr_payload["publish_body_file"]
        lines.append("")
        lines.append("PR body: advisory" if pr_payload["blocked"] else "PR body: clean")
        lines.append(to_text(pr_text, path=str(args.pr_body)))
        lines.append(f"publish_body_file={publish_body_file}")
    else:
        checks["pr_body"] = {"enabled": False}
        lines.append("")
        lines.append("PR body: skipped")

    staged_payload, staged_lines = _scan_file_check(
        policy=repo_policy,
        all_tracked=False,
        include_git_config=args.include_git_config,
    )
    checks["staged_files"] = staged_payload
    lines.append("")
    lines.append(_summary_line("Staged files", staged_payload))
    lines.extend(staged_lines)

    commit_payload, commit_lines = _scan_commit_check(
        policy=repo_policy,
        rev_range=args.rev_range,
        all_commits=args.all_commits,
    )
    checks["commit_messages"] = commit_payload
    lines.append("")
    lines.append(_summary_line("Commit messages", commit_payload))
    lines.extend(commit_lines)

    if args.all_tracked:
        all_payload, all_lines = _scan_file_check(
            policy=repo_policy,
            all_tracked=True,
            include_git_config=args.include_git_config,
        )
        checks["all_tracked_files"] = all_payload
        lines.append("")
        lines.append(_summary_line("All tracked files", all_payload))
        lines.extend(all_lines)
    else:
        checks["all_tracked_files"] = {"enabled": False}
        lines.append("")
        lines.append("All tracked files: skipped")

    would_fail = bool(
        checks["staged_files"].get("blocked")
        or checks["commit_messages"].get("blocked")
        or checks["all_tracked_files"].get("blocked")
    )
    blocked = bool(would_fail or checks["pr_body"].get("blocked"))
    ok = not would_fail or bool(args.advisory_only)

    payload: dict[str, Any] = {
        "ok": ok,
        "blocked": blocked,
        "would_fail": would_fail,
        "advisory_only": bool(args.advisory_only),
        "checks": checks,
    }
    if publish_body_file:
        payload["publish_body_file"] = publish_body_file

    lines.append("")
    if args.advisory_only and would_fail:
        lines.append("Result: advisory-only, publish blockers found")
    elif ok:
        lines.append("Result: pass")
    else:
        lines.append("Result: fail")

    return payload, lines


def _prepare_pr_check(
    args: argparse.Namespace, policy_path: Path | PathLike[str] | Traversable
) -> tuple[dict[str, Any], Any]:
    source = Path(args.pr_body)
    out_dir = Path(args.out_dir) if args.out_dir else Path(".content-guard") / "pr-drafts"
    payload, result = prepare_pr_body(
        source.read_text(),
        source=source,
        policy_path=policy_path,
        out_dir=out_dir,
        name=args.name or source.stem,
        strict=False,
        include_opf=args.opf,
        opf_bin=args.opf_bin,
        opf_device=args.opf_device,
    )
    payload["enabled"] = True
    return payload, result


def _scan_file_check(*, policy: Any, all_tracked: bool, include_git_config: bool) -> tuple[dict[str, Any], list[str]]:
    paths = _tracked_paths(all_tracked=all_tracked)
    if include_git_config and Path(".git/config").is_file():
        paths.append(Path(".git/config"))

    files = []
    blocked = False
    lines: list[str] = []
    for path in paths:
        text = _read_text(path)
        if text is None:
            continue
        result = scan_text(text, policy=policy)
        if not result.findings:
            continue
        blocked = blocked or result.blocked
        files.append(
            {
                "path": str(path),
                "blocked": result.blocked,
                "changed": result.changed,
                "counts_by_action": result.counts_by_action(),
                "counts_by_category": result.counts_by_category(),
            }
        )
        lines.append(to_text(result, path=str(path)))

    return (
        {
            "enabled": True,
            "mode": "all-tracked" if all_tracked else "staged",
            "ok": not blocked,
            "blocked": blocked,
            "files_scanned": len(paths),
            "files_with_findings": len(files),
            "files": files,
        },
        lines or [f"Clean. {len(paths)} Git file(s) checked."],
    )


def _scan_commit_check(*, policy: Any, rev_range: str | None, all_commits: bool) -> tuple[dict[str, Any], list[str]]:
    revs = _commit_revs(argparse.Namespace(rev_range=rev_range, all=all_commits))

    commits = []
    blocked = False
    lines: list[str] = []
    for rev in revs:
        message = _git(["log", "-1", "--format=%B", rev])
        result = scan_text(message, policy=policy)
        if not result.findings:
            continue
        blocked = blocked or result.blocked
        subject = _subject(result.redacted_text)
        commits.append(
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
        )
        label = f"commit {rev[:12]} {subject}".strip()
        lines.append(to_text(result, path=label))

    return (
        {
            "enabled": True,
            "ok": not blocked,
            "blocked": blocked,
            "commits_scanned": len(revs),
            "commits_with_findings": len(commits),
            "commits": commits,
        },
        lines or [f"Clean. {len(revs)} commit message(s) checked."],
    )


def _summary_line(label: str, payload: dict[str, Any]) -> str:
    if payload.get("blocked"):
        return f"{label}: blocked"
    return f"{label}: clean"


def _default_policy_path(name: str) -> Path | PathLike[str] | Traversable:
    return default_policy(name)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
