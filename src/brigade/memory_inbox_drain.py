"""Drain safe review drafts without discarding the evidence or card bytes."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import ingest, instruction_overflow, memory_content_scan, memory_evidence_log, untrusted
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
}
RETRY_CODES = frozenset(
    {
        "expected-sha-stale",
        "expected-sha-mismatch",
        "card-publication-changed",
        "card-publication-busy",
        "document-destination-unwritable",
        "bootstrap-budget-exceeded",
    }
)


HOLD_CODES = frozenset(
    {
        "proposal-notification",
        "near-match-proposal",
        "card-frontmatter-invalid",
        "card-fence-error",
        "unknown-sections",
        "expected-sha-invalid",
        "missing-action",
        "document-has-headings",
        "missing-target-document",
        "empty-document-content",
        "action-not-auto-handled",
        "card-promoted",
        "card-reinforced",
        "document-appended",
        "document-routed-to-card",
    }
)
EXPIRE_CODES: frozenset[str] = frozenset()  # No ingest code proves that unapplied knowledge is disposable.

# Per-code dependence on usable evidence-log proof, separate from destination reads.
NEEDS_EVIDENCE_PROOF: dict[str, bool] = {
    "action-not-auto-handled": False,
    "bootstrap-budget-exceeded": False,
    "card-destination-not-regular": False,
    "card-destination-symlinked": False,
    "card-destination-unreadable": False,
    "card-fence-error": False,
    "card-frontmatter-invalid": False,
    "card-id-collision": False,
    "card-injection-signal": False,
    "card-promoted": False,
    "card-publication-busy": False,
    "card-publication-changed": False,
    "card-reinforced": True,
    "contradiction-candidate": True,
    "document-anchor-present": False,
    "document-appended": False,
    "document-content-present": False,
    "document-destination-unsafe": False,
    "document-destination-unwritable": False,
    "document-has-headings": False,
    "document-injection-signal": False,
    "document-routed-to-card": False,
    "empty-document-content": False,
    "expected-sha-invalid": False,
    "expected-sha-mismatch": False,
    "expected-sha-stale": False,
    "governed-card-migration": False,
    "identity-migration-fields": False,
    "missing-action": False,
    "missing-target-document": False,
    "near-match-proposal": True,
    "proposal-notification": False,
    "unknown-sections": False,
    "unsafe-card-name": False,
    "unsafe-target-document": False,
}


def retry_flags(code: str, text: str) -> tuple[bool, bool] | None:
    header = text.replace("\r\n", "\n").split("-->", 1)[0].rstrip() + " -->"
    match = re.search(r"^     retry-flags: promote=(true|false) route=(true|false) -->$", header, re.M)
    if match:
        return match[1] == "true", match[2] == "true"
    if "retry-flags:" in header:
        return None
    # An old budget hold was an append to a document, so it retries with routing on and promotion off.
    routes = ("document-destination-unwritable", "bootstrap-budget-exceeded")
    if code in RETRY_CODES - set(routes):
        return True, False
    if code in routes or code in RESOLVE_CODES:
        return False, True
    return None


def parse_header(text: str) -> tuple[str | None, datetime | None]:
    text = text.replace("\r\n", "\n")
    match = re.match(
        r"\A<!-- routed from [^\n]*\n\s+reason: ([a-z0-9-]+)[^\n]*\n\s+routed-at: ([0-9]{8}T[0-9]{6}Z)(?: -->|\n     retry-flags: [^\n]* -->)\n",
        text,
    )
    if not match:
        return None, None
    try:
        return match[1], datetime.strptime(match[2], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None, None


def classify(code: str | None, text: str, table: dict[str, tuple[str, bool]] | None = None) -> str:
    if (
        code in HUMAN_CODES
        or memory_content_scan.scan_verdict([text])[0] != "clean"
        or untrusted.scan_untrusted(text).flagged
    ):
        return "human"
    if code in RESOLVE_CODES:
        return "resolve"
    if code in RETRY_CODES:
        return "retry"
    if table and code in table:
        return table[code][0]
    return "expire" if code in EXPIRE_CODES else "hold"


def _ledger(target: Path) -> set[str]:
    from .memory_inbox_io import rows

    from .memory_log_retractions import active, retraction

    parsed = []
    for line in rows(target, LEDGER_REL):
        row = json.loads(line)
        if not retraction(row) and (not isinstance(row, dict) or not isinstance(row.get("path"), str)):
            raise ValueError("malformed inbox ledger")
        parsed.append((row, line))
    return {row["path"] for row in active(parsed)}


def evidence_uncertain(target: Path, warnings: int, unsupported: bool) -> bool:
    if warnings:
        return True
    if unsupported:
        try:
            return journal.safe_path(target, memory_evidence_log.LOG_PATH).exists()
        except (ValueError, OSError):
            return True
    return False


def destination_proven(
    target: Path,
    path: Path,
    data: bytes,
    code: str,
    text: str,
    card_ids: ingest.CardIdIndex,
    routing: instruction_overflow.RouteContext | None = None,
) -> bool:
    flags = retry_flags(code, text)
    if flags is None:
        return False
    try:
        outcome = ingest.decide(
            ingest.parse(path, data),
            target=target,
            promote_cards=flags[0],
            route_documents=flags[1],
            card_ids=card_ids,
            routing=routing,
        )
        return outcome.code == "document-content-present"
    except (ValueError, OSError):
        return False


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
        return [journal.Change(rel, data, data)]
    return move_changes(target, rel, data, RESOLVED_REL + ("/expired" if expired else ""))


def disposition_metadata(rel: str, reason: str, *, cited: bool) -> dict[str, Any]:
    from .memory_inbox_append import plan_append

    if not cited:
        return {}
    import uuid

    row = json.dumps({"path": rel, "reason": reason, "id": uuid.uuid4().hex}, sort_keys=True).encode() + b"\n"
    return {"appends": [plan_append(LEDGER_REL, row, "ledger")]}


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
        metadata=disposition_metadata(rel, reason, cited=cited),
    )

    if journal._payload(target, decision.decision_id).get("phase") != "committed":
        raise journal.JournalConflict("inbox disposition abandoned")
    return decision


@journal.serialized
def drain(
    target: Path,
    *,
    today: date,
    deadline_days: int,
    dry_run: bool = False,
    classifications: dict[str, tuple[str, bool]] | None = None,
) -> dict[str, Any]:
    from .memory_inbox_sweep import sweep

    target = target.resolve()
    result: dict[str, Any] = dict(
        resolved=0, retried=0, expired=0, kept_for_person=0, held=0, waiting=0, skipped=0, decisions=[]
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
        preserve_all = evidence_uncertain(target, warnings, unsupported)
        cited = {row["evidence_id"] for row in evidence}
    except (ValueError, OSError) as exc:
        result["error"] = str(exc)
        return result
    card_ids = ingest.CardIdIndex(target)
    routing = instruction_overflow.RouteContext(target)  # discovery once per drain, not per draft
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
            disposition = classify(code, text, classifications)
            needs_proof = (classifications or {}).get(code, (disposition, NEEDS_EVIDENCE_PROOF.get(code, True)))[1]
            expired = (today - routed.date()).days >= deadline_days
            keep = preserve_all or rel in cited or str(path) in cited
            decision = None
            if disposition == "human":
                result["kept_for_person"] += 1
                reason = f"trust-or-security:{code}"
            elif disposition == "hold" or (needs_proof and (warnings or unsupported)):
                result["held"] += 1
                reason = f"hold:{code}" if disposition == "hold" else "hold:evidence-unavailable"
            elif disposition == "retry":
                from .memory_inbox_retry import reprocess

                action = reprocess(target, path, card_ids=card_ids, today=today, dry_run=dry_run, routing=routing)
                if action.kind in ("promoted", "reinforced", "routed"):
                    result["retried"] += 1
                    reason = f"retry-applied:{action.destination}"
                    decision = action.summary
                elif action.summary in RESOLVE_CODES:
                    if destination_proven(target, path, data, code, text, card_ids, routing):
                        reason = "already-applied"
                        if not dry_run:
                            decision = _record(target, rel, data, reason, cited=keep, expired=False).decision_id
                        result["resolved"] += 1
                    else:
                        result["held"] += 1
                        reason = "hold:destination-proof-unavailable"
                elif action.summary == "document-publication-busy":
                    result["waiting"] += 1
                    reason = "retry-waiting:document-publication-busy"
                else:
                    # Re-evaluate safety after retry; new unsafe findings never expire.
                    if classify(action.summary, text) in ("human", "hold"):
                        key = "kept_for_person" if classify(action.summary, text) == "human" else "held"
                        result[key] += 1
                        reason = (
                            f"trust-or-security:{action.summary}"
                            if key == "kept_for_person"
                            else f"hold:{action.summary}"
                        )
                    elif expired and classify(action.summary, text, classifications) == "expire":
                        reason = "retry-deadline-expired"
                        if not dry_run:
                            decision = _record(target, rel, data, reason, cited=keep, expired=True).decision_id
                        result["expired"] += 1
                    elif classify(action.summary, text, classifications) == "retry":
                        result["held"] += 1
                        reason = f"hold:{action.summary}"
                    else:
                        result["waiting"] += 1
                        reason = f"retry-unresolved:{action.summary}"
            elif disposition == "resolve" and not destination_proven(target, path, data, code, text, card_ids, routing):
                result["held"] += 1
                reason = "hold:destination-proof-unavailable"
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
