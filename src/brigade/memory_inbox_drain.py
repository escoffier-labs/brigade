"""Drain safe review drafts without discarding the evidence or card bytes."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import ingest, memory_content_scan, memory_evidence_log, untrusted
from . import memory_care_journal as journal
from .memory_inbox_io import MAX_DRAFT_BYTES as MAX_DRAFT_BYTES, read

INBOX_REL = "memory/handoff-inbox"
RESOLVED_REL = INBOX_REL + "/resolved"
LEDGER_REL = ".brigade/memory-care/inbox-ledger.jsonl"
HUMAN_CODES = frozenset(
    {
        "card-injection-signal",
        "document-injection-signal",
        "contradiction-candidate",
        "governed-card-migration",
        "identity-migration-fields",
        "card-id-collision",
        "card-destination-symlinked",
        "card-destination-not-regular",
        "card-destination-unreadable",
        "document-destination-unsafe",
        "unsafe-target-document",
        "unsafe-card-name",
    }
)
RESOLVE_CODES = {
    "document-content-present": "already-applied",
    "document-anchor-present": "already-applied",
    "near-match-proposal": "evidence-logged",
}
RETRY_CODES = frozenset(
    {
        "expected-sha-stale",
        "expected-sha-mismatch",
        "card-publication-changed",
        "card-publication-busy",
        "document-destination-unwritable",
    }
)


def parse_header(text: str) -> tuple[str | None, datetime | None]:
    text = text.replace("\r\n", "\n")
    match = re.match(
        r"\A<!-- routed from [^\n]*\n\s+reason: ([a-z0-9-]+)[^\n]*\n\s+routed-at: ([0-9]{8}T[0-9]{6}Z) -->\n", text
    )
    if not match:
        return None, None
    try:
        return match[1], datetime.strptime(match[2], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None, None


def classify(code: str | None, text: str) -> str:
    if (
        code in HUMAN_CODES
        or memory_content_scan.scan_verdict([text])[0] != "clean"
        or untrusted.scan_untrusted(text).flagged
    ):
        return "human"
    if code in RESOLVE_CODES:
        return "resolve"
    return "retry" if code in RETRY_CODES else "expire"


def _ledger(target: Path) -> set[str]:
    data = read(target, LEDGER_REL, limit=4 * 1024 * 1024)
    if data is None:
        return set()
    result = set()
    for line in data.splitlines():
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise ValueError("malformed inbox ledger")
        result.add(row["path"])
    return result


def pending(target: Path) -> list[Path]:
    target = target.resolve()
    ledger = _ledger(target)
    root = journal.safe_path(target, INBOX_REL)
    if not root.exists():
        return []
    return [path for path in sorted(root.glob("*.md")) if path.relative_to(target).as_posix() not in ledger]


def move_changes(target: Path, rel: str, data: bytes, directory: str) -> list[journal.Change]:
    name = Path(rel).name
    for n in range(10000):
        dest = f"{directory}/{name}" if not n else f"{directory}/{Path(name).stem}-{n}.md"
        if read(target, dest) is None:
            return [journal.Change(dest, None, data), journal.Change(rel, data, None)]
    raise ValueError("resolved draft namespace exhausted")


def disposition_changes(
    target: Path, rel: str, data: bytes, reason: str, *, cited: bool, expired: bool
) -> list[journal.Change]:
    if cited:
        before = read(target, LEDGER_REL, limit=4 * 1024 * 1024)
        row = json.dumps({"path": rel, "reason": reason}, sort_keys=True).encode() + b"\n"
        return [journal.Change(rel, data, data), journal.Change(LEDGER_REL, before, (before or b"") + row)]
    return move_changes(target, rel, data, RESOLVED_REL + ("/expired" if expired else ""))


def _record(target: Path, rel: str, data: bytes, reason: str, *, cited: bool, expired: bool) -> journal.Decision:
    path = target / rel
    if ingest._is_tracked(path, ingest._git_tracked_files(path.parent)):
        raise ingest.TrackedHandoffError("tracked review draft")
    changes = disposition_changes(target, rel, data, reason, cited=cited, expired=expired)
    decision = journal.commit_phased(
        target,
        kind="inbox",
        reason=reason,
        card_id=None,
        changes=changes,
        prepared_paths=[] if cited else [changes[0].path],
        hub={},
    )

    if journal._payload(target, decision.decision_id).get("phase") != "committed":
        raise journal.JournalConflict("inbox disposition abandoned")
    return decision


@journal.serialized
def drain(target: Path, *, today: date, deadline_days: int, dry_run: bool = False) -> dict[str, Any]:
    from .memory_inbox_sweep import sweep

    target = target.resolve()
    result: dict[str, Any] = dict(
        resolved=0, retried=0, expired=0, kept_for_person=0, waiting=0, skipped=0, decisions=[]
    )
    if deadline_days < 1:
        raise ValueError("deadline_days must be positive")
    if not dry_run:
        result["recovery"] = journal.resolve_prepared(target)
    try:
        paths = pending(target)
        for inbox in ingest._resolve_inbox_paths(target):
            try:
                rel = inbox.relative_to(target).as_posix() + "/malformed"
                root = journal.safe_path(target, rel)
                if root.exists():
                    paths.extend(sorted(root.glob("*.md")))
            except ValueError:
                result["decisions"].append({"reason": "skipped:external-writer-inbox"})
        handled = _ledger(target)
        paths = [path for path in paths if path.relative_to(target).as_posix() not in handled]
        evidence, warnings, unsupported = memory_evidence_log.read_evidence(target)
        # A truncated or unsupported evidence scan cannot prove a path is unreferenced.
        preserve_all = bool(
            warnings or (unsupported and read(target, memory_evidence_log.LOG_PATH, limit=4 * 1024 * 1024) is not None)
        )
        cited = {row["evidence_id"] for row in evidence}
    except (ValueError, OSError) as exc:
        result["error"] = str(exc)
        return result
    card_ids = ingest.CardIdIndex(target)
    for path in paths:
        rel = path.relative_to(target).as_posix()
        reason = ""
        try:
            data = read(target, rel)
            if data is None:
                raise ValueError("draft disappeared")
            text = data.decode("utf-8")
            code, routed = parse_header(text)
            if not code or routed is None:
                raise ValueError("missing or malformed routing header")
            disposition = classify(code, text)
            expired = (today - routed.date()).days >= deadline_days
            keep = preserve_all or rel in cited or str(path) in cited
            decision = None
            if disposition == "human":
                result["kept_for_person"] += 1
                reason = f"trust-or-security:{code}"
            elif disposition == "retry":
                if not dry_run:
                    action = ingest.reprocess_review_draft(target, path, card_ids=card_ids)
                else:
                    action = ingest.Action("inboxed", "dry-run retry")
                if action.kind in ("promoted", "reinforced", "routed"):
                    result["retried"] += 1
                    reason = "retry-applied"
                    decision = action.summary
                else:
                    # Re-evaluate safety after retry; new unsafe findings never expire.
                    sections = ingest.parse(path, data)
                    outcome = ingest.decide(
                        sections, target=target, promote_cards=True, route_documents=True, card_ids=card_ids
                    )
                    if classify(outcome.code, text) == "human":
                        result["kept_for_person"] += 1
                        reason = f"trust-or-security:{outcome.code}"
                    elif expired:
                        reason = "retry-deadline-expired"
                        if not dry_run:
                            decision = _record(target, rel, data, reason, cited=keep, expired=True).decision_id
                        result["expired"] += 1
                    else:
                        result["waiting"] += 1
                        reason = f"retry-unresolved:{outcome.code}"
            elif disposition == "resolve" or expired:
                key = "resolved" if disposition == "resolve" else "expired"
                reason = RESOLVE_CODES[code] if key == "resolved" else "deadline-expired"
                if not dry_run:
                    decision = _record(target, rel, data, reason, cited=keep, expired=key == "expired").decision_id
                result[key] += 1
            else:
                result["waiting"] += 1
                reason = "deadline-not-reached"
            result["decisions"].append({"path": rel, "reason": reason, "decision_id": decision})
        except (ValueError, OSError, ingest.TrackedHandoffError, journal.kernel.ProjectionError) as exc:
            result["skipped"] += 1
            result["decisions"].append({"path": rel, "reason": f"skipped:{type(exc).__name__}"})
    result["sweep"] = sweep(target, dry_run=dry_run)
    if not dry_run:
        from .memory_hub_store import write_json

        write_json(journal.safe_path(target, ".brigade/memory-care/inbox/latest.json"), result)
    return result
