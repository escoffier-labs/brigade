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

A commit that needs an attest verdict and has none counts against the text, so deleting or
cancelling a run never helps. See ``outcome_ledger_trials``.

A ledger carries forward trials under the same manifest pin and reads the Actions API only
for newer commits. A pin change restarts the cohort, and every hurt verdict before it is kept.

Trials come only from ``outcome_ledger_actions`` (Actions API jobs, never check runs).
Git reads come only from ``outcome_ledger_git`` (commit ids, never a working tree).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import outcome as outcome_core, scorecard, shipped_history, template_profiles, verify_manifest
from .outcome_ledger_actions import GhApi, owner_review_ok, parse_time
from .outcome_ledger_git import Repo, bundle_fingerprint, is_sha
from .outcome_ledger_trials import MANIFESTS_DIR, TrialPlan, is_hurt_trial, plan_trials, scope_history

SCHEMA = "brigade.outcome_ledger.v1"
LEDGER_PATH = "outcome-ledger/skills.json"
LEDGER_DIR = "outcome-ledger/"
GENESIS_PREV = "0" * 64
SHIPPED_HISTORY_PATH = "src/brigade/templates/shipped-file-history.json"
# Visibility control, not an access wall: see outcome_ledger_actions.
OWNER_LOGINS = ("solomonneas",)
BASES = ("release", "owner_review")
STATUSES = ("candidate", "promoted", "demoted")
CLOCK_SKEW = dt.timedelta(minutes=5)
# How far behind the check's clock a ledger's cutoff may sit. A writer that runs at least
# daily rebuilds well inside a day, and a pull request normally merges the same day, so a
# day is room for an overnight wait. The completeness check is the real defense against a
# parked cutoff. This bound only keeps a ledger from sitting on old evidence.
MAX_LEDGER_AGE = dt.timedelta(hours=24)


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
    """The bundle fingerprint at ``commit``, or None when the directory holds no skill content.

    A bundle that exists but cannot be read (a symlink, a missing blob, a failed git read)
    raises ``GitError`` instead of reading as absent.
    """
    return bundle_fingerprint(repo.snapshot(commit, directory))


def _seed_for(prev: Mapping[str, Any] | None, key: str) -> tuple[list[dict[str, Any]], str | None]:
    """The recorded trials and the commit through which they are complete."""
    if prev is None:
        return [], None
    before = prev["subjects"].get(key) or {}
    rows = before.get("trials") or []
    return [dict(row) for row in rows if isinstance(row, dict)], prev["covers"]["main_sha"]


def _plan(
    ev: Evidence,
    definition: SubjectDef,
    manifest: verify_manifest.VerifyManifest,
    covers: str,
    ref: dt.datetime,
    seed_tip: str | None,
) -> TrialPlan:
    chain = ev.repo.first_parent(definition.since_commit, covers)
    if seed_tip is not None:
        if seed_tip in chain:
            chain = chain[chain.index(seed_tip) + 1 :]
        else:
            chain = [sha for sha in chain if not ev.repo.is_ancestor(sha, seed_tip)]
    directory = skill_dir(definition.subject_path)
    return plan_trials(
        ev.gh,
        ev.slug,
        ev.repo_id,
        ev.repo,
        manifest=manifest,
        chain=chain,
        ref=ref,
        fingerprint_at=lambda sha: _snapshot_fingerprint(ev.repo, sha, directory),
    )


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
        seed, seed_tip = _seed_for(prev, key)
        plan = _plan(ev, definition, manifest, covers_sha, cutoff, seed_tip)
        before = prev_subjects.get(key) or {}
        if plan.restarted or before.get("manifest_sha256") != pin:
            seed = [row for row in seed if is_hurt_trial(row)]
        trials = seed + list(plan.trials)
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
    landed: bool = False,
    change_range: tuple[str, str] | None = None,
) -> list[str]:
    """Recompute the ledger from trusted inputs and return every reason ``raw`` is not it. Empty means valid.

    ``landed`` is for a change already on main. Other commits may have merged alongside it and
    the pull request is over, so the age limit and the rule on which paths a change may touch no
    longer apply. Every rule about the ledger itself still does, restored bundle included.
    A trusted merge group can supply the individual ledger change's range for the path check.
    The final group head must still contain the exact restored bundles.
    """
    stage = on_stage or (lambda _name: None)
    ev = dataclasses.replace(ev, gh=_memoized(ev.gh))
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
        elif not landed and now - cutoff > MAX_LEDGER_AGE:
            errors.append("cutoff_at is older than the allowed ledger age")
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
        uncovered = repo.first_parent(covers, main_sha)
        for key, definition in definitions.items():
            manifest, _pin = _load_manifest(repo, covers, definition)
            scope = scope_history(repo, uncovered, manifest)
            if scope.commits:
                sha = scope.commits[0].sha
                errors.append(f"{key}: ledger is stale, main commit {sha[:12]} is in scope after covers.main_sha")
            current = proposed["subjects"][key].get("current")
            fingerprint = current.get("fingerprint") if isinstance(current, dict) else None
            if fingerprint is None or fingerprint != _snapshot_fingerprint(
                repo, main_sha, skill_dir(definition.subject_path)
            ):
                errors.append(f"{key}: current.fingerprint does not match the bundle on trusted main")
        if errors:
            return errors
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
            if prev is not None:
                for key in prev["subjects"]:
                    errors += _trial_history_errors(
                        repo, key, expected["subjects"][key], proposed["subjects"][key], prev["covers"]["main_sha"]
                    )
        stage("complete")
        errors += _completeness_errors(ev, definitions, expected, prev, covers, now)
        stage("paths")
        base, change_head = change_range or (repo.merge_base(main_sha, head_sha), head_sha)
        errors += _path_errors(repo, expected, base, change_head, head_sha, any_other_path=landed)
    except Exception as exc:  # noqa: BLE001 - any failed read must fail the check closed
        errors.append(f"recompute could not finish: {type(exc).__name__}: {str(exc)[:200]}")
    return errors


def _memoized(gh: GhApi) -> GhApi:
    """Answer a repeated endpoint once. A read that raises is not remembered."""
    seen: dict[str, Any] = {}

    def read(endpoint: str) -> Any:
        if endpoint not in seen:
            seen[endpoint] = gh(endpoint)
        return seen[endpoint]

    return read


def _trial_history_errors(
    repo: Repo, key: str, expected: Mapping[str, Any], proposed: Mapping[str, Any], prev_tip: str
) -> list[str]:
    """Carried trials must match the recomputed history, including across a cohort restart."""
    new = proposed.get("trials")
    if not isinstance(new, list):
        return [f"{key}: trials must be a list"]
    kept = []
    for row in new:
        if not (isinstance(row, dict) and is_sha(row.get("sha"))):
            return [f"{key}: a trial row is malformed"]
        if repo.is_ancestor(row["sha"], prev_tip):
            kept.append(row)
    carried = [row for row in expected["trials"] if repo.is_ancestor(row["sha"], prev_tip)]
    if kept != carried:
        return [f"{key}: trials for commits the previous ledger covered may not change"]
    return []


def _completeness_errors(
    ev: Evidence,
    definitions: Mapping[str, SubjectDef],
    expected: Mapping[str, Any],
    prev: Mapping[str, Any] | None,
    covers: str,
    now: dt.datetime,
) -> list[str]:
    """Fail when the evidence as of ``now`` holds a verdict the ledger lacks or reads differently.

    The ledger's own ``cutoff_at`` cannot decide what counts, or a cutoff set before a failed
    verdict would hide it. Trials carried from previous ledgers are outside this check.
    """
    errors: list[str] = []
    seed_tip = prev["covers"]["main_sha"] if prev is not None else None
    for key, definition in definitions.items():
        body = expected["subjects"][key]
        recorded = {row["sha"]: (row["outcome"], row.get("reason")) for row in body["trials"]}
        manifest, _pin = _load_manifest(ev.repo, covers, definition)
        plan = _plan(ev, definition, manifest, covers, now, seed_tip)
        for row in plan.trials:
            if recorded.get(row["sha"]) != (row["outcome"], row["reason"]):
                reason = f" ({row['reason']})" if row["reason"] else ""
                errors.append(
                    f"{key}: ledger is stale, commit {row['sha'][:12]} has a {row['outcome']}{reason} attest verdict "
                    "the ledger does not record"
                )
        for note in plan.pending:
            errors.append(f"{key}: ledger is stale, commit {note.sha[:12]} {note.why}")
    return errors


TEMPLATES_PATH = "src/brigade/templates"
PROFILE_SNAPSHOT_PATH = f"{TEMPLATES_PATH}/{template_profiles.SNAPSHOT_NAME}"
ROLLBACK_DERIVED_PATHS = (PROFILE_SNAPSHOT_PATH,)


def rollback_derived_files(repo: Repo, tree: str) -> dict[str, bytes]:
    """Run the trusted generator with data from a candidate Git commit or tree."""
    fixtures_path = template_profiles.FIXTURES_PATH
    with tempfile.TemporaryDirectory(prefix="ledger-template-data-") as directory:
        root = Path(directory)
        for source in (TEMPLATES_PATH, fixtures_path):
            data_root = root / source
            for name, content in repo.snapshot(tree, source).items():
                path = template_profiles.template_data_path(data_root, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        snapshot = template_profiles.build_snapshot(
            templates_root=root / TEMPLATES_PATH, fixtures_dir=root / fixtures_path
        )
    return {PROFILE_SNAPSHOT_PATH: template_profiles.render_snapshot(snapshot).encode("utf-8")}


def _path_errors(
    repo: Repo, expected: Mapping[str, Any], base: str, change_head: str, head: str, *, any_other_path: bool = False
) -> list[str]:
    """Allow only ledger, restored bundles, and verified rollback outputs."""
    errors: list[str] = []
    restores = {
        key: body["restore"] for key, body in expected["subjects"].items() if isinstance(body.get("restore"), dict)
    }
    bundle_dirs = {skill_dir(expected["subjects"][key]["subject_path"]) + "/": row for key, row in restores.items()}
    for path in repo.changed_paths(base, change_head):
        if any_other_path or path == LEDGER_PATH:
            continue
        if bundle_dirs and path in ROLLBACK_DERIVED_PATHS:
            continue
        if any(path.startswith(prefix) for prefix in bundle_dirs):
            continue
        errors.append(f"a ledger change may not touch {path}")
    for prefix, row in bundle_dirs.items():
        directory = prefix.rstrip("/")
        if repo.snapshot(head, directory) != repo.snapshot(row["to_commit"], directory):
            errors.append(f"{directory}: bundle is not the approved text restored byte for byte")
    if bundle_dirs and not errors:
        for path, data in rollback_derived_files(repo, change_head).items():
            if repo.show(change_head, path) != data:
                errors.append(f"{path}: does not match the regenerated rollback output")
    return errors
