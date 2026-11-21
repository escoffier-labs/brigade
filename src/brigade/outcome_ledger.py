"""Outcome ledger for skill promotion and rollback decisions (#2589, #2590).

The ledger is one tracked JSON file, ``outcome-ledger/skills.json``. It records, per
skill, which texts the owner side has approved, the CI trials observed on main, and the
decision that follows from them. Everything in it is a pure function of three things:
the previous ledger on main, the git history of main, and the Actions API. The
scheduled writer builds it and the required ``ledger-recompute`` check rebuilds it from
trusted main, so a hand-edited ledger cannot match.

Decisions reuse ``scorecard.decide_scorecard`` and ``scorecard.classify_band`` with the
default ``ReconcileConfig``. This module only turns CI trials into the scorecard those
functions read, and adds the two ledger rules they do not know about:

- New skill text that was never approved cannot be promoted. It waits for owner approval.
- A regression restores the last approved text. Restoring is a rollback, not a hold.

Trials come only from ``outcome_ledger_actions`` (Actions API jobs, never check runs).
Git reads come only from ``outcome_ledger_git`` (commit ids, never a working tree).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import outcome as outcome_core, scorecard, shipped_history, verify_manifest
from .outcome_ledger_actions import (
    GhApi,
    has_completed_verdict,
    owner_review_ok,
    parse_time,
    trial_for_commit,
)
from .outcome_ledger_git import GitError, Repo, bundle_fingerprint, is_sha

SCHEMA = "brigade.outcome_ledger.v1"
LEDGER_PATH = "outcome-ledger/skills.json"
LEDGER_DIR = "outcome-ledger/"
GENESIS_PREV = "0" * 64
MANIFESTS_DIR = "verify/manifests"
SHIPPED_HISTORY_PATH = "src/brigade/templates/shipped-file-history.json"
# Visibility control, not an access wall: see outcome_ledger_actions.
OWNER_LOGINS = ("solomonneas",)
BASES = ("release", "owner_review")
STATUSES = ("candidate", "promoted", "demoted")
CLOCK_SKEW = dt.timedelta(minutes=5)


class LedgerError(Exception):
    """The ledger or its inputs are not valid."""


@dataclass(frozen=True)
class Evidence:
    """Where the ledger reads facts from. Tests replace ``gh`` and ``repo``."""

    repo: Repo
    gh: GhApi
    slug: str
    repo_id: int


def canonical_bytes(ledger: Mapping[str, Any]) -> bytes:
    """The one byte form of a ledger. A ledger file is valid only in this form."""
    return (json.dumps(ledger, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def policy_block(config: outcome_core.ReconcileConfig) -> dict[str, str]:
    payload = json.dumps(dataclasses.asdict(config), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"scorecard_policy": scorecard.SCORECARD_POLICY_VERSION, "config_sha256": sha256_hex(payload)}


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_ledger(raw: bytes) -> dict[str, Any]:
    """Parse ledger bytes and check the shape. Raise ``LedgerError`` naming the first problem."""
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise LedgerError(f"ledger is not valid JSON: {exc}") from exc
    errors = shape_errors(payload)
    if errors:
        raise LedgerError("; ".join(errors[:5]))
    return payload  # type: ignore[no-any-return]


def shape_errors(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return ["ledger must be an object"]
    errors: list[str] = []
    if payload.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    prev = payload.get("prev_ledger_sha256")
    if not (isinstance(prev, str) and len(prev) == 64):
        errors.append("prev_ledger_sha256 must be a sha256 hex digest")
    covers = payload.get("covers")
    if not (isinstance(covers, dict) and is_sha(covers.get("main_sha")) and parse_time(covers.get("cutoff_at"))):
        errors.append("covers needs main_sha and cutoff_at")
    if not isinstance(payload.get("policy"), dict):
        errors.append("policy must be an object")
    subjects = payload.get("subjects")
    if not isinstance(subjects, dict) or not subjects:
        errors.append("subjects must be a non-empty object")
        return errors
    for key, subject in subjects.items():
        if not (isinstance(key, str) and key.startswith("skill:") and isinstance(subject, dict)):
            errors.append(f"subject {key!r} must be skill:<id> with an object body")
            continue
        for field in ("manifest_id", "subject_path"):
            if not (isinstance(subject.get(field), str) and subject[field]):
                errors.append(f"{key}: {field} must be a string")
        if not is_sha(subject.get("since_commit")):
            errors.append(f"{key}: since_commit must be a commit id")
        approved = subject.get("approved")
        if not (isinstance(approved, list) and approved and all(isinstance(row, dict) for row in approved)):
            errors.append(f"{key}: approved must be a non-empty list of objects")
        if not isinstance(subject.get("demoted"), list):
            errors.append(f"{key}: demoted must be a list")
    return errors


@dataclass(frozen=True)
class SubjectDecision:
    """The outcome of the decision rule for one skill's current text."""

    status: str
    band: str
    action: str
    code: str | None
    reason: str
    restore_to: str | None = None
    demote_current: bool = False


def scorecard_from_trials(artifact_id: str, fingerprint: str, helped: int, hurt: int) -> scorecard.SubjectScorecard:
    """A receipt-free scorecard carrying only CI-attested effectiveness, which is all the decision reads."""
    trials = helped + hurt
    return scorecard.SubjectScorecard(
        subject=scorecard.SubjectRef(artifact_kind="skill", artifact_id=artifact_id, content_fingerprint=fingerprint),
        dimensions={
            "effectiveness": {
                "helped": helped,
                "hurt": hurt,
                "wilson": outcome_core.wilson_lower_bound(helped, trials),
                "trials": trials,
                "local_trials": 0,
                "attested_trials": trials,
            },
            "verifier_cost": {},
            "retry_stability": {},
            "evidence_integrity": {},
        },
        utility_guardrails={
            "required_check_ids": [],
            "passing_trials": 0,
            "required_trials": scorecard._UTILITY_REQUIRED_TRIALS,
            "per_check": {},
        },
    )


def decide_subject(
    *,
    artifact_id: str,
    fingerprint: str,
    approved: Sequence[str],
    demoted: Sequence[str],
    prior_status: str | None,
    trials: Sequence[Mapping[str, Any]],
    last_action_at: dt.datetime | None,
    cutoff: dt.datetime,
    config: outcome_core.ReconcileConfig,
) -> SubjectDecision:
    """Apply the scorecard rule to the current text, then the two ledger rules.

    ``approved`` is ordered oldest first. ``trials`` covers every cohort and is filtered
    here to the current fingerprint. ``prior_status`` is the status the previous ledger
    recorded for this same text, or None for a text the ledger has not seen current.
    """
    cohort = [row for row in trials if row.get("fingerprint") == fingerprint]
    helped = sum(1 for row in cohort if row.get("outcome") == "helped")
    hurt = sum(1 for row in cohort if row.get("outcome") == "hurt")
    is_approved = fingerprint in approved
    is_demoted = fingerprint in demoted
    status_in = (
        "demoted" if is_demoted else (prior_status if prior_status in {"candidate", "promoted"} else "candidate")
    )
    if status_in == "promoted" and not is_approved:
        status_in = "candidate"
    card = scorecard_from_trials(artifact_id, fingerprint, helped, hurt)
    verdict = scorecard.decide_scorecard(
        card,
        artifact_id=artifact_id,
        current_status=status_in,
        last_action_ts=last_action_at,
        now=cutoff,
        config=config,
    )
    target = next((fp for fp in reversed(list(approved)) if fp != fingerprint and fp not in demoted), None)

    def banded(status: str) -> str:
        marker = scorecard.SCORECARD_POLICY_VERSION if status == "promoted" else None
        return scorecard.classify_band(card, persisted_status=status, policy_marker=marker)

    def restore(code: str, reason: str, *, demote: bool) -> SubjectDecision:
        status = "demoted" if demote or is_demoted else status_in
        if target is None:
            return SubjectDecision(
                status,
                banded(status),
                "hold",
                "no_approved_target",
                f"{reason}; no approved text to restore",
                None,
                demote,
            )
        return SubjectDecision(status, banded(status), "rollback", code, reason, target, demote)

    if verdict.code == outcome_core.REGRESSION_ROLLBACK:
        return restore(outcome_core.REGRESSION_ROLLBACK, verdict.reason, demote=True)
    if hurt > 0 and not is_approved:
        return restore("regression_restore", "verified regression on text that was never approved", demote=False)
    if is_demoted:
        return restore("demoted_text_current", "demoted text is still current", demote=False)
    if verdict.action == "install":
        if is_approved:
            return SubjectDecision("promoted", banded("promoted"), "install", verdict.code, verdict.reason)
        return SubjectDecision(
            "candidate", banded("candidate"), "hold", "awaiting_owner", "attested, but this text was never approved"
        )
    status = verdict.new_status if verdict.new_status in {"candidate", "promoted", "demoted"} else status_in
    return SubjectDecision(status, banded(status), verdict.action, verdict.code, verdict.reason)


@dataclass(frozen=True)
class SubjectDef:
    """The inputs a ledger takes on faith for one skill. ``approved`` is checked, never trusted."""

    key: str
    manifest_id: str
    subject_path: str
    since_commit: str
    approved: tuple[dict[str, Any], ...]


def defs_from_ledger(ledger: Mapping[str, Any]) -> dict[str, SubjectDef]:
    return {
        key: SubjectDef(
            key=key,
            manifest_id=body["manifest_id"],
            subject_path=body["subject_path"],
            since_commit=body["since_commit"],
            approved=tuple(body["approved"]),
        )
        for key, body in ledger["subjects"].items()
    }


def skill_dir(subject_path: str) -> str:
    if not subject_path.endswith("/SKILL.md") or ".." in subject_path.split("/") or subject_path.startswith("/"):
        raise LedgerError(f"subject_path must be a repo-relative path ending in /SKILL.md: {subject_path!r}")
    return subject_path.rsplit("/", 1)[0]


def _load_manifest(repo: Repo, commit: str, definition: SubjectDef) -> tuple[verify_manifest.VerifyManifest, str]:
    raw = repo.show(commit, f"{MANIFESTS_DIR}/{definition.manifest_id}.json")
    if raw is None:
        raise LedgerError(f"{definition.key}: manifest {definition.manifest_id} is missing at {commit[:12]}")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
        manifest = verify_manifest.manifest_from_payload(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise LedgerError(f"{definition.key}: manifest is invalid: {exc}") from exc
    if (
        manifest.manifest_id != definition.manifest_id
        or manifest.artifact_kind != "skill"
        or manifest.subject_path != definition.subject_path
        or f"skill:{manifest.artifact_id}" != definition.key
    ):
        raise LedgerError(f"{definition.key}: manifest does not describe this subject")
    return manifest, sha256_hex(raw)


def _snapshot_fingerprint(repo: Repo, commit: str, directory: str) -> str | None:
    try:
        return bundle_fingerprint(repo.snapshot(commit, directory))
    except GitError:
        return None


def collect_trials(
    ev: Evidence,
    definition: SubjectDef,
    manifest: verify_manifest.VerifyManifest,
    pin: str,
    covers: str,
    cutoff: dt.datetime,
) -> list[dict[str, Any]]:
    """Trials on main commits after ``since_commit`` that touched the manifest scope under the pinned manifest."""
    directory = skill_dir(definition.subject_path)
    trials: list[dict[str, Any]] = []
    for sha, paths in ev.repo.first_parent_touching(definition.since_commit, covers):
        if not any(verify_manifest.path_in_manifest_scope(manifest, path) for path in paths):
            continue
        at_commit = ev.repo.show(sha, f"{MANIFESTS_DIR}/{definition.manifest_id}.json")
        if at_commit is None or sha256_hex(at_commit) != pin:
            continue
        fingerprint = _snapshot_fingerprint(ev.repo, sha, directory)
        if fingerprint is None:
            continue
        trial = trial_for_commit(ev.gh, ev.slug, ev.repo_id, definition.manifest_id, sha, cutoff)
        if trial is not None:
            trials.append({"fingerprint": fingerprint, **trial})
    return trials


def build_ledger(
    ev: Evidence,
    *,
    prev: Mapping[str, Any] | None,
    prev_sha256: str,
    definitions: Mapping[str, SubjectDef],
    covers_sha: str,
    cutoff_at: str,
    config: outcome_core.ReconcileConfig | None = None,
) -> dict[str, Any]:
    """The ledger that follows from ``prev``, main's history up to ``covers_sha`` and CI trials finished by ``cutoff_at``."""
    config = config or outcome_core.ReconcileConfig()
    cutoff = parse_time(cutoff_at)
    if cutoff is None:
        raise LedgerError("cutoff_at is not a UTC timestamp")
    prev_subjects = prev["subjects"] if prev else {}
    subjects: dict[str, Any] = {}
    for key in sorted(definitions):
        definition = definitions[key]
        directory = skill_dir(definition.subject_path)
        manifest, pin = _load_manifest(ev.repo, covers_sha, definition)
        fingerprint = _snapshot_fingerprint(ev.repo, covers_sha, directory)
        if fingerprint is None:
            raise LedgerError(f"{key}: skill bundle is missing at {covers_sha[:12]}")
        trials = collect_trials(ev, definition, manifest, pin, covers_sha, cutoff)
        before = prev_subjects.get(key) or {}
        demoted: list[str] = list(before.get("demoted", []))
        same_cohort = (before.get("current") or {}).get("fingerprint") == fingerprint
        last_action_text = before.get("last_action_at") if same_cohort else None
        decision = decide_subject(
            artifact_id=key.removeprefix("skill:"),
            fingerprint=fingerprint,
            approved=[row["fingerprint"] for row in definition.approved],
            demoted=demoted,
            prior_status=before.get("status") if same_cohort else None,
            trials=trials,
            last_action_at=parse_time(last_action_text),
            cutoff=cutoff,
            config=config,
        )
        if decision.demote_current and fingerprint not in demoted:
            demoted.append(fingerprint)
        acted = decision.action in {"install", "rollback"}
        restore_row = None
        if decision.restore_to is not None:
            target = next(row for row in definition.approved if row["fingerprint"] == decision.restore_to)
            restore_row = {
                "from_commit": covers_sha,
                "from_fingerprint": fingerprint,
                "to_commit": target["commit"],
                "to_fingerprint": target["fingerprint"],
            }
        subjects[key] = {
            "manifest_id": definition.manifest_id,
            "manifest_sha256": pin,
            "subject_path": definition.subject_path,
            "since_commit": definition.since_commit,
            "approved": list(definition.approved),
            "demoted": demoted,
            "current": {"fingerprint": fingerprint, "commit": covers_sha},
            "status": decision.status,
            "band": decision.band,
            "trials": trials,
            "decision": {
                "action": decision.action,
                "code": decision.code,
                "reason": decision.reason,
                "decided_at": cutoff_at,
            },
            "last_action_at": cutoff_at if acted else last_action_text,
            "restore": restore_row,
        }
    return {
        "schema": SCHEMA,
        "prev_ledger_sha256": prev_sha256,
        "covers": {"main_sha": covers_sha, "cutoff_at": cutoff_at},
        "policy": policy_block(config),
        "subjects": subjects,
    }


@dataclass(frozen=True)
class EventContext:
    """What the CI event says about the proposed change. ``pr_number`` is None when it is not known."""

    event: str
    pr_number: int | None


def _approved_errors(
    ev: Evidence,
    key: str,
    definition: SubjectDef,
    previous: Sequence[Mapping[str, Any]],
    covers: str,
    context: EventContext,
    owners: Sequence[str],
) -> list[str]:
    errors: list[str] = []
    rows = definition.approved
    if list(rows[: len(previous)]) != list(previous):
        errors.append(f"{key}: approved entries may only be appended")
    directory = skill_dir(definition.subject_path)
    try:
        history = shipped_history.parse_manifest(ev.repo.show(covers, SHIPPED_HISTORY_PATH) or b"")
    except ValueError:
        history = {}
    released = {
        entry.sha256 for entry in history.get(shipped_history.skill_key(key.removeprefix("skill:")), ()) if entry.tag
    }
    seen: set[str] = set()
    for index, row in enumerate(rows):
        label = f"{key}: approved[{index}]"
        basis = row.get("basis")
        commit = row.get("commit")
        fingerprint = row.get("fingerprint")
        expected_fields = {"fingerprint", "skill_md_sha256", "commit", "basis"} | (
            {"pr"} if basis == "owner_review" else set()
        )
        if set(row) != expected_fields or basis not in BASES or not is_sha(commit) or not isinstance(fingerprint, str):
            errors.append(f"{label} is malformed")
            continue
        if fingerprint in seen:
            errors.append(f"{label} repeats a fingerprint")
        seen.add(fingerprint)
        if not ev.repo.is_ancestor(commit, covers):
            errors.append(f"{label}: commit is not in main history")
            continue
        if _snapshot_fingerprint(ev.repo, commit, directory) != fingerprint:
            errors.append(f"{label}: bundle at the commit does not match the fingerprint")
            continue
        skill_md = ev.repo.show(commit, definition.subject_path)
        if skill_md is None or shipped_history.skill_digest(skill_md) != row.get("skill_md_sha256"):
            errors.append(f"{label}: SKILL.md digest does not match the commit")
            continue
        if basis == "release" and row["skill_md_sha256"] not in released:
            errors.append(f"{label}: SKILL.md is not a recorded release text")
        is_new = row not in previous
        if basis == "owner_review" and is_new:
            number = row.get("pr")
            if not isinstance(number, int) or number != context.pr_number:
                errors.append(f"{label}: owner approval must come from this pull request")
            elif not owner_review_ok(ev.gh, ev.slug, number, owners):
                errors.append(f"{label}: no owner approval on the head commit")
    return errors


def verify_ledger(
    ev: Evidence,
    *,
    raw: bytes,
    main_raw: bytes | None,
    main_ref: str,
    head_ref: str,
    context: EventContext,
    now: dt.datetime,
    owners: Sequence[str] = OWNER_LOGINS,
    config: outcome_core.ReconcileConfig | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> list[str]:
    """Recompute the ledger from trusted inputs and return every reason ``raw`` is not it. Empty means valid."""
    stage = on_stage or (lambda _name: None)
    try:
        proposed = parse_ledger(raw)
    except LedgerError as exc:
        return [str(exc)]
    errors: list[str] = []
    try:
        prev = parse_ledger(main_raw) if main_raw is not None else None
    except LedgerError as exc:
        return [f"ledger on trusted main is unreadable: {exc}"]
    expected_prev = sha256_hex(main_raw) if main_raw is not None else GENESIS_PREV
    if proposed["prev_ledger_sha256"] != expected_prev:
        errors.append("prev_ledger_sha256 does not match the ledger on trusted main")
    covers = proposed["covers"]["main_sha"]
    cutoff_text = proposed["covers"]["cutoff_at"]
    cutoff = parse_time(cutoff_text)
    assert cutoff is not None
    repo = ev.repo
    try:
        main_sha = repo.resolve(main_ref)
        head_sha = repo.resolve(head_ref)
        if not repo.is_ancestor(covers, main_sha):
            return errors + ["covers.main_sha is not in trusted main history"]
        if prev is not None and not repo.is_ancestor(prev["covers"]["main_sha"], covers):
            errors.append("covers.main_sha moved backwards")
        covers_time = parse_time(repo.commit_time(covers))
        if covers_time is None or cutoff < covers_time or cutoff > now + CLOCK_SKEW:
            errors.append("cutoff_at is outside the allowed window")
        if proposed.get("policy") != policy_block(config or outcome_core.ReconcileConfig()):
            errors.append("policy differs from the trusted scoring policy")
        definitions = defs_from_ledger(proposed)
        if prev is not None and set(prev["subjects"]) - set(definitions):
            errors.append("subjects may not be removed")
        stage("approved")
        for key, definition in definitions.items():
            before = (prev or {"subjects": {}})["subjects"].get(key)
            previous = before["approved"] if before else []
            if before and (before["since_commit"], before["manifest_id"], before["subject_path"]) != (
                definition.since_commit,
                definition.manifest_id,
                definition.subject_path,
            ):
                errors.append(f"{key}: since_commit, manifest_id and subject_path are fixed once recorded")
            if not repo.is_ancestor(definition.since_commit, covers):
                errors.append(f"{key}: since_commit is not in main history")
            errors += _approved_errors(ev, key, definition, previous, covers, context, owners)
        if errors:
            return errors
        stage("stale")
        for key, definition in definitions.items():
            manifest, _pin = _load_manifest(repo, covers, definition)
            for sha, paths in repo.first_parent_touching(covers, main_sha):
                if any(
                    verify_manifest.path_in_manifest_scope(manifest, path) for path in paths
                ) and has_completed_verdict(ev.gh, ev.slug, ev.repo_id, definition.manifest_id, sha, now):
                    return [f"{key}: ledger is stale, main commit {sha[:12]} already has a completed attest verdict"]
        stage("rebuild")
        expected = build_ledger(
            ev,
            prev=prev,
            prev_sha256=expected_prev,
            definitions=definitions,
            covers_sha=covers,
            cutoff_at=cutoff_text,
            config=config,
        )
        if canonical_bytes(expected) != raw:
            errors.append("ledger differs from the recomputed ledger")
        stage("paths")
        base = repo.merge_base(main_sha, head_sha)
        errors += _path_errors(repo, expected, base, head_sha)
    except Exception as exc:  # noqa: BLE001 - any failed read must fail the check closed
        errors.append(f"recompute could not finish: {type(exc).__name__}: {str(exc)[:200]}")
    return errors


def _path_errors(repo: Repo, expected: Mapping[str, Any], base: str, head: str) -> list[str]:
    """A ledger change may touch the ledger file and the restored bundle, and nothing else."""
    errors: list[str] = []
    restores = {
        key: body["restore"] for key, body in expected["subjects"].items() if isinstance(body.get("restore"), dict)
    }
    bundle_dirs = {skill_dir(expected["subjects"][key]["subject_path"]) + "/": row for key, row in restores.items()}
    for path in repo.changed_paths(base, head):
        if path == LEDGER_PATH:
            continue
        if any(path.startswith(prefix) for prefix in bundle_dirs):
            continue
        errors.append(f"a ledger change may not touch {path}")
    for prefix, row in bundle_dirs.items():
        directory = prefix.rstrip("/")
        if repo.snapshot(head, directory) != repo.snapshot(row["to_commit"], directory):
            errors.append(f"{directory}: bundle is not the approved text restored byte for byte")
    return errors
