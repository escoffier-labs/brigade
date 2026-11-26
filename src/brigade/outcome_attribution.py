"""Session-observed skill attribution for ``--capture auto`` (#2589 criterion 1).

Ordinary work reads a skill's ``SKILL.md`` before following it. The Claude Code
hook records every read in its per-session state file, so a verify run can name
the skills that guided the work without a hand-typed artifact id.

Trust rule (Solomon, 2026-11-17): this attribution is caller-side and
forgeable, because the agent chooses which files it reads. It may therefore only
VETO, never promote:

* ledger rows written from it carry ``attribution_source = "session_observed"``;
* ``score_records`` counts them toward ``hurt`` only, so they can lower a rank
  but never raise it;
* reconcile may turn an install or bump into a hold
  (``observed_regression_hold``). It never produces an install, a bump or a
  rollback from these rows, for skills or cards. Rollback policy is a later,
  security-reviewed PR. The hold is bounded so it cannot pin a skill forever:
  only unresolved observed hurt inside a recency window, from at least a
  minimum number of distinct sessions, counts, and it releases on its own
  (``observed_hurt_sessions``);
* scorecards stay receipt-only and never read these rows.

Session identity comes from the hook-printed ``BRIGADE_CLAUDE_SESSION``, else the
ambient ``BRIGADE_AGENT_SESSION``, else Claude Code's own ``CLAUDE_CODE_SESSION_ID``
(so a verify typed directly in a hook-enabled session still resolves). Two ambient
values that disagree name no session.

When ``auto`` resolves no observed skill (Codex repos, plain shells) the run
falls back to the previous default, ``brigade-work``, recorded as
``caller_typed``, so the loop never silently records nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import sys
from collections.abc import Collection, Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from . import dirfd, localio, outcome as core, receipt_schema
from .claude_hooks import anchored_io, skill_invocations

AUTO_CAPTURE = "auto"
"""``--capture`` value that resolves skill ids from the session state. Never persisted."""
CALLER_TYPED = core.CALLER_TYPED
SESSION_OBSERVED = core.SESSION_OBSERVED
OBSERVED_REGRESSION_HOLD = core.OBSERVED_REGRESSION_HOLD
"""Decision code: observed-session hurt withholds an install or bump (veto only)."""

CLAUDE_SESSION_ENV = "BRIGADE_CLAUDE_SESSION"
"""Set by the hook-built verify command for exactly this session. Preferred."""
SESSION_TARGET_ENV = "BRIGADE_CLAUDE_SESSION_TARGET"
"""Set when the verifier's checkout differs from the checkout holding the session state."""
AGENT_SESSION_ENV = "BRIGADE_AGENT_SESSION"
"""Ambient: exported to every shell of a session, so subagents and workers inherit it."""
NATIVE_SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
"""Claude Code's own session id for every Bash child. It equals the hook payload ``session_id``."""
MAX_STATE_BYTES = 1_048_576
MAX_STATES_SCANNED = 512
_FINGERPRINT = re.compile(r"[0-9a-f]{16}")


if TYPE_CHECKING:
    from .verify_manifest_git import VerifyGitFacts


def normalize_attribution_source(value: object) -> str:
    """Read a stored attribution source; anything but the observed marker is caller typed."""
    return SESSION_OBSERVED if value == SESSION_OBSERVED else CALLER_TYPED


def session_fingerprint_from_env(environ: Mapping[str, str]) -> str | None:
    """The session this process may read. The explicit hook value wins, alone.

    ``BRIGADE_AGENT_SESSION`` and ``CLAUDE_CODE_SESSION_ID`` are ambient: a
    ``brigade run`` worker inherits the parent's values, so they only count
    outside a worker (no ``BRIGADE_RUN_ID``). A verify typed directly in a Claude
    session has no hook-printed value, so the ambient pair supplies the identity.
    Claude Code's id is hashed the way the hook fingerprints ``session_id``. When
    both ambient values are present and disagree, no single session can be
    named, so there is no identity and the caller falls back.
    """
    explicit = environ.get(CLAUDE_SESSION_ENV, "")
    if explicit:
        return explicit if _FINGERPRINT.fullmatch(explicit) else None
    if environ.get(receipt_schema.BRIGADE_RUN_ID_ENV, ""):
        return None
    ambient = environ.get(AGENT_SESSION_ENV, "")
    ambient = ambient if _FINGERPRINT.fullmatch(ambient) else ""
    native_id = environ.get(NATIVE_SESSION_ENV, "")
    native = localio.stable_hash({"claude_session_id": native_id}) if native_id and len(native_id) <= 4096 else ""
    if ambient and native and ambient != native:
        return None
    return ambient or native or None


def _skill_id_from_read_path(item: str) -> str | None:
    """Skill id for a repo-relative read of an installed harness skill or registry skill.

    Only ``.<harness>/skills/<id>/SKILL.md`` and ``.brigade/skills/registry/<id>/SKILL.md``
    count; ``tests/fixtures/skills/<id>/SKILL.md`` and the like do not.
    """
    parts = PurePosixPath(item.replace("\\", "/")).parts
    folded = [part.casefold() for part in parts]
    if len(parts) == 4 and parts[0].startswith(".") and folded[0] != ".brigade" and folded[1] == "skills":
        skill_id = parts[2]
    elif len(parts) == 5 and folded[:3] == [".brigade", "skills", "registry"]:
        skill_id = parts[3]
    else:
        return None
    if folded[-1] != "skill.md" or skill_id in {"", ".", ".."}:
        return None
    return skill_id


def skill_ids_from_read_files(read_files: object) -> list[str]:
    """Ordered unique skill ids named by reads of installed or registry ``SKILL.md`` files."""
    if not isinstance(read_files, list):
        return []
    seen: dict[str, None] = {}
    for item in read_files:
        skill_id = _skill_id_from_read_path(item) if isinstance(item, str) and item else None
        if skill_id is not None:
            seen.setdefault(skill_id, None)
    return list(seen)


def _iter_states(target: Path) -> Iterator[dict[str, Any]]:
    """Session state files, newest first, one parse at a time.

    Regular files only, never followed. A file over ``MAX_STATE_BYTES`` is skipped, and only the
    ``MAX_STATES_SCANNED`` most recently modified files are read, so an older session's state
    can fall out of view in a target with many sessions.
    """
    try:
        with anchored_io.session_directory(target) as anchor:
            names = [name for name in dirfd.child_names(anchor.fd, sys.maxsize) if name.endswith(".json")]
            candidates: list[tuple[float, str]] = []
            for name in names:
                try:
                    info = dirfd.stat_child(anchor.fd, name, allow_reparse=True)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_size <= MAX_STATE_BYTES:
                    candidates.append((info.st_mtime, name))
            candidates.sort(reverse=True)
            for _mtime, name in candidates[:MAX_STATES_SCANNED]:
                try:
                    raw = anchor.read_bytes(name, MAX_STATE_BYTES + 1)
                except OSError:
                    continue
                if len(raw) > MAX_STATE_BYTES:
                    continue
                try:
                    value = json.loads(raw)
                except (UnicodeError, ValueError, RecursionError):
                    # A file nested deeper than the parser's recursion limit is malformed too.
                    continue
                if isinstance(value, dict):
                    yield value
    except (OSError, ValueError):
        return


def target_known_skill_ids(target: Path) -> set[str]:
    """Skill ids the target registry or installed harness skills actually have."""
    from . import outcome_cmd

    return set(outcome_cmd._known_skill_names(target))


def _state_matches(state: Mapping[str, Any], fingerprint: str) -> bool:
    """Match the fingerprint and, when the state names its session id, that id's hash too.

    A ``session_id`` that is not the string the fingerprint was hashed from (a number, or an empty
    string) never matches. Only a state whose ``session_id`` is absent or null is matched on the
    fingerprint alone.
    """
    if state.get("session_fingerprint") != fingerprint:
        return False
    session_id = state.get("session_id")
    return session_id is None or localio.stable_hash({"claude_session_id": session_id}) == fingerprint


def resolve_observed_skill_ids(
    target: Path,
    environ: Mapping[str, str] | None = None,
    known_skill_ids: Collection[str] | None = None,
) -> list[str]:
    """Skill ids the current session read or invoked, ordered, deduplicated, known only.

    File-only: reads the hook session state, never calls the hook. Returns ``[]``
    when there is no session identity, no matching state, an unreadable or
    refused state, or no known skill was read. Failure never raises. Ids compare
    case-insensitively and come back in the known spelling. The verify target's
    state wins; the hook target named by ``BRIGADE_CLAUDE_SESSION_TARGET`` is the
    fallback, because the hook stores the session where it fired.
    """
    environ = os.environ if environ is None else environ
    fingerprint = session_fingerprint_from_env(environ)
    if fingerprint is None:
        return []
    roots = [target.expanduser().resolve()]
    hook_target = environ.get(SESSION_TARGET_ENV, "")
    if hook_target and os.path.isabs(hook_target) and Path(hook_target).is_dir():
        roots.append(Path(hook_target).resolve())
    roots = list(dict.fromkeys(roots))
    known_names = set(target_known_skill_ids(roots[0]) if known_skill_ids is None else known_skill_ids)
    for extra in roots[1:]:
        known_names |= target_known_skill_ids(extra)
    known = {name.casefold(): name for name in known_names}
    if not known:
        return []
    for root in roots:
        for state in _iter_states(root):
            if not _state_matches(state, fingerprint):
                continue
            named = [*skill_ids_from_read_files(state.get("read_files")), *skill_invocations.invoked_names(state)]
            resolved: dict[str, None] = {}
            for name in named:
                canonical = known.get(name.casefold())
                if canonical is not None:
                    resolved.setdefault(canonical, None)
            if resolved:
                return list(resolved)
            break
    return []


def resolve_auto_capture(target: Path, environ: Mapping[str, str] | None = None) -> tuple[list[str], bool]:
    """Resolve ``--capture auto`` once into ``(skill ids, observed)``.

    No observed skill falls back to the generic ``brigade-work`` id as a caller
    typed capture (the previous default), with one stderr note, so a run in a
    Codex repo or a plain shell still records its outcome.
    """
    from . import outcome_cmd

    ids = resolve_observed_skill_ids(target, environ)
    if ids:
        return ids, True
    fallback = outcome_cmd.DEFAULT_CAPTURE_ARTIFACT_ID
    environ = os.environ if environ is None else environ
    # Only a Claude Code session (not a ``brigade run`` worker, a Codex repo or a plain shell) has a
    # hook-printed verify command to rerun.
    in_claude_session = bool(environ.get("CLAUDECODE")) and not environ.get(receipt_schema.BRIGADE_RUN_ID_ENV)
    hint = (
        "; no single Claude session identity was found, so rerun the verify command the hook printed"
        if in_claude_session and not session_fingerprint_from_env(environ)
        else ""
    )
    print(
        f"outcome capture: auto found no skill read in this session; capturing against {fallback} (caller typed){hint}",
        file=sys.stderr,
    )
    return [fallback], False


def observed_capture_meta(artifact_ids: Sequence[str], environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Receipt stamp for an observed capture: the source, every observed id, and the session.

    ``outcome_capture.artifact_id`` only names the first id, so retry hints and the
    bounded veto read the rest from here instead of treating ``artifact_id`` as typed.
    """
    environ = os.environ if environ is None else environ
    return {
        "attribution_source": SESSION_OBSERVED,
        "observed_artifact_ids": list(artifact_ids),
        "session_fingerprint": session_fingerprint_from_env(environ),
    }


def receipt_is_observed(receipt: Mapping[str, Any] | None) -> bool:
    """Whether a verify receipt's capture stamp came from ``--capture auto`` resolution."""
    capture = receipt.get("outcome_capture") if isinstance(receipt, Mapping) else None
    return isinstance(capture, Mapping) and capture.get("attribution_source") == SESSION_OBSERVED


def capture_artifacts(
    *,
    target: Path,
    artifact_ids: list[str],
    artifact_kind: str,
    run_id: str,
    observed: bool,
    git_facts: VerifyGitFacts | None = None,
) -> int:
    """Write one ledger row per artifact id; ``observed`` marks them ``session_observed``."""
    from . import outcome_cmd

    source = SESSION_OBSERVED if observed else CALLER_TYPED
    rc = 0
    for artifact_id in artifact_ids:
        rc = (
            outcome_cmd.capture(
                target=target,
                artifact_id=artifact_id,
                artifact_kind=artifact_kind,
                run_id=run_id,
                json_output=False,
                attribution_source=source,
                git_facts=git_facts,
            )
            or rc
        )
    return rc


def capture_auto(*, target: Path, artifact_kind: str, run_id: str | None, run_receipt: str | None) -> int:
    """``brigade outcome capture auto``: resolve the session's skills and capture a past verify run."""
    if artifact_kind != "skill" or run_receipt is not None:
        print(
            "error: capture auto resolves skills for a verify run; drop --kind card and --run-receipt", file=sys.stderr
        )
        return 1
    target = target.expanduser().resolve()
    ids, observed = resolve_auto_capture(target)
    return capture_artifacts(
        target=target, artifact_ids=ids, artifact_kind=artifact_kind, run_id=run_id or "latest", observed=observed
    )


def _receipt_session(receipt: Mapping[str, Any] | None) -> str | None:
    """Session fingerprint a verify receipt was produced under, when it records one."""
    if not isinstance(receipt, Mapping):
        return None
    capture = receipt.get("outcome_capture")
    session = capture.get("session_fingerprint") if isinstance(capture, Mapping) else None
    if not session:
        harness = receipt.get("harness_session")
        session = harness.get("fingerprint") if isinstance(harness, Mapping) else None
    return session if isinstance(session, str) and session else None


def _receipt_started(receipt: Mapping[str, Any]) -> dt.datetime | None:
    return localio.parse_iso_datetime(receipt.get("started_at") or receipt.get("completed_at"))


def _receipt_identity(receipt: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """What the receipt verified: its manifest id, else its normalized planned commands."""
    from .work_cmd import verification

    manifest = receipt.get("verify_manifest_id")
    if isinstance(manifest, str) and manifest:
        return ("manifest", manifest)
    planned = verification._receipt_planned_commands_identity(dict(receipt))
    return ("commands", *planned) if planned else None


class _FixedLaterIndex:
    """Passing verify receipts by (session, verified thing), loaded once and only when needed."""

    def __init__(self, target: Path) -> None:
        self._target = target
        self._passing: dict[tuple[str, tuple[Any, ...]], list[dt.datetime]] | None = None

    def _load(self) -> dict[tuple[str, tuple[Any, ...]], list[dt.datetime]]:
        from .work_cmd import verification

        passing: dict[tuple[str, tuple[Any, ...]], list[dt.datetime]] = {}
        for receipt in verification._verify_receipts(self._target):
            session = _receipt_session(receipt)
            identity = _receipt_identity(receipt)
            started = _receipt_started(receipt)
            if session and identity and started and core.signal_value("verify", str(receipt.get("status"))) > 0:
                passing.setdefault((session, identity), []).append(started)
        return passing

    def session_of(self, record: core.OutcomeRecord) -> str:
        """The failing receipt's session, or a per-receipt key when it cannot be proven."""
        return _receipt_session(self.receipt_for(record)) or f"receipt:{record.evidence_ref or id(record)}"

    def receipt_for(self, record: core.OutcomeRecord) -> dict[str, Any] | None:
        from .work_cmd import helpers, verification

        if not record.evidence_ref:
            return None
        path = Path(record.evidence_ref)
        try:
            path.resolve().relative_to(helpers._verify_runs_root(self._target).resolve())
        except (OSError, ValueError):
            return None
        return verification._verify_read_receipt(path)

    def fixed_later(self, record: core.OutcomeRecord) -> bool:
        """A failure later passed again by the same session on the same verifier is iteration, not regression."""
        receipt = self.receipt_for(record)
        session = _receipt_session(receipt)
        identity = _receipt_identity(receipt) if receipt is not None else None
        started = _receipt_started(receipt) if receipt is not None else None
        if not (session and identity and started):
            return False
        if self._passing is None:
            self._passing = self._load()
        return any(stamp > started for stamp in self._passing.get((session, identity), ()))


def observed_hurt_sessions(
    target: Path,
    records: Sequence[core.OutcomeRecord],
    cohorts_by_artifact: Mapping[str, core.FingerprintCohorts],
    *,
    now: dt.datetime,
    config: core.ReconcileConfig,
) -> dict[str, int]:
    """Per artifact, distinct sessions whose observed hurt still counts toward the veto.

    A session-observed hurt row counts only when it is content-current, inside
    ``observed_hurt_window_days``, and not fixed later (a passing receipt for the
    same verifier in the same session after the failure). Counted per distinct
    session, so one noisy session never reaches the threshold alone.
    """
    window = config.observed_hurt_window_days * 86_400.0
    by_artifact: dict[str, list[core.OutcomeRecord]] = {}
    for record in records:
        by_artifact.setdefault(record.artifact_id, []).append(record)
    index = _FixedLaterIndex(target)
    counts: dict[str, int] = {}
    for artifact_id, rows in by_artifact.items():
        if not any(r.attribution_source == SESSION_OBSERVED and r.signal_value < 0 for r in rows):
            continue
        cohort = cohorts_by_artifact.get(artifact_id)
        # Deduped first, so an observed row twinned by a caller-typed row of the same receipt is not counted.
        current = core.current_content_records(rows, cohort.current_fingerprint if cohort else None)
        sessions = {
            index.session_of(r)
            for r in current
            if r.attribution_source == SESSION_OBSERVED
            and r.signal_value < 0
            and core._record_age_seconds(now, r.ts) <= window
            and not index.fixed_later(r)
        }
        if sessions:
            counts[artifact_id] = len(sessions)
    return counts


def apply_observed_veto(
    decision: core.Decision,
    sessions: int,
    *,
    prior_status: str,
    config: core.ReconcileConfig,
) -> core.Decision:
    """Turn an install or bump into a hold while bounded observed hurt stands.

    ``sessions`` is ``observed_hurt_sessions`` for the artifact. Applies to skills
    and cards alike. Never produces install, bump or rollback, and releases on its
    own once the count falls below ``observed_hurt_min_sessions`` (hurt ages out of
    the window or its session fixed it). A receipt-derived rollback or a cooldown
    hold passes through unchanged.
    """
    if sessions < max(1, config.observed_hurt_min_sessions) or decision.action not in {"install", "bump"}:
        return decision
    return core.Decision(
        decision.artifact_id,
        "hold",
        prior_status,
        "withheld: unresolved regression observed in sessions that read this skill "
        "(session_observed attribution, veto only)",
        code=OBSERVED_REGRESSION_HOLD,
    )


def veto_projection(
    decisions: Mapping[str, core.Decision],
    sessions: Mapping[str, int],
    *,
    config: core.ReconcileConfig,
) -> dict[str, core.Decision]:
    """Apply the observed veto to a dry-run projection, as reconcile does (bump starts promoted)."""
    return {
        artifact_id: apply_observed_veto(
            decision,
            sessions.get(artifact_id, 0),
            prior_status="promoted" if decision.action == "bump" else "candidate",
            config=config,
        )
        for artifact_id, decision in decisions.items()
    }
