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
  security-reviewed PR;
* scorecards stay receipt-only and never read these rows.

When ``auto`` resolves no observed skill (Codex repos, plain shells) the run
falls back to the previous default, ``brigade-work``, recorded as
``caller_typed``, so the loop never silently records nothing.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Collection, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

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
AGENT_SESSION_ENV = "BRIGADE_AGENT_SESSION"
"""Ambient: exported to every shell of a session, so subagents and workers inherit it."""
MAX_STATE_BYTES = 1_048_576
MAX_STATES_SCANNED = 512
_FINGERPRINT = re.compile(r"[0-9a-f]{16}")


def normalize_attribution_source(value: object) -> str:
    """Read a stored attribution source; anything but the observed marker is caller typed."""
    return SESSION_OBSERVED if value == SESSION_OBSERVED else CALLER_TYPED


def session_fingerprint_from_env(environ: Mapping[str, str]) -> str | None:
    """The session this process may read. The explicit hook value wins, alone.

    ``BRIGADE_AGENT_SESSION`` is ambient: a ``brigade run`` worker inherits the
    parent's value, so it only counts outside a worker (no ``BRIGADE_RUN_ID``).
    """
    explicit = environ.get(CLAUDE_SESSION_ENV, "")
    if explicit:
        return explicit if _FINGERPRINT.fullmatch(explicit) else None
    if environ.get(receipt_schema.BRIGADE_RUN_ID_ENV, ""):
        return None
    ambient = environ.get(AGENT_SESSION_ENV, "")
    return ambient if _FINGERPRINT.fullmatch(ambient) else None


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
    """Session state files, newest first, one parse at a time. Regular files only, size capped, never followed."""
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
                except (UnicodeError, ValueError):
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
    """Match the fingerprint and, when the state names its session id, that id's hash too."""
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
    case-insensitively and come back in the known spelling.
    """
    environ = os.environ if environ is None else environ
    fingerprint = session_fingerprint_from_env(environ)
    if fingerprint is None:
        return []
    known_names = target_known_skill_ids(target) if known_skill_ids is None else set(known_skill_ids)
    known = {name.casefold(): name for name in known_names}
    if not known:
        return []
    for state in _iter_states(target.expanduser().resolve()):
        if not _state_matches(state, fingerprint):
            continue
        named = [*skill_ids_from_read_files(state.get("read_files")), *skill_invocations.invoked_names(state)]
        resolved: dict[str, None] = {}
        for name in named:
            canonical = known.get(name.casefold())
            if canonical is not None:
                resolved.setdefault(canonical, None)
        return list(resolved)
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
    print(
        f"outcome capture: auto found no skill read in this session; capturing against {fallback} (caller typed)",
        file=sys.stderr,
    )
    return [fallback], False


def capture_artifacts(
    *,
    target: Path,
    artifact_ids: list[str],
    artifact_kind: str,
    run_id: str,
    observed: bool,
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
            )
            or rc
        )
    return rc


def apply_observed_veto(
    decision: core.Decision,
    cohorts: core.FingerprintCohorts | None,
    *,
    prior_status: str,
) -> core.Decision:
    """Turn an install or bump into a hold when observed sessions show hurt.

    Applies to skills and cards alike. Never produces install, bump or rollback.
    A receipt-derived rollback or a cooldown hold passes through unchanged.
    """
    if cohorts is None or cohorts.current.observed_hurt <= 0:
        return decision
    if decision.action not in {"install", "bump"}:
        return decision
    return core.Decision(
        decision.artifact_id,
        "hold",
        prior_status,
        "withheld: regression observed in sessions that read this skill (session_observed attribution, veto only)",
        code=OBSERVED_REGRESSION_HOLD,
    )
