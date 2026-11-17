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
  rollback from these rows. Rollback policy is a later, security-reviewed PR;
* scorecards stay receipt-only and never read these rows.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Collection, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from . import dirfd, outcome as core
from .claude_hooks import anchored_io

AUTO_CAPTURE = "auto"
"""``--capture`` value that resolves skill ids from the session state. Never persisted."""
CALLER_TYPED = core.CALLER_TYPED
SESSION_OBSERVED = core.SESSION_OBSERVED
OBSERVED_REGRESSION_HOLD = "observed_regression_hold"
"""Decision code: observed-session hurt withholds an install or bump (veto only)."""

SESSION_ENV_KEYS = ("BRIGADE_CLAUDE_SESSION", "BRIGADE_AGENT_SESSION")
MAX_STATE_BYTES = 1_048_576
MAX_STATES_SCANNED = 512
_FINGERPRINT = re.compile(r"[0-9a-f]{16}")


def normalize_attribution_source(value: object) -> str:
    """Read a stored attribution source; anything but the observed marker is caller typed."""
    return SESSION_OBSERVED if value == SESSION_OBSERVED else CALLER_TYPED


def session_fingerprint_from_env(environ: Mapping[str, str]) -> str | None:
    for key in SESSION_ENV_KEYS:
        value = environ.get(key, "")
        if _FINGERPRINT.fullmatch(value):
            return value
    return None


def skill_ids_from_read_files(read_files: object) -> list[str]:
    """Ordered unique skill ids named by ``.../skills/<id>/SKILL.md`` reads."""
    if not isinstance(read_files, list):
        return []
    seen: dict[str, None] = {}
    for item in read_files:
        if not isinstance(item, str) or not item:
            continue
        parts = PurePosixPath(item.replace("\\", "/")).parts
        for index, part in enumerate(parts):
            if part == "skills" and index + 3 == len(parts) and parts[index + 2] == "SKILL.md":
                skill_id = parts[index + 1]
                if skill_id not in {"", ".", ".."}:
                    seen.setdefault(skill_id, None)
    return list(seen)


def _read_states(target: Path) -> list[dict[str, Any]]:
    """Session state files, newest first. Regular files only, size capped, never followed."""
    states: list[tuple[float, dict[str, Any]]] = []
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
            for mtime, name in candidates[:MAX_STATES_SCANNED]:
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
                    states.append((mtime, value))
    except (OSError, ValueError):
        return []
    return [state for _, state in states]


def target_known_skill_ids(target: Path) -> set[str]:
    """Skill ids the target registry or installed harness skills actually have."""
    from . import outcome_cmd

    return set(outcome_cmd._known_skill_names(target))


def resolve_observed_skill_ids(
    target: Path,
    environ: Mapping[str, str] | None = None,
    known_skill_ids: Collection[str] | None = None,
) -> list[str]:
    """Skill ids the current session read, ordered, deduplicated, known only.

    File-only: reads the hook session state, never calls the hook. Returns ``[]``
    when there is no session identity, no matching state, an unreadable or
    refused state, or no known skill was read. Failure never raises.
    """
    environ = os.environ if environ is None else environ
    fingerprint = session_fingerprint_from_env(environ)
    if fingerprint is None:
        return []
    known = target_known_skill_ids(target) if known_skill_ids is None else set(known_skill_ids)
    if not known:
        return []
    for state in _read_states(target.expanduser().resolve()):
        if state.get("session_fingerprint") != fingerprint:
            continue
        return [skill_id for skill_id in skill_ids_from_read_files(state.get("read_files")) if skill_id in known]
    return []


def resolve_auto_capture(target: Path, environ: Mapping[str, str] | None = None) -> list[str]:
    """Resolve ``--capture auto`` once. No match prints one stderr note and writes nothing."""
    ids = resolve_observed_skill_ids(target, environ)
    if not ids:
        print(
            "outcome capture: auto found no skill read in this session; nothing captured",
            file=sys.stderr,
        )
    return ids


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
    artifact_kind: str,
    prior_status: str,
) -> core.Decision:
    """Turn an install or bump into a hold when observed sessions show hurt.

    Never produces install, bump or rollback. A receipt-derived rollback or a
    cooldown hold passes through unchanged.
    """
    if artifact_kind != "skill" or cohorts is None or cohorts.current.observed_hurt <= 0:
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
