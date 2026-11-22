"""Read-only planning for the scheduled outcome ledger writer and its local dry run."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal
from urllib.parse import urlencode

from . import outcome_ledger as ledger, shipped_history
from .outcome_ledger_actions import EvidenceError, GhApi
from .outcome_ledger_git import Repo, bundle_fingerprint, is_sha

AUTO_BRANCH = "ledger/auto"
NOTE_MANIFEST = "skill-note"
NOTE_PATH = "src/brigade/templates/skills/note/SKILL.md"
NOTE_KEY = "skill:note"
REPO_SLUG = "escoffier-labs/brigade"

PlanState = Literal["update", "equal", "pending", "stale"]
BranchAction = Literal["create", "update", "queued", "closed"]


@dataclass(frozen=True)
class Plan:
    main: str
    state: PlanState
    reason: str
    proposed: dict[str, Any] | None = None


@dataclass(frozen=True)
class Approval:
    key: str
    fingerprint: str

    @property
    def branch(self) -> str:
        return approval_branch(self.key, self.fingerprint)


def approval_branch(key: str, fingerprint: str) -> str:
    if not re.fullmatch(r"skill:[a-z0-9][a-z0-9_-]*", key):
        raise ledger.LedgerError(f"subject {key!r} cannot be encoded in an approval branch")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ledger.LedgerError(f"subject {key!r} has an invalid approval fingerprint")
    return f"ledger/approve/{key.removeprefix('skill:')}-{fingerprint[:12]}"


def approvals(proposed: Mapping[str, Any]) -> list[Approval]:
    return [
        Approval(key, body["current"]["fingerprint"])
        for key, body in sorted(proposed["subjects"].items())
        if body.get("decision", {}).get("code") == "awaiting_owner"
    ]


def branch_action(branch: str, states: Sequence[str], *, queued: bool) -> BranchAction:
    if queued:
        return "queued"
    if branch != AUTO_BRANCH and any(state in {"CLOSED", "MERGED"} for state in states):
        return "closed"
    return "update" if "OPEN" in states else "create"


def branch_prs(gh: GhApi, slug: str, branch: str) -> list[dict[str, Any]]:
    """Exact same-repository heads, including rejected approvals."""
    owner = slug.split("/", 1)[0]
    state = "open" if branch == AUTO_BRANCH else "all"
    query = urlencode({"state": state, "base": "main", "head": f"{owner}:{branch}", "per_page": 100})
    rows = gh(f"repos/{slug}/pulls?{query}")
    if not isinstance(rows, list) or len(rows) >= 100:
        raise EvidenceError("pull request listing is unreadable or incomplete")
    found = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or type(row.get("number")) is not int
            or row.get("state") not in {"open", "closed"}
        ):
            raise EvidenceError("pull request listing has an unreadable row")
        head = row.get("head")
        if not isinstance(head, dict) or not is_sha(head.get("sha")):
            raise EvidenceError("pull request head is unreadable")
        if head.get("ref") != branch or head.get("label") != f"{owner}:{branch}":
            raise EvidenceError("pull request listing returned a different head")
        found.append(row)
    if sum(row["state"] == "open" for row in found) > 1:
        raise EvidenceError("multiple open pull requests for one ledger branch")
    return found


def evidence(repo: Repo, gh: GhApi, slug: str) -> ledger.Evidence:
    gh = ledger._memoized(gh)
    info = gh(f"repos/{slug}")
    if not isinstance(info, dict) or type(info.get("id")) is not int:
        raise EvidenceError("repository id is unreadable")
    return ledger.Evidence(repo=repo, gh=gh, slug=slug, repo_id=info["id"])


def _genesis_definition(repo: Repo, main: str) -> ledger.SubjectDef:
    """Seed note from a tagged release and start trials at the manifest introduction."""
    introduced = (
        repo._run(
            "log",
            "--first-parent",
            "--diff-filter=A",
            "-1",
            "--format=%H",
            main,
            "--",
            f"{ledger.MANIFESTS_DIR}/{NOTE_MANIFEST}.json",
        )
        .stdout.decode("ascii")
        .strip()
    )
    if not is_sha(introduced):
        raise ledger.LedgerError("note manifest has no introduction on main")
    since = repo.resolve(f"{introduced}^1")
    shipped = shipped_history.parse_manifest(repo.show(main, ledger.SHIPPED_HISTORY_PATH) or b"")
    released = {row.sha256 for row in shipped.get(NOTE_KEY, ()) if row.tag}
    tags = repo._run("tag", "--merged", main, "--sort=-version:refname", "--list", "v*").stdout.decode().splitlines()
    for tag in tags:
        if not shipped_history.TAG_PATTERN.fullmatch(tag):
            continue
        commit = repo.resolve(f"refs/tags/{tag}")
        bundle = repo.snapshot(commit, ledger.skill_dir(NOTE_PATH))
        skill = bundle.get("SKILL.md")
        fingerprint = bundle_fingerprint(bundle)
        if skill is None or fingerprint is None:
            continue
        digest = shipped_history.skill_digest(skill)
        if digest in released:
            row = {"fingerprint": fingerprint, "skill_md_sha256": digest, "commit": commit, "basis": "release"}
            return ledger.SubjectDef(NOTE_KEY, NOTE_MANIFEST, NOTE_PATH, since, (row,))
    raise ledger.LedgerError("note has no released bundle reachable from main")


def same_decisions(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Ignore the coverage clock and reference commits when the recorded facts are unchanged."""

    def relevant(payload: Mapping[str, Any]) -> dict[str, Any]:
        subjects = {}
        for key, body in payload["subjects"].items():
            subjects[key] = {
                **body,
                "current": body["current"]["fingerprint"],
                "decision": {k: v for k, v in body["decision"].items() if k != "decided_at"},
            }
        return {"policy": payload.get("policy"), "subjects": subjects}

    return relevant(before) == relevant(after)


def main_plan(ev: ledger.Evidence, main: str, now: dt.datetime) -> Plan:
    """Build the scheduled candidate from committed main and trusted Actions evidence."""
    previous_raw = ev.repo.show(main, ledger.LEDGER_PATH)
    previous = ledger.parse_ledger(previous_raw) if previous_raw is not None else None
    if previous is None:
        definitions = {NOTE_KEY: _genesis_definition(ev.repo, main)}
    else:
        if not ev.repo.is_ancestor(previous["covers"]["main_sha"], main):
            return Plan(main, "stale", "the current ledger covers a commit outside main history")
        definitions = ledger.defs_from_ledger(previous)
    proposed = ledger.build_ledger(
        ev,
        prev=previous,
        prev_sha256=ledger.sha256_hex(previous_raw) if previous_raw is not None else ledger.GENESIS_PREV,
        definitions=definitions,
        covers_sha=main,
        cutoff_at=now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    incomplete = ledger._completeness_errors(ev, definitions, proposed, previous, main, now)
    if incomplete:
        return Plan(main, "pending", " | ".join(incomplete))
    if previous is not None and same_decisions(previous, proposed):
        return Plan(main, "equal", "the automatic ledger matches main", proposed)
    return Plan(main, "update", "the ledger has new trials or decisions", proposed)


def with_approval(
    ev: ledger.Evidence, previous_raw: bytes, proposed: Mapping[str, Any], approval: Approval, pr: int
) -> dict[str, Any]:
    """Rebuild with one additional approval row tied to its pull request number."""
    previous = ledger.parse_ledger(previous_raw)
    definitions = ledger.defs_from_ledger(previous)
    definition = definitions[approval.key]
    main = proposed["covers"]["main_sha"]
    skill = ev.repo.show(main, definition.subject_path)
    if skill is None:
        raise ledger.LedgerError("the approval's SKILL.md is missing")
    row = {
        "basis": "owner_review",
        "pr": pr,
        "fingerprint": approval.fingerprint,
        "commit": main,
        "skill_md_sha256": shipped_history.skill_digest(skill),
    }
    definitions[approval.key] = replace(definition, approved=(*definition.approved, row))
    return ledger.build_ledger(
        ev,
        prev=previous,
        prev_sha256=ledger.sha256_hex(previous_raw),
        definitions=definitions,
        covers_sha=main,
        cutoff_at=proposed["covers"]["cutoff_at"],
    )


def retryable_errors(errors: Sequence[str]) -> bool:
    return bool(errors) and all(
        ": ledger is stale, " in message or message == "cutoff_at is older than the allowed ledger age"
        for message in errors
    )


def approval_search(slug: str, branch: str) -> str:
    return f"https://github.com/{slug}/pulls?{urlencode({'q': f'is:pr is:open head:{branch}'})}"
