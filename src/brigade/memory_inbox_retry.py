"""Reprocess review drafts with the ingest rules and journal-first writes."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from . import ingest, memory_archive, memory_cmd
from . import memory_care_journal as journal
from .memory_inbox_io import read


@journal.serialized
def reprocess(
    owner: Path,
    draft: Path,
    *,
    card_ids: ingest.CardIdIndex | None = None,
    today: date | None = None,
    dry_run: bool = False,
) -> ingest.Action:
    from .inbox_lock import held_file_lock
    from .memory_inbox_drain import (
        disposition_changes,
        disposition_metadata,
        classify,
        parse_header,
        retry_flags,
        evidence_uncertain,
        NEEDS_EVIDENCE_PROOF,
    )
    from .memory_inbox_append import plan_append
    from .memory_evidence_log import LOG_PATH, append_evidence, read_evidence

    draft = draft.absolute()
    rel = draft.relative_to(owner).as_posix()
    if ingest._is_tracked(draft, ingest._git_tracked_files(draft.parent)):
        raise ingest.TrackedHandoffError("tracked review draft")
    data = read(owner, rel)
    if data is None:
        raise ValueError("review draft missing")
    text = data.decode("utf-8")
    code, routed = parse_header(text)
    if not code or routed is None or classify(code, text) in ("human", "hold"):
        return ingest.Action("inboxed", "unsafe-review-draft")
    flags = retry_flags(code, text)
    if flags is None:
        return ingest.Action("inboxed", "retry-flags-unknown")
    evidence, warnings, unsupported = read_evidence(owner)
    cited = evidence_uncertain(owner, warnings, unsupported) or any(
        row["evidence_id"] in (rel, str(draft)) for row in evidence
    )
    sections = ingest.parse(draft, data)
    lock = journal.safe_path(owner, ".brigade/memory/proposals/proposal.lock")
    from contextlib import nullcontext, ExitStack

    if not dry_run:
        lock.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack, nullcontext() if dry_run else held_file_lock(lock, deadline_seconds=1):
        outcome = ingest.decide(
            sections, target=owner, promote_cards=flags[0], route_documents=flags[1], card_ids=card_ids
        )
        if (warnings or unsupported) and NEEDS_EVIDENCE_PROOF.get(outcome.code, True):
            return ingest.Action("inboxed", "evidence-unavailable")
        if outcome.kind not in ("promoted", "reinforced", "routed") or outcome.dest is None:
            return ingest.Action("inboxed", outcome.code)
        if outcome.kind == "routed" and not dry_run:
            from .inbox_lock import InboxLockTimeout

            try:
                stack.enter_context(ingest._document_lock(owner))
            except InboxLockTimeout:
                return ingest.Action("inboxed", "document-publication-busy")
        dest = outcome.dest
        dest_rel = dest.relative_to(owner).as_posix()
        try:
            before = read(owner, dest_rel)
        except ValueError:
            return ingest.Action("inboxed", "destination-unreadable-or-oversized")
        if before is not None:
            refusal = memory_archive.frontmatter_refusal(before.decode("utf-8"))
            if refusal:
                raise ValueError(refusal)
        config = memory_cmd._config_or_default(owner)
        if memory_cmd._is_read_only(config, dest_rel):
            raise journal.ReadOnlyPath(dest_rel)
        changes = disposition_changes(owner, rel, data, f"retry:{outcome.code}", cited=cited, expired=False)
        evidence_rel = rel if cited else changes[0].path
        metadata = disposition_metadata(rel, f"retry:{outcome.code}", cited=cited)
        appends = metadata.setdefault("appends", [])
        if outcome.kind == "promoted":
            expected = (
                sections.get(ingest.EXPECTED_SHA_SECTION.lower(), "").strip().lower()
                if sections.get("recommended memory action", "").strip().lower() == "update-card"
                else ""
            )
            text = ingest._publish_promoted_card(dest, owner, sections, dry_run=True, expected_sha=expected)
            after = text.encode("utf-8")
        elif outcome.kind == "reinforced":
            if before is None:
                raise ValueError("reinforcement destination missing")
            # Run the producing function on a private staging copy, never on the destination.
            with TemporaryDirectory(prefix="care-") as staging:
                copy = Path(staging) / dest_rel
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_bytes(before)
                ingest.reinforce_existing_card(
                    copy, today=today or datetime.now(timezone.utc).date(), evidence_pointer=evidence_rel
                )
                after = copy.read_bytes()
                if not unsupported and outcome.match is not None:
                    append_evidence(
                        Path(staging),
                        card_path=copy,
                        evidence_id=evidence_rel,
                        kind="reinforced",
                        similarity=outcome.match.similarity,
                        ts=datetime.combine(
                            today or datetime.now(timezone.utc).date(), datetime.now(timezone.utc).time(), timezone.utc
                        ).isoformat(),
                    )
                    row = (Path(staging) / LOG_PATH).read_bytes()
                    appends.append(plan_append(LOG_PATH, row, "evidence"))
        else:
            block = ("\n\n" + sections["suggested document content"].strip() + "\n").encode()
            appends.append(plan_append(dest_rel, block, "document"))
            after = before or b""
        if outcome.kind != "routed":
            changes.insert(0, journal.Change(dest_rel, before, after))
        else:
            # Guard the observed destination until finish without replacing it.
            metadata["append_guards"] = {dest_rel: journal.kernel.content_digest(before)}
        if dry_run:
            return ingest.Action(outcome.kind, outcome.code, dest_rel)
        if ingest._is_tracked(draft, ingest._git_tracked_files(draft.parent)):
            raise ingest.TrackedHandoffError("tracked review draft")
        decision = journal.commit_phased(
            owner,
            kind="inbox",
            reason=f"retry:{outcome.code}",
            card_id=None,
            changes=changes,
            prepared_paths=[] if cited else [next(c.path for c in changes if c.before is None)],
            hub={},
            metadata=metadata,
        )
        if journal._payload(owner, decision.decision_id).get("phase") != "committed":
            raise journal.JournalConflict("retry abandoned")
        if outcome.kind in ("promoted", "reinforced") and card_ids is not None:
            card_ids.register(dest_rel, after.decode("utf-8"))
        return ingest.Action(outcome.kind, decision.decision_id, dest_rel)
