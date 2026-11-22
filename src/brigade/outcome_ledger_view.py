"""Read-only display of the committed outcome ledger."""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import ci_attestation, outcome_ledger as ledger, outcome_ledger_plan as planner
from .outcome_ledger_actions import EvidenceError
from .outcome_ledger_git import GitError, Repo, is_sha
from .work_cmd.brief_values import name

ORIGIN_MAIN = "refs/remotes/origin/main"
APPROVAL_HEAD = re.compile(r"ledger/approve/[a-z0-9][a-z0-9_-]*-[0-9a-f]{12}")


def _subject_trials(body: Mapping[str, Any]) -> tuple[int, int]:
    fingerprint = body["current"]["fingerprint"]
    rows = [row for row in body["trials"] if row["fingerprint"] == fingerprint]
    return sum(row["outcome"] == "helped" for row in rows), sum(row["outcome"] == "hurt" for row in rows)


def brief_line(target: Path) -> str:
    try:
        repo = Repo(target)
        raw = repo.show(repo.resolve("HEAD"), ledger.LEDGER_PATH)
        if raw is None:
            return "ledger: no tracked outcome ledger"
        payload = ledger.parse_ledger(raw)
        subjects = []
        for key, body in sorted(payload["subjects"].items())[:3]:
            helped, hurt = _subject_trials(body)
            counts = f"{helped} helped" + (f", {hurt} hurt" if hurt else "")
            subjects.append(f"{name(key.removeprefix('skill:'))} {name(body['status'])} ({counts})")
        remaining = len(payload["subjects"]) - len(subjects)
        if remaining:
            subjects.append(f"{remaining} more")
        awaiting = planner.approvals(payload)
        if awaiting:
            url = planner.approval_search(planner.REPO_SLUG, awaiting[0].branch)
            subjects.append(f"{len(awaiting)} awaiting owner ({url})")
        return "ledger: " + ", ".join(subjects)
    except GitError:
        return "ledger: no tracked outcome ledger"
    except (ledger.LedgerError, ValueError, TypeError, KeyError, AttributeError):
        return "ledger: tracked outcome ledger is unreadable"


def _text(
    payload: dict[str, Any] | None,
    links: dict[str, str],
    main: str,
    *,
    decision: str | None = None,
) -> None:
    print(f"ledger for main at {main[:12]}")
    if decision:
        print(name(decision, max_len=400))
    if payload is None:
        print("No proposal." if decision else "No ledger on main.")
    for key, body in sorted((payload or {}).get("subjects", {}).items()):
        helped, hurt = _subject_trials(body)
        print(f"{name(key)} {name(body['status'])} ({helped} helped, {hurt} hurt for the current fingerprint)")
        if decision:
            verdict = body["decision"]
            print(f"  {name(verdict['action'])}: {name(verdict['reason'])}")
            if body["restore"]:
                print(f"  restore {name(body['restore']['to_fingerprint'])} from {name(body['restore']['to_commit'])}")
        for row in body["approved"]:
            basis = row["basis"]
            if basis == "owner_review":
                basis = f"{basis}, PR #{row['pr']}"
            print(f"  approved {name(row['fingerprint'])} at {name(row['commit'])} ({name(basis)})")
        for row in body["trials"]:
            detail = f" ({name(row['reason'])})" if row.get("reason") else ""
            print(f"  trial {name(row['sha'][:12])} {name(row['fingerprint'][:12])} {name(row['outcome'])}{detail}")
    for branch, url in sorted(links.items()):
        print(f"open approval {name(branch)} {name(url, max_len=240)}")


def _remote_main(slug: str) -> str:
    ref = ci_attestation.default_gh_api(f"repos/{slug}/git/ref/heads/main")
    obj = ref.get("object") if isinstance(ref, dict) else None
    if not isinstance(obj, dict) or not is_sha(obj.get("sha")):
        raise EvidenceError("GitHub main ref is unreadable")
    return obj["sha"]


def _main_ledger(repo: Repo, sha: str, slug: str | None) -> bytes | None:
    try:
        repo.resolve(sha)
    except GitError:
        if slug is None:
            raise
        try:
            item = ci_attestation.default_gh_api(f"repos/{slug}/contents/{ledger.LEDGER_PATH}?ref={sha}")
        except ci_attestation.GhNotFound:
            return None
        if not isinstance(item, dict) or item.get("encoding") != "base64" or not isinstance(item.get("content"), str):
            raise EvidenceError("GitHub ledger contents are unreadable") from None
        return base64.b64decode("".join(item["content"].splitlines()), validate=True)
    return repo.show(sha, ledger.LEDGER_PATH)


def _approval_links(slug: str) -> dict[str, str]:
    """List all open approval branches, including earlier fingerprints still on GitHub."""
    owner = slug.split("/", 1)[0]
    links: dict[str, str] = {}
    page_size = 100
    for page in range(1, 11):
        endpoint = f"repos/{slug}/pulls?state=open&base=main&per_page={page_size}&page={page}"
        rows = ci_attestation.default_gh_api(endpoint)
        if not isinstance(rows, list):
            raise EvidenceError("open pull request listing is unreadable")
        for row in rows:
            head = row.get("head") if isinstance(row, dict) else None
            if not isinstance(head, dict):
                continue
            branch = head.get("ref")
            if not isinstance(branch, str) or not APPROVAL_HEAD.fullmatch(branch):
                continue
            if head.get("label") == f"{owner}:{branch}" and type(row.get("number")) is int:
                links[branch] = f"https://github.com/{slug}/pull/{row['number']}"
        if len(rows) < page_size:
            return links
    raise EvidenceError("open pull request listing exceeds the display limit")


def display(target: Path, *, json_output: bool, dry_run: bool) -> int:
    repo = Repo(target.expanduser().resolve())
    slug = ci_attestation.repo_slug(repo.path)
    try:
        if dry_run:
            if slug is None:
                raise EvidenceError("a GitHub origin is required for an Actions dry run")
            main = _remote_main(slug)
            try:
                repo.resolve(main)
            except GitError:
                raise EvidenceError(
                    "GitHub main is missing locally. Fetch main and its tags before planning."
                ) from None
            ev = planner.evidence(repo, ci_attestation.default_gh_api, slug)
            plan = planner.main_plan(ev, main, dt.datetime.now(dt.timezone.utc))
            output = dataclasses.asdict(plan)
            output["approval_branches"] = [item.branch for item in planner.approvals(plan.proposed or {"subjects": {}})]
            if json_output:
                print(json.dumps(output, indent=2))
            else:
                _text(plan.proposed, {}, main, decision=plan.reason)
                if plan.state == "equal":
                    for branch in output["approval_branches"]:
                        print(f"  owner approval branch {branch}")
            return 0
        cached = False
        try:
            main = _remote_main(slug) if slug is not None else repo.resolve(ORIGIN_MAIN)
        except (ci_attestation.GhUnavailable, ci_attestation.GhNotFound):
            main, cached = repo.resolve(ORIGIN_MAIN), True
        raw = _main_ledger(repo, main, slug)
        payload = ledger.parse_ledger(raw) if raw is not None else None
        try:
            links = _approval_links(slug) if slug is not None else {}
            links_available = slug is not None
        except (ci_attestation.GhUnavailable, ci_attestation.GhNotFound, EvidenceError):
            links = {}
            links_available = False
        if json_output:
            print(
                json.dumps(
                    {
                        "main": main,
                        "cached": cached,
                        "ledger": payload,
                        "open_approval_prs": links if links_available else None,
                    },
                    indent=2,
                )
            )
        else:
            _text(payload, links, main)
            if cached:
                print("Showing the cached origin/main ref. GitHub main was unavailable.")
            if not links_available:
                print("Open approval PR status is unavailable.")
    except (
        GitError,
        ledger.LedgerError,
        EvidenceError,
        ci_attestation.GhUnavailable,
        ci_attestation.GhNotFound,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ) as exc:
        print(f"ledger: {name(str(exc), max_len=400)}", file=sys.stderr)
        return 1
    return 0
