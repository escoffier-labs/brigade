"""Versioned control crosswalk and `brigade evidence controls` evaluation.

The crosswalk is an evidence index and design documentation, not evidence that a
control operated.  It maps Brigade evidence claims to external framework control
identifiers and computes an evidentiary state per mapping from workspace
artifacts.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib import resources as importlib_resources
from pathlib import Path
from typing import Any

from . import (
    agent_request,
    approval,
    attestation,
    attestation_input,
    causal_receipt,
    cosign_attestation,
    localio,
    receipts_trailer,
    run_journal,
)

SCHEMA = "brigade.control_crosswalk.v1"
EVIDENCE_CONTROLS_SCHEMA = "brigade.evidence_controls.v1"
HEADER_NOTICE = (
    "Evidence index for configured Brigade artifacts. Not a compliance determination, "
    "not evidence that a control operated."
)

VALID_RELATIONSHIPS = frozenset({"supports", "partially-supports", "no-relationship"})
VALID_OBLIGATION_BEARERS = frozenset({"provider", "deployer", "service-organisation", "supplier", "any"})
VALID_STATES = frozenset({"evidenced_passed", "evidenced_failed", "untested", "not_applicable"})
VALID_MAPPING_STATUSES = frozenset({"mapped", "identifiers-not-sourced"})

_STATE_RULES: dict[str, str] = {
    "EC-01": "verify-receipt-completed",
    "EC-02": "sshsig-test-result-signed-ok",
    "EC-03": "cosign-bundle-exists",
    "EC-04": "agent-request-signed",
    "EC-05": "human-approval-allow-with-sod",
    "EC-06": "run-event-journal-exists",
    "EC-07": "governance-inventory-exists",
    "EC-08": "commit-trailer-receipts",
    "EC-09": "guard-audit-allow",
    "EC-10": "outcome-record-exists",
    "EC-11": "verify-archive-index-exists",
    "EC-12": "artifact-absent-until-merged",
}


class ControlCrosswalkError(ValueError):
    """Raised when a crosswalk or evaluation invariant is violated."""


def load_crosswalk() -> dict[str, Any]:
    """Load the bundled control-crosswalk template."""
    text = importlib_resources.files(__package__).joinpath("templates/control-crosswalk.json").read_text()
    return json.loads(text)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    """Bounded read of a JSON object; returns None on missing or invalid."""
    try:
        return attestation_input.read_json_object(path)
    except (attestation_input.AttestationInputError, FileNotFoundError, IsADirectoryError, OSError):
        return None


def _is_git_repo(target: Path) -> bool:
    """Return True when target is inside a Git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(target),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _verify_receipt_dirs(target: Path) -> list[Path]:
    """Return receipt directories under .brigade/work/verify-runs."""
    root = target / ".brigade" / "work" / "verify-runs"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "receipt.json").is_file())


def _run_dirs(target: Path) -> list[Path]:
    """Return run directories under .brigade/runs."""
    root = target / ".brigade" / "runs"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _receipt_matches_scope(receipt: dict[str, Any], run_id: str | None) -> bool:
    """Return True when the receipt is in scope for the requested run_id."""
    if run_id is None:
        return True
    producer_run_id = receipt.get("producer_run_id")
    if isinstance(producer_run_id, str) and producer_run_id == run_id:
        return True
    receipt_run_id = receipt.get("run_id")
    if isinstance(receipt_run_id, str) and receipt_run_id == run_id:
        return True
    return False


def _evaluate_verify_receipt_completed(target: Path, run_id: str | None) -> str:
    """EC-01 state: any completed verify receipt with all commands exit 0."""
    dirs = _verify_receipt_dirs(target)
    if not dirs:
        return "untested"
    any_completed = False
    for run_dir in dirs:
        receipt = _read_json_object(run_dir / "receipt.json")
        if receipt is None:
            continue
        if not _receipt_matches_scope(receipt, run_id):
            continue
        status = receipt.get("status")
        commands = receipt.get("commands")
        if status == "completed":
            any_completed = True
            if isinstance(commands, list) and all(isinstance(c, dict) and c.get("exit_code") == 0 for c in commands):
                return "evidenced_passed"
    return "evidenced_failed" if any_completed else "untested"


def _evaluate_sshsig_test_result_signed_ok(target: Path, run_id: str | None) -> str:
    """EC-02 state: signed SSHSIG Test Result attestation with rederived receipt."""
    dirs = _verify_receipt_dirs(target)
    if not dirs:
        return "untested"
    for run_dir in dirs:
        receipt = _read_json_object(run_dir / "receipt.json")
        if receipt is not None and not _receipt_matches_scope(receipt, run_id):
            continue
        attestation_path = run_dir / "attestation.json"
        if not attestation_path.is_file():
            continue
        result = attestation.verify_attestation(
            attestation_path,
            target=target,
            require_receipt=True,
        )
        if result.status == attestation.STATUS_SIGNED_OK and result.rederived:
            return "evidenced_passed"
        # A present but unverifiable attestation is a failed control.
        return "evidenced_failed"
    return "untested"


def _evaluate_cosign_bundle_exists(target: Path, run_id: str | None) -> str:
    """EC-03 state: cosign Sigstore bundle with correct mediaType and dsseEnvelope."""
    dirs = _verify_receipt_dirs(target)
    if not dirs:
        return "untested"
    for run_dir in dirs:
        receipt = _read_json_object(run_dir / "receipt.json")
        if receipt is not None and not _receipt_matches_scope(receipt, run_id):
            continue
        path = run_dir / "attestation.sigstore.json"
        if not path.is_file():
            continue
        bundle = _read_json_object(path)
        if (
            isinstance(bundle, dict)
            and bundle.get("mediaType") == cosign_attestation.SIGSTORE_BUNDLE_MEDIA_TYPE
            and isinstance(bundle.get("dsseEnvelope"), dict)
        ):
            return "evidenced_passed"
        return "evidenced_failed"
    return "untested"


def _evaluate_agent_request_signed(target: Path, run_id: str | None) -> str:
    """EC-04 state: a signed agent-request envelope verifies."""
    for run_dir in _run_dirs(target):
        if run_id is not None and run_dir.name != run_id:
            continue
        for candidate in ("agent-request.json", "request.json"):
            path = run_dir / candidate
            if not path.is_file():
                continue
            result = attestation.verify_attestation(
                path,
                target=target,
                expected_predicate_type=agent_request.AGENT_REQUEST_PREDICATE_TYPE,
            )
            if result.status == attestation.STATUS_SIGNED_OK:
                return "evidenced_passed"
            return "evidenced_failed"
    return "untested"


def _evaluate_human_approval_allow_with_sod(target: Path, run_id: str | None) -> str:
    """EC-05 state: a run has a verified allow approval with SOD passed."""
    for run_dir in _run_dirs(target):
        if run_id is not None and run_dir.name != run_id:
            continue
        verification = approval.verify_run_approval(target, run_dir)
        if verification.status == "APPROVED":
            sod = verification.sod or {}
            if sod.get("result") == "PASSED":
                return "evidenced_passed"
            return "evidenced_failed"
        if verification.status != "UNAPPROVED":
            # An approval artifact exists but did not pass.
            return "evidenced_failed"
    return "untested"


def _evaluate_run_event_journal_exists(target: Path, run_id: str | None) -> str:
    """EC-06 state: a run lifecycle journal with a valid chain exists."""
    for run_dir in _run_dirs(target):
        if run_id is not None and run_dir.name != run_id:
            continue
        journal_path = run_dir / "events" / "lifecycle.jsonl"
        if not journal_path.is_file():
            continue
        report = run_journal.read_journal(journal_path)
        if report.chain_errors:
            return "evidenced_failed"
        if report.events:
            return "evidenced_passed"
    return "untested"


def _evaluate_governance_inventory_exists(target: Path, run_id: str | None) -> str:
    """EC-07 state: a governance inventory artifact exists."""
    del run_id  # whole-workspace artifact
    for path in (
        target / ".brigade" / "governance" / "inventory.json",
        target / "governance-inventory.json",
    ):
        if path.is_file() and _read_json_object(path) is not None:
            return "evidenced_passed"
    return "untested"


def _evaluate_commit_trailer_receipts(target: Path, run_id: str | None) -> str:
    """EC-08 state: recent Git history contains matching Brigade-Run/Receipt trailers."""
    del run_id  # whole-workspace artifact
    if not _is_git_repo(target):
        return "not_applicable"
    try:
        result = subprocess.run(
            ["git", "log", "-20", "--format=%H"],
            cwd=str(target),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return "untested"
    except (OSError, subprocess.TimeoutExpired):
        return "untested"

    commits = [sha for sha in result.stdout.splitlines() if sha]
    any_trailer = False
    for sha in commits:
        try:
            msg_out = subprocess.run(
                ["git", "log", "-1", "--format=%B", sha],
                cwd=str(target),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if msg_out.returncode != 0:
                continue
        except (OSError, subprocess.TimeoutExpired):
            continue
        msg = msg_out.stdout
        run_id_value: str | None = None
        expected_digest: str | None = None
        for line in msg.splitlines():
            if line.startswith("Brigade-Run: "):
                run_id_value = line[len("Brigade-Run: ") :].strip()
            elif line.startswith("Brigade-Receipt: sha256:"):
                expected_digest = line[len("Brigade-Receipt: sha256:") :].strip()
        if not run_id_value or not expected_digest:
            continue
        any_trailer = True
        if not receipts_trailer._is_bare_run_id(run_id_value):
            continue
        run_json = target / ".brigade" / "runs" / run_id_value / "run.json"
        if not run_json.is_file():
            continue
        receipt = _read_json_object(run_json)
        if receipt is None:
            continue
        try:
            actual_digest = causal_receipt.receipt_digest(receipt)
        except Exception:
            continue
        if actual_digest == expected_digest:
            return "evidenced_passed"
    return "evidenced_failed" if any_trailer else "untested"


def _evaluate_guard_audit_allow(target: Path, run_id: str | None) -> str:
    """EC-09 state: a content-guard audit artifact exists and is not blocked."""
    del run_id  # whole-workspace artifact
    path = target / ".brigade" / "work" / "guard" / "audit.json"
    if not path.is_file():
        return "untested"
    payload = _read_json_object(path)
    # Missing summary or malformed artifact is "untested", not a failing control.
    if not isinstance(payload, dict):
        return "untested"
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return "untested"
    if summary.get("blocked") is True:
        return "evidenced_failed"
    return "evidenced_passed"


def _evaluate_outcome_record_exists(target: Path, run_id: str | None) -> str:
    """EC-10 state: outcome ledger records exist and are non-empty."""
    del run_id  # whole-workspace artifact
    path = target / "memory" / "outcome" / "records.jsonl"
    if not path.is_file():
        return "untested"
    try:
        text = path.read_text()
    except OSError:
        return "untested"
    if any(line.strip() for line in text.splitlines()):
        return "evidenced_passed"
    return "untested"


def _evaluate_verify_archive_index_exists(target: Path, run_id: str | None) -> str:
    """EC-11 state: verify archive index exists and is parseable JSONL."""
    del run_id  # whole-workspace artifact
    path = target / ".brigade" / "work" / "verify-archive" / "index.jsonl"
    if not path.is_file():
        return "untested"
    records = localio.read_jsonl_dicts(path)
    if records:
        return "evidenced_passed"
    return "untested"


def _evaluate_artifact_absent_until_merged(target: Path, run_id: str | None) -> str:
    """EC-12 state: the evidence package artifact is not yet implemented."""
    del target, run_id
    return "untested"


_EVALUATORS: dict[str, Any] = {
    "verify-receipt-completed": _evaluate_verify_receipt_completed,
    "sshsig-test-result-signed-ok": _evaluate_sshsig_test_result_signed_ok,
    "cosign-bundle-exists": _evaluate_cosign_bundle_exists,
    "agent-request-signed": _evaluate_agent_request_signed,
    "human-approval-allow-with-sod": _evaluate_human_approval_allow_with_sod,
    "run-event-journal-exists": _evaluate_run_event_journal_exists,
    "governance-inventory-exists": _evaluate_governance_inventory_exists,
    "commit-trailer-receipts": _evaluate_commit_trailer_receipts,
    "guard-audit-allow": _evaluate_guard_audit_allow,
    "outcome-record-exists": _evaluate_outcome_record_exists,
    "verify-archive-index-exists": _evaluate_verify_archive_index_exists,
    "artifact-absent-until-merged": _evaluate_artifact_absent_until_merged,
}


def _evaluate_claim_state(target: Path, claim_id: str, run_id: str | None) -> str:
    """Compute the evidentiary state for a single claim."""
    state_rule = _STATE_RULES.get(claim_id)
    if state_rule is None:
        raise ControlCrosswalkError(f"unknown claim: {claim_id}")
    evaluator = _EVALUATORS.get(state_rule)
    if evaluator is None:
        raise ControlCrosswalkError(f"unknown state_rule: {state_rule}")
    state = evaluator(target, run_id)
    if state not in VALID_STATES:
        raise ControlCrosswalkError(f"invalid state {state!r} for {claim_id}")
    return state


def evaluate_controls(
    target: Path,
    *,
    run_id: str | None = None,
    framework_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate evidentiary states for the crosswalk mappings in scope."""
    target = target.expanduser().resolve()
    crosswalk = load_crosswalk()
    claims_by_id = {c["id"]: c for c in crosswalk["claims"]}
    frameworks_by_id = {f["id"]: f for f in crosswalk["frameworks"]}
    states: dict[str, str] = {}
    for claim in crosswalk["claims"]:
        states[claim["id"]] = _evaluate_claim_state(target, claim["id"], run_id)
    mappings: list[dict[str, Any]] = []
    for mapping in crosswalk["mappings"]:
        if framework_id is not None and mapping["framework_id"] != framework_id:
            continue
        claim = claims_by_id[mapping["claim_id"]]
        framework = frameworks_by_id[mapping["framework_id"]]
        state = states[mapping["claim_id"]]
        if mapping["relationship"] == "no-relationship":
            state = "not_applicable"
        mappings.append(
            {
                "claim_id": mapping["claim_id"],
                "claim_title": claim["title"],
                "framework_id": mapping["framework_id"],
                "framework_name": framework["name"],
                "control_id": mapping["control_id"],
                "relationship": mapping["relationship"],
                "obligation_bearer": mapping["obligation_bearer"],
                "applicability_condition": mapping["applicability_condition"],
                "rationale": mapping["rationale"],
                "source_locator": mapping["source_locator"],
                "state": state,
            }
        )
    mappings.sort(key=lambda m: (m["framework_id"], m["control_id"], m["claim_id"]))
    scope = f"run:{run_id}" if run_id else "workspace"
    return {
        "schema": EVIDENCE_CONTROLS_SCHEMA,
        "crosswalk_version": crosswalk["crosswalk_version"],
        "evaluated_at": localio.utc_now_iso(),
        "scope": scope,
        "notice": HEADER_NOTICE,
        "mappings": mappings,
    }


def render_doc(evaluated: dict[str, Any]) -> str:
    """Render evaluated crosswalk as markdown documentation."""
    lines: list[str] = []
    lines.append("# Brigade Control Crosswalk")
    lines.append("")
    lines.append(HEADER_NOTICE)
    lines.append("")
    lines.append(f"Crosswalk version: {evaluated['crosswalk_version']}. Schema: `{evaluated['schema']}`.")
    lines.append("")
    lines.append(f"Scope: {evaluated['scope']}.")
    lines.append("")
    by_framework: dict[str, list[dict[str, Any]]] = {}
    for mapping in evaluated["mappings"]:
        by_framework.setdefault(mapping["framework_id"], []).append(mapping)
    crosswalk = load_crosswalk()
    frameworks_by_id = {f["id"]: f for f in crosswalk["frameworks"]}
    for fw_id in sorted(frameworks_by_id):
        rows = by_framework.get(fw_id, [])
        framework = frameworks_by_id[fw_id]
        lines.append(f"## {framework['name']} (`{fw_id}`)")
        lines.append("")
        if not rows:
            lines.append(f"Not mapped in crosswalk version {evaluated['crosswalk_version']}.")
        else:
            lines.append("| Control | Claim | Relationship | Obligation | Applicability | State | Rationale | Source |")
            lines.append("|---------|-------|--------------|------------|---------------|-------|-----------|--------|")
            for row in rows:
                control = f"`{row['control_id']}`"
                claim = f"{row['claim_id']}"
                rel = row["relationship"]
                obl = row["obligation_bearer"]
                app = row["applicability_condition"]
                state = row["state"]
                rationale = row["rationale"]
                source = row["source_locator"]
                lines.append(f"| {control} | {claim} | {rel} | {obl} | {app} | {state} | {rationale} | {source} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def controls(
    target: Path,
    *,
    run_id: str | None = None,
    framework_id: str | None = None,
    json_output: bool = False,
    render_doc_path: Path | None = None,
) -> int:
    """CLI entry for `brigade evidence controls`."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    evaluated = evaluate_controls(target, run_id=run_id, framework_id=framework_id)
    if render_doc_path is not None:
        render_doc_path = render_doc_path.expanduser().resolve()
        render_doc_path.parent.mkdir(parents=True, exist_ok=True)
        localio.write_text_atomic(render_doc_path, render_doc(evaluated))
    if json_output:
        print(json.dumps(evaluated, indent=2, sort_keys=True))
        return 0
    print(f"evidence controls: {target}")
    print(HEADER_NOTICE)
    print(f"scope: {evaluated['scope']}")
    by_framework: dict[str, list[dict[str, Any]]] = {}
    for mapping in evaluated["mappings"]:
        by_framework.setdefault(mapping["framework_id"], []).append(mapping)
    crosswalk = load_crosswalk()
    frameworks_by_id = {f["id"]: f for f in crosswalk["frameworks"]}
    for fw_id in sorted(frameworks_by_id):
        rows = by_framework.get(fw_id, [])
        print(f"\n{frameworks_by_id[fw_id]['name']} ({fw_id})")
        if not rows:
            print(f"  not mapped in crosswalk version {evaluated['crosswalk_version']}")
        else:
            for row in rows:
                print(
                    f"  [{row['state']}] {row['control_id']} -> {row['claim_id']} "
                    f"({row['relationship']}, {row['obligation_bearer']}, {row['applicability_condition']})"
                )
    return 0
