#!/usr/bin/env python3
"""Recompute the outcome ledger from trusted main and fail when a change disagrees (#2589).

CI runs this file from a copy of main's tree, never from the pull request, with
``python3 -I -S``. The pull request's ledger is read as data only. Library code comes
from the same trusted tree, put on ``sys.path`` below because ``-I`` ignores PYTHONPATH.

A change that does not touch ``outcome-ledger/`` passes at once. A change that does is
rebuilt from the previous ledger on trusted main, the git history of main and the
Actions API, and must match byte for byte. It may touch only the ledger file and a
restored skill bundle. Pushes to main pass, because the merge group already gated them.

Environment: GITHUB_EVENT_NAME, GITHUB_EVENT_PATH, GITHUB_REPOSITORY, GITHUB_REF_NAME and
GH_TOKEN (read access to Actions and pull requests).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brigade import ci_attestation, outcome_ledger as ledger  # noqa: E402
from brigade.outcome_ledger_git import GitError, Repo  # noqa: E402

QUEUE_REF = re.compile(r"^refs/heads/gh-readonly-queue/[^/]+/pr-(\d+)-[0-9a-f]{40}$")


def say(message: str) -> None:
    """Print one line with control characters and workflow command markers removed."""
    clean = re.sub(r"[^\x20-\x7e]", "?", message).replace("::", ":")
    print(f"ledger-recompute: {clean}")


def event_context(event_name: str, payload: dict, gh, slug: str) -> ledger.EventContext:
    number: int | None = None
    if event_name == "pull_request":
        value = (payload.get("pull_request") or {}).get("number")
        number = value if isinstance(value, int) else None
    elif event_name == "merge_group":
        match = QUEUE_REF.match(str((payload.get("merge_group") or {}).get("head_ref", "")))
        number = int(match.group(1)) if match else None
    elif event_name == "workflow_dispatch":
        branch = os.environ.get("GITHUB_REF_NAME", "")
        owner = slug.split("/", 1)[0]
        found = gh(f"repos/{slug}/pulls?state=open&head={owner}:{branch}&per_page=2") if branch else []
        if isinstance(found, list) and len(found) == 1 and isinstance(found[0].get("number"), int):
            number = found[0]["number"]
    return ledger.EventContext(event=event_name, pr_number=number)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--main-ref", default="refs/ci-trusted/main")
    parser.add_argument("--head-ref", default="HEAD")
    args = parser.parse_args(argv)
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name == "push":
        say("push to main: gated earlier by the merge group, nothing to recompute")
        return 0
    if event_name not in {"pull_request", "merge_group", "workflow_dispatch"}:
        say(f"unsupported event {event_name!r}")
        return 1
    try:
        payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        slug = os.environ["GITHUB_REPOSITORY"]
        repo = Repo(Path(args.repo))
        main_sha = repo.resolve(args.main_ref)
        head_sha = repo.resolve(args.head_ref)
        touched = repo.changed_paths(repo.merge_base(main_sha, head_sha), head_sha)
    except (KeyError, OSError, ValueError, GitError) as exc:
        say(f"cannot read the event or the checkout: {type(exc).__name__}")
        return 1
    if not any(path.startswith(ledger.LEDGER_DIR) for path in touched):
        say("the outcome ledger is untouched")
        return 0
    gh = ci_attestation.default_gh_api
    try:
        info = gh(f"repos/{slug}")
        repo_id = info["id"]
        if not isinstance(repo_id, int):
            raise TypeError("repository id")
        evidence = ledger.Evidence(repo=repo, gh=gh, slug=slug, repo_id=repo_id)
        context = event_context(event_name, payload, gh, slug)
        raw = repo.show(head_sha, ledger.LEDGER_PATH)
        if raw is None:
            say(f"{ledger.LEDGER_PATH} is missing from the proposed tree")
            return 1
        errors = ledger.verify_ledger(
            evidence,
            raw=raw,
            main_raw=repo.show(main_sha, ledger.LEDGER_PATH),
            main_ref=main_sha,
            head_ref=head_sha,
            context=context,
            now=dt.datetime.now(dt.timezone.utc),
        )
    except (ci_attestation.GhUnavailable, ci_attestation.GhNotFound, KeyError, TypeError, GitError) as exc:
        say(f"cannot read trusted evidence: {type(exc).__name__}")
        return 1
    for message in errors:
        say(message)
    if errors:
        return 1
    say("the ledger matches the recomputed ledger")
    return 0


if __name__ == "__main__":
    sys.exit(main())
