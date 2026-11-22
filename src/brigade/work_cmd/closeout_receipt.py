"""Select local verification evidence for a work closeout."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import localio
from . import verify_ci, verify_receipt_details


def _matches_session(receipt: dict[str, Any], session_id: str, session: dict[str, Any]) -> bool:
    producer = receipt.get("producer_work_session_id")
    if producer is not None:
        return producer == session_id

    binding = session.get("harness_session")
    if not isinstance(binding, dict) or not binding.get("fingerprint"):
        return False
    harness_session = receipt.get("harness_session")
    if not isinstance(harness_session, dict) or any(
        harness_session.get(key) != binding.get(key) for key in ("harness", "fingerprint")
    ):
        return False
    # A harness session can span several work sessions. Bound older receipts
    # that carry only harness identity to the lifetime of this work session.
    started = localio.parse_iso_datetime(session.get("started_at"))
    ended = localio.parse_iso_datetime(session.get("ended_at"))
    receipt_start = localio.parse_iso_datetime(receipt.get("started_at"))
    receipt_end = localio.parse_iso_datetime(receipt.get("completed_at"))
    return bool(
        started and ended and receipt_start and receipt_end and started <= receipt_start <= receipt_end <= ended
    )


def select(
    target: Path, session_path: Path, session: dict[str, Any], receipts: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """Return the newest qualifying receipt or the newest session receipt and its blocker."""
    if not receipts:
        return None, "no verification receipt found"

    session_id = str(session.get("id") or session_path.name)
    matching = [receipt for receipt in receipts if _matches_session(receipt, session_id, session)]
    if not matching:
        if any(
            receipt.get("producer_work_session_id") is None and not receipt.get("harness_session")
            for receipt in receipts
        ):
            return None, "verification receipt is unattributed: no work or harness session binding"
        return None, "no verification receipt for this session"

    tested = [receipt for receipt in matching if receipt.get("status") not in ("busy", "canceled")]
    latest = tested[0] if tested else matching[0]
    tree = localio.tree_fingerprint(target)
    if tree is None:
        return matching[0], "could not read the current tree fingerprint"
    for receipt in tested:
        fingerprint = receipt.get("tree_fingerprint")
        if not isinstance(fingerprint, str):
            return receipt, "verification receipt for this session has no tree fingerprint"
        same_tree = verify_receipt_details.same_verification_tree(target, fingerprint, tree)
        if same_tree is None:
            return receipt, "could not compare verification tree fingerprints; only Brigade state may have changed"
        if not same_tree:
            continue
        status = receipt.get("status")
        if status != "completed":
            return receipt, f"latest verification did not complete: {receipt.get('run_id')} [{status}]"
        if receipt.get("tree_stable") is False:
            return receipt, "tree changed during the verification run"
        return receipt, None

    if not tested:
        return latest, f"latest verification did not complete: {latest.get('run_id')} [{latest.get('status')}]"
    return latest, "latest verification receipt for this session covers a different tree"


def local_checks_summary(verification: object) -> str:
    if not isinstance(verification, dict):
        return "Local checks: no receipt"
    return (
        f"Local checks: {verification.get('run_id')} [{verification.get('status')}] "
        f"commands={verification.get('command_count', 0)}"
    )


def write_markdown(path: Path, closeout: dict[str, Any]) -> None:
    session = closeout.get("session")
    lines = [
        "# Brigade Work Closeout",
        "",
        f"- Closeout: `{closeout.get('closeout_id')}`",
        f"- Status: {closeout.get('status')}",
        f"- Ready: {closeout.get('ready')}",
        f"- Session: `{session.get('id') if isinstance(session, dict) else ''}`",
        f"- {local_checks_summary(closeout.get('verification'))}",
        f"- {verify_ci.closeout_summary(closeout['ci_verification'])}",
        "",
        "## Blockers",
        "",
    ]
    blockers = closeout.get("blockers") if isinstance(closeout.get("blockers"), list) else []
    lines.extend(f"- {item}" for item in blockers) if blockers else lines.append("- none")
    lines.extend(["", "## Evidence", ""])
    for key in ("task", "scanner_sweep", "code_review", "handoff_drafts"):
        value = closeout.get(key)
        lines.append(f"- {key}: `{json.dumps(value, sort_keys=True, default=str)[:500]}`")
    path.with_name("closeout.md").write_text("\n".join(lines) + "\n")
