"""Scoreability projection and graph-impact printing for work verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import verify_manifest, verify_trial


def _print_graph_impact(ranking: object) -> None:
    if not isinstance(ranking, dict):
        return
    if ranking.get("degraded") is True:
        reason = ranking.get("degraded_reason") or "graphtrail unavailable"
        print(f"graph_impact: degraded ({reason})")
        return
    candidates_raw = ranking.get("candidates")
    candidates = candidates_raw if isinstance(candidates_raw, list) else []
    changed_raw = ranking.get("changed_files")
    changed = changed_raw if isinstance(changed_raw, list) else []
    print(f"graph_impact: {len(candidates)} candidate(s) from {len(changed)} changed file(s)")
    for item in candidates:
        if not isinstance(item, dict):
            continue
        confidence_raw = item.get("confidence")
        confidence = confidence_raw if isinstance(confidence_raw, dict) else {}
        band = confidence.get("band") or "unknown"
        score = confidence.get("score")
        hops = confidence.get("min_hops")
        evidence_raw = item.get("evidence")
        evidence = evidence_raw if isinstance(evidence_raw, dict) else {}
        via_raw = evidence.get("via")
        via = via_raw if isinstance(via_raw, list) else []
        via_text = ",".join(str(symbol) for symbol in via[:3]) if via else "-"
        print(f"- [{band} score={score} hops={hops} via={via_text}] {item.get('command')}")
    note = ranking.get("attribution")
    if isinstance(note, str) and note.strip() and candidates:
        print(f"graph_impact_note: {note.strip()}")


_ADHOC_SCOREABILITY_REMEDIATION = (
    "ad-hoc --command/--argv-json receipts are audit-only evidence and never score an artifact; "
    "run `brigade work verify run --manifest <id>` against a manifest tracked under "
    "verify/manifests/ to produce a scoreable receipt"
)


def _scoreability_notice(target: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    """Project whether this receipt can score its ``--capture`` artifact (#1378).

    Eligibility used to be visible only in an aggregate ``work brief`` field, so a
    session could run dozens of green ``--command --capture`` checks and feed the
    ratchet nothing while every immediate indicator said otherwise. Project the
    verdict on the run that produced the receipt instead.
    """
    run_dir_value = receipt.get("path")
    patch_path = None
    if isinstance(run_dir_value, str) and run_dir_value:
        candidate = Path(run_dir_value) / "changes.patch"
        if candidate.is_file():
            patch_path = candidate
    projection = verify_trial.project_trial(receipt, target=target, patch_path=patch_path)
    notice: dict[str, Any] = {
        "eligible": projection.eligible,
        "reason": projection.reason,
        "attributed": projection.attributed,
        "registered_manifest_ids": [],
        "remediation": None,
    }
    if not projection.eligible:
        notice["registered_manifest_ids"] = verify_manifest.registered_manifest_ids(target)
        notice["remediation"] = _ADHOC_SCOREABILITY_REMEDIATION
    return notice


def _print_scoreability_notice(notice: dict[str, Any]) -> None:
    if notice.get("eligible"):
        print("scoreable: yes")
        return
    print(f"warning: scoreable: no (reason={notice.get('reason')}); this receipt cannot score the captured artifact")
    remediation = notice.get("remediation")
    if remediation:
        print(f"  {remediation}")
    manifest_ids = notice.get("registered_manifest_ids")
    if isinstance(manifest_ids, list) and manifest_ids:
        print(f"  registered manifests: {', '.join(manifest_ids)}")
    else:
        print("  registered manifests: none tracked under verify/manifests/ in this target")
