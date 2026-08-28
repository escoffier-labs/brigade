"""Outcome ledger persistence and read-side CLI (score, explain).

The ledger lives under ``memory/outcome/`` so it is git-tracked and portable
(readable without Brigade, movable across harnesses), unlike the gitignored
``.brigade/`` correlation buffers. Scores are derived from the records on every
read, so the audit trail and the score can never drift apart.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import platform as platform_mod
import secrets
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

from . import causal_receipt, localio, outcome as core, receipt_schema, scorecard as scorecard_mod, verify_trial
from .outcome_discovery import (
    _artifact_content_path_at,
    _artifact_id_has_controls,
    _contained_under,
    _linked_worktree_skill_roots,
    _resolve_root,
    _safe_path_component,
    _skill_bundle_fingerprint,
    _skill_names_at,
)

_LOCK_WAIT_SECONDS = 30.0
_LOCK_SENTINEL_BYTE = b"\0"
_WARNING_LABEL_LIMIT = 80

# Generic fallback when no exercised skill/card is known. Prefer a real artifact
# id at capture time so distinct skills do not collapse into one rank bucket.
DEFAULT_CAPTURE_ARTIFACT_ID = "brigade-work"


class OutcomeLedgerError(RuntimeError):
    """Outcome ledger persistence failed; callers must not assume the append succeeded."""


def resolve_capture_artifact_id(*candidates: str | None) -> str:
    """Return the first non-empty capture id, else the generic brigade-work fallback."""
    for candidate in candidates:
        if isinstance(candidate, str):
            trimmed = candidate.strip()
            if trimmed:
                return trimmed
    return DEFAULT_CAPTURE_ARTIFACT_ID


def _records_lock_path(target: Path) -> Path:
    return _records_path(target).parent / ".records.lock"


@dataclasses.dataclass(frozen=True)
class _ReconcileItem:
    decision: core.Decision
    prior_status: str
    cohorts: core.FingerprintCohorts | None = None
    scorecard: scorecard_mod.SubjectScorecard | None = None


def _records_path(target: Path) -> Path:
    return target / "memory" / "outcome" / "records.jsonl"


def _status_path(target: Path) -> Path:
    return target / "memory" / "outcome" / "status.json"


def _decision_path(target: Path, now, artifact_id: str) -> Path:
    """Return a collision-resistant path for a new decision receipt."""
    stamp = now.strftime("%Y%m%d-%H%M%S-%f")
    slug = localio.slugify(artifact_id, fallback="artifact")
    token = secrets.token_hex(4)
    return target / "memory" / "outcome" / "decisions" / f"{stamp}-{slug}-{token}.json"


def _write_decision_receipt(
    target: Path,
    now,
    artifact_id: str,
    receipt: dict[str, Any],
) -> Path:
    """Write a decision receipt exclusively, retrying identity collisions."""
    last_error: FileExistsError | None = None
    for _ in range(8):
        path = _decision_path(target, now, artifact_id)
        try:
            localio.write_json_exclusive(path, receipt)
            return path
        except FileExistsError as exc:
            last_error = exc
    raise FileExistsError(f"could not allocate a unique decision receipt path for {artifact_id}: {last_error}")


def load_status(target: Path) -> dict[str, dict]:
    payload = localio.read_json_dict(_status_path(target)) or {}
    artifacts = payload.get("artifacts")
    return artifacts if isinstance(artifacts, dict) else {}


def _safe_card_id_component(card_id: str) -> str | None:
    """Return card_id when it is a single safe path component (Unicode/spaces ok)."""
    return _safe_path_component(card_id)


def _contained_card_file(target: Path, candidate: Path) -> Path | None:
    """Return candidate when it resolves under the selected cards root inside target.

    Blocks both an escaping ``memory`` / ``memory/cards`` directory symlink
    (cards resolve outside the target) and a per-file symlink that lands
    outside ``memory/cards`` even when still inside the target tree.
    """
    target_resolved = _resolve_root(target)
    if target_resolved is None:
        return None
    cards_resolved = _resolve_root(target / "memory" / "cards")
    if cards_resolved is None or not cards_resolved.is_relative_to(target_resolved):
        return None
    return _contained_under(cards_resolved, candidate)


def _known_skill_names(target: Path) -> list[str]:
    """Skill ids the target actually has: wired into a harness or in the registry.

    Linked git worktrees inherit the parent clone's harness/registry skills after
    the target-local set, matching roster resolution. Global home skill dirs are
    intentionally out of scope so capture attribution stays repo-bounded.
    """
    names: set[str] = set()
    for root in _linked_worktree_skill_roots(target):
        names |= _skill_names_at(root)
    return sorted(names)


def _known_card_names(target: Path) -> list[str]:
    cards = target / "memory" / "cards"
    if not cards.is_dir():
        return []
    names: list[str] = []
    for path in cards.glob("*.md"):
        card_id = _safe_card_id_component(path.stem)
        if card_id is None:
            continue
        if _contained_card_file(target, path) is None:
            continue
        names.append(card_id)
    return sorted(names)


def _artifact_known(target: Path, artifact_id: str, kind: str) -> bool:
    if kind == "card":
        return artifact_id in _known_card_names(target)
    return artifact_id in _known_skill_names(target)


def _artifact_content_path(target: Path, artifact_id: str, kind: str) -> Path | None:
    """Locate the artifact text a fingerprint should pin.

    Harness-installed copy first: a verified run exercises the installed skill,
    not the registry master, so when the two drift the installed text is what
    the signal is evidence about. The registry copy is the fallback for skills
    known but not yet installed. On a linked worktree, target-local copies win
    over the parent clone. capture and rank/explain both resolve through here,
    so "current cohort" always means the text that actually runs.
    """
    if kind == "card":
        card_id = _safe_card_id_component(artifact_id)
        if card_id is None:
            return None
        candidate = target / "memory" / "cards" / f"{card_id}.md"
        return _contained_card_file(target, candidate)
    for root in _linked_worktree_skill_roots(target):
        path = _artifact_content_path_at(root, artifact_id)
        if path is not None:
            return path
    return None


def _strip_warning_controls(value: str) -> str:
    """Remove terminal, C0/C1, bidi, and other format controls from warning text."""
    pieces: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf"}:
            if character in "\t\n\r":
                pieces.append(" ")
            continue
        pieces.append(character)
    return "".join(pieces)


def _warning_label(value: str) -> str:
    """Render a bounded, control-stripped label for stderr warnings."""
    clean = " ".join(_strip_warning_controls(value).split())
    if len(clean) <= _WARNING_LABEL_LIMIT:
        return clean
    return clean[: _WARNING_LABEL_LIMIT - 3] + "..."


def _normalize_path_separators(value: str) -> str:
    return value.replace("\\", "/")


def _path_shaped_artifact_id(artifact_id: str) -> bool:
    normalized = _normalize_path_separators(artifact_id)
    return (
        normalized.startswith(("~", "/"))
        or "/" in normalized
        or normalized.endswith("SKILL.md")
        or (len(normalized) >= 2 and normalized[1] == ":")
    )


def _skill_id_from_path_shaped(artifact_id: str) -> str | None:
    normalized = _normalize_path_separators(artifact_id)
    try:
        home = str(Path.home().expanduser().resolve())
    except OSError:
        home = ""
    rendered = normalized
    if home:
        rendered = rendered.replace(_normalize_path_separators(home), "~")
        rendered = rendered.replace(home, "~")
    path = Path(rendered)
    name = path.name
    if name == "SKILL.md":
        name = path.parent.name
    if not name or name in {".", ".."}:
        return None
    return name


def _safe_artifact_id_for_warning(artifact_id: str) -> str:
    """Keep untrusted path-shaped capture ids out of warning text."""
    if not artifact_id or artifact_id in {".", ".."}:
        return _warning_label(artifact_id)
    if not _path_shaped_artifact_id(artifact_id):
        return _warning_label(artifact_id)
    name = _skill_id_from_path_shaped(artifact_id)
    if name is None:
        return "[redacted-path]"
    return _warning_label(name)


def _unknown_artifact_remedy(artifact_id: str, artifact_kind: str) -> str | None:
    """Exact repair command when Brigade can name one without guessing paths."""
    if artifact_kind != "skill":
        return None
    # Path-shaped ids are caller mistakes; point at the directory name only.
    if _path_shaped_artifact_id(artifact_id):
        skill_id = _skill_id_from_path_shaped(artifact_id)
        if skill_id is None:
            return None
        safe_id = _warning_label(skill_id)
        return f"Capture against the skill id `{safe_id}`, not a filesystem path."
    from . import skills_cmd

    if skills_cmd._bundled_skill_exists(artifact_id):
        safe_id = _warning_label(artifact_id)
        return f"Install it with: brigade skills install {safe_id}"
    return None


def _unknown_artifact_warning(target: Path, artifact_id: str, artifact_kind: str) -> str:
    known = _known_skill_names(target) if artifact_kind == "skill" else _known_card_names(target)
    hint = ", ".join(_warning_label(name) for name in known) if known else "none"
    display_id = _safe_artifact_id_for_warning(artifact_id)
    message = (
        f"warning: '{display_id}' is not a known installed {artifact_kind}; recording anyway. "
        f"Capture against a real {artifact_kind} id to keep ranking trustworthy. "
        f"known {artifact_kind}s: {hint}"
    )
    remedy = _unknown_artifact_remedy(artifact_id, artifact_kind)
    if remedy:
        return f"{message} {remedy}"
    return message


def _link_target_slug(raw: str) -> str:
    """Reduce a raw ``[[wiki-link]]`` body to the card stem it points at.

    Strips an Obsidian-style ``|alias`` and ``#section`` and a trailing ``.md``.
    (``extract_wiki_links`` has already stripped any ``cards/`` prefix.)
    """
    return raw.split("|", 1)[0].split("#", 1)[0].strip().removesuffix(".md")


def _linked_card_closure(cards_dir: Path, root_id: str) -> list[str]:
    """Existing cards reachable from ``root_id`` through ``[[links]]``, transitively.

    CocoIndex's logic_tracking walks nested calls; a card's ``[[links]]`` are its
    "calls", so a card depends on the cards it links, and on the cards those link,
    and so on. Returns the sorted set of reachable card stems, excluding the root
    itself. Cycle-safe (a shared visited set), deterministic (sorted output), and
    tolerant of dead links (a ``[[missing]]`` contributes nothing until the card
    exists). Case-insensitive resolution to the actual on-disk stem.

    Only cards whose resolved path stays under the selected cards directory are
    discoverable, so a symlink escaping that root is treated as a dead link.
    """
    from brigade.memory_doctor.parsing import extract_wiki_links

    cards_root = _resolve_root(cards_dir)
    if cards_root is None:
        return []
    stem_by_lower = {
        p.stem.lower(): p.stem for p in cards_dir.glob("*.md") if _contained_under(cards_root, p) is not None
    }
    visited = {root_id.lower()}
    queue = [root_id]
    reached: set[str] = set()
    while queue:
        current = queue.pop()
        card_path = cards_dir / f"{current}.md"
        if _contained_under(cards_root, card_path) is None:
            continue
        for raw in extract_wiki_links(card_path.read_text(encoding="utf-8", errors="replace")):
            slug = _link_target_slug(raw).lower()
            if not slug or slug in visited:
                continue
            visited.add(slug)
            actual = stem_by_lower.get(slug)
            if actual is not None:
                reached.add(actual)
                queue.append(actual)
    return sorted(reached)


def _card_fingerprint(card_path: Path) -> str | None:
    """Fingerprint a card over itself plus the transitive closure of its links.

    A card with no resolvable ``[[links]]`` hashes to *exactly*
    ``sha256(card content)`` - byte-identical to the pre-link scheme - so existing
    single-card records are never invalidated. A card that links others folds each
    linked card's content hash into the fingerprint, so editing a linked card
    invalidates the referrer the way editing the card itself does.
    """
    cards_dir = card_path.parent
    cards_root = _resolve_root(cards_dir)
    if cards_root is None or _contained_under(cards_root, card_path) is None:
        return None
    try:
        self_bytes = card_path.read_bytes()
        closure = _linked_card_closure(cards_dir, card_path.stem)
        if not closure:
            return hashlib.sha256(self_bytes).hexdigest()
        digest = hashlib.sha256()
        digest.update(hashlib.sha256(self_bytes).hexdigest().encode("ascii"))
        digest.update(b"\0")
        for stem in closure:
            linked = cards_dir / f"{stem}.md"
            if _contained_under(cards_root, linked) is None:
                continue
            digest.update(stem.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(linked.read_bytes()).hexdigest().encode("ascii"))
            digest.update(b"\0")
        return digest.hexdigest()
    except OSError:
        return None


def artifact_fingerprint(target: Path, artifact_id: str, kind: str) -> str | None:
    """sha256 of the artifact's current content, or None when it cannot be resolved.

    For a skill the fingerprint covers the whole installed (or registry) bundle,
    not just SKILL.md, so editing a bundled helper invalidates the skill's signals
    the same way editing SKILL.md does. For a card it covers the card plus the
    transitive closure of the cards it ``[[links]]``, so editing a linked card
    invalidates the referrer too. The fingerprint still sees only card/skill files,
    not the runtime harness around them, the caveat CocoIndex documents for
    undecorated helpers.
    """
    path = _artifact_content_path(target, artifact_id, kind)
    if path is None:
        return None
    if kind == "card":
        return _card_fingerprint(path)
    return _skill_bundle_fingerprint(target, path.parent)


# --- Runtime context manifest (Phase 1: capture and surface, no scoring) -------
# See docs/design/context-blind-spot.md. A content hash sees an artifact's files,
# not the harness it ran inside. We stamp a coarse, honestly-sourced manifest onto
# each new record so cohort-aware scoring (Phase 2) has data to work with. Nothing
# here changes a score or a ratchet decision.

# Ordered (env var -> harness name) auto-detection. An explicit
# BRIGADE_CONTEXT_HARNESS override wins; otherwise a known signal implies a name.
_HARNESS_SIGNALS: tuple[tuple[str, str], ...] = (
    ("CLAUDECODE", "claude-code"),
    ("CURSOR_TRACE_ID", "cursor"),
    ("CODEX_SANDBOX", "codex"),
    ("CODEX_SANDBOX_NETWORK_DISABLED", "codex"),
)

# Coarse model-family buckets, matched as substrings in priority order. Low
# cardinality on purpose: the family is what plausibly flips an exit code, and
# coarse buckets are what let a capability cohort ever accumulate samples.
_MODEL_FAMILIES: tuple[tuple[str, str], ...] = (
    ("opus", "claude"),
    ("sonnet", "claude"),
    ("haiku", "claude"),
    ("fable", "claude"),
    ("claude", "claude"),
    ("codex", "openai"),
    ("gpt", "openai"),
    ("grok", "xai"),
    ("gemini", "google"),
)


def _detect_harness() -> tuple[str, str]:
    """Return (harness, source). Best-effort: env override, then known signals."""
    override = os.environ.get("BRIGADE_CONTEXT_HARNESS", "").strip()
    if override:
        return localio.slugify(override, fallback="harness"), "env"
    for var, name in _HARNESS_SIGNALS:
        if os.environ.get(var):
            return name, "auto"
    generic = os.environ.get("AI_AGENT", "").strip()
    if generic:
        return localio.slugify(generic, fallback="harness"), "auto"
    return "unknown", "unknown"


def _detect_model(run_agent: dict[str, Any] | None) -> tuple[str, str]:
    """Return (model, source). The run receipt's agent is the strongest signal."""
    if isinstance(run_agent, dict):
        model = run_agent.get("model")
        if isinstance(model, str) and model:
            return model, "run-receipt"
    override = os.environ.get("BRIGADE_CONTEXT_MODEL", "").strip()
    if override:
        return override, "env"
    return "unknown", "unknown"


def _model_family(model: str) -> str:
    lowered = model.lower()
    if lowered in ("", "unknown"):
        return "unknown"
    for needle, family in _MODEL_FAMILIES:
        if needle in lowered:
            return family
    return "other"


def context_manifest(run_agent: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the coarse runtime-context manifest stamped onto a new record.

    Brigade-computed fields (version, interpreter, platform) carry the same
    trust as an exit code. The harness and model are best-effort and tagged with
    a ``*_source`` so a reader can tell an attested value from a declared one.
    """
    from . import __version__

    harness, harness_source = _detect_harness()
    model, model_source = _detect_model(run_agent)
    return {
        "brigade_version": __version__,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "platform": platform_mod.system() or "unknown",
        "harness": harness,
        "harness_source": harness_source,
        "model": model,
        "model_family": _model_family(model),
        "model_source": model_source,
    }


def capability_fingerprint(manifest: dict[str, Any]) -> str:
    """sha256 of the low-cardinality capability vector the manifest reduces to.

    Coarse on purpose (family not slug, major.minor not patch) so a capability
    cohort can accumulate samples where an exact-context cohort would be n=1.
    """
    vector = {
        "harness": manifest.get("harness", "unknown"),
        "model_family": manifest.get("model_family", "unknown"),
        "python": manifest.get("python", "unknown"),
        "platform": manifest.get("platform", "unknown"),
    }
    return localio.canonical_json_digest(vector)


def _route_coverage(run_json_path: Path | None) -> str | None:
    """ "clean" / "gap" / None, read best-effort from the run's plan-attempts.json
    sibling. A gap is a final attempt with uncovered stages or hallucinated
    covers; None when the file is absent or unreadable (never a hard failure)."""
    if run_json_path is None:
        return None
    attempts_path = run_json_path.parent / "plan-attempts.json"
    if not attempts_path.is_file():
        return None
    try:
        payload = json.loads(attempts_path.read_text())
        attempts = payload.get("attempts") if isinstance(payload, dict) else None
        if not isinstance(attempts, list) or not attempts:
            return None
        final = attempts[-1]
    except (OSError, json.JSONDecodeError, AttributeError, IndexError):
        return None
    if not isinstance(final, dict):
        return None
    if final.get("coverage_missing") or final.get("unknown_covers"):
        return "gap"
    return "clean"


def route_manifest(run_payload: dict[str, Any] | None, run_json_path: Path | None = None) -> dict[str, Any]:
    """Coarse manifest of the route the owning brigade run followed. A run with
    no attached route, or a bare verify-capture with no owning run, is the honest
    unrouted cohort: ``{"followed": False}``. Signals are sorted so the manifest
    (and its fingerprint) do not depend on derivation order."""
    from . import router

    route = run_payload.get("route") if isinstance(run_payload, dict) else None
    if not isinstance(route, dict) or not route.get("attached"):
        return {"followed": False}
    raw = route.get("signals")
    signals = [str(s) for s in raw] if isinstance(raw, list) else []
    # The path is the code/docs/system signal by identity, not by position, so a
    # reordered signals list yields the same manifest and fingerprint.
    path = next((s for s in signals if s in router.PATHS), "unknown")
    manifest: dict[str, Any] = {
        "followed": True,
        "path": path,
        "size": route.get("size", "unknown"),
        "signals": sorted(signals),
    }
    coverage = _route_coverage(run_json_path)
    if coverage is not None:
        manifest["coverage"] = coverage
    return manifest


def route_fingerprint(manifest: dict[str, Any]) -> str | None:
    """sha256 of the low-cardinality route vector (path + size + sorted signals),
    or None for an unrouted record. Coverage is in the manifest but not the
    fingerprint: it would fragment cohorts the way exact context does."""
    if not manifest.get("followed"):
        return None
    vector = {
        "path": manifest.get("path", "unknown"),
        "size": manifest.get("size", "unknown"),
        "signals": manifest.get("signals", []),
    }
    return localio.canonical_json_digest(vector)


def _scorecards_by_artifact(target: Path) -> dict[str, scorecard_mod.SubjectScorecard]:
    return {card.subject.artifact_id: card for card in scorecard_mod.build_scorecards(target)}


def _artifact_kinds(
    records: list[core.OutcomeRecord],
    status_map: dict[str, dict],
    scorecards: dict[str, scorecard_mod.SubjectScorecard],
) -> dict[str, str]:
    kinds: dict[str, str] = {}
    for record in records:
        kinds.setdefault(record.artifact_id, record.artifact_kind or "skill")
    for artifact_id in status_map:
        kinds.setdefault(artifact_id, "skill")
    for artifact_id, card in scorecards.items():
        kinds.setdefault(artifact_id, card.subject.artifact_kind)
    return kinds


def _reconcile_artifact_ids(
    kinds: dict[str, str],
    status_map: dict[str, dict],
    scorecards: dict[str, scorecard_mod.SubjectScorecard],
    cohorts_by_artifact: dict[str, core.FingerprintCohorts],
) -> list[str]:
    artifact_ids: set[str] = set()
    for artifact_id, kind in kinds.items():
        if kind == "card":
            if artifact_id in cohorts_by_artifact:
                artifact_ids.add(artifact_id)
            continue
        if artifact_id in scorecards or artifact_id in status_map or artifact_id in cohorts_by_artifact:
            artifact_ids.add(artifact_id)
    return sorted(artifact_ids)


def _decide_reconcile_item(
    artifact_id: str,
    artifact_kind: str,
    *,
    cohorts: core.FingerprintCohorts | None,
    card: scorecard_mod.SubjectScorecard | None,
    prior_status: str,
    last_action_ts: Any,
    now: Any,
    config: core.ReconcileConfig,
) -> core.Decision:
    if artifact_kind == "card":
        if cohorts is None:
            return core.Decision(artifact_id, "hold", prior_status, "no scored evidence")
        return core.decide(
            cohorts.current,
            current_status=prior_status,
            last_action_ts=last_action_ts,
            now=now,
            config=config,
        )
    return scorecard_mod.decide_scorecard(
        card,
        artifact_id=artifact_id,
        current_status=prior_status,
        last_action_ts=last_action_ts,
        now=now,
        config=config,
    )


def _surface_hold_decision(decision: core.Decision, prior_status: str) -> bool:
    """Whether a hold should appear in reconcile/fork output (fail-closed, auditable)."""
    if decision.action != "hold":
        return False
    if prior_status != "candidate":
        return False
    return decision.reason != "cooldown active"


def _scorecard_decision_fields(card: scorecard_mod.SubjectScorecard | None) -> dict[str, Any]:
    if card is None:
        return {}
    return {
        "policy_version": scorecard_mod.SCORECARD_POLICY_VERSION,
        "scorecard": scorecard_mod.subject_scorecard_to_dict(card),
    }


def _status_entry_for_transition(
    *,
    new_status: str,
    now: Any,
    decision: core.Decision,
    install_failed: bool,
    artifact_kind: str,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"status": new_status, "last_action_ts": now.isoformat()}
    if (
        artifact_kind == "skill"
        and new_status == "promoted"
        and not install_failed
        and decision.action in {"install", "bump"}
    ):
        entry["route_policy"] = scorecard_mod.route_policy_marker_for_promotion()
    return entry


def _fingerprint_cohorts_by_artifact(
    target: Path, records: list[core.OutcomeRecord]
) -> dict[str, core.FingerprintCohorts]:
    kinds: dict[str, str] = {}
    for record in records:
        kinds.setdefault(record.artifact_id, record.artifact_kind or "skill")
    return {
        artifact_id: core.split_by_fingerprint(
            artifact_id, recs, artifact_fingerprint(target, artifact_id, kinds.get(artifact_id, "skill"))
        )
        for artifact_id, recs in _records_by_artifact(records).items()
    }


def _record_from_dict(payload: dict) -> core.OutcomeRecord | None:
    code_graph_delta = payload.get("code_graph_delta")
    context_eval = payload.get("context_eval")
    content_fingerprint = payload.get("content_fingerprint")
    context = payload.get("context")
    capability_fingerprint_value = payload.get("capability_fingerprint")
    route = payload.get("route")
    route_fingerprint_value = payload.get("route_fingerprint")
    reused_evidence_ref = payload.get("reused_evidence_ref")
    try:
        return core.OutcomeRecord(
            artifact_id=str(payload["artifact_id"]),
            artifact_kind=str(payload.get("artifact_kind", "")),
            task_id=str(payload.get("task_id", "")),
            source=str(payload.get("source", "")),
            signal_value=int(payload.get("signal_value", 0)),
            evidence_ref=str(payload.get("evidence_ref", "")),
            ts=str(payload.get("ts", "")),
            code_graph_delta=code_graph_delta if isinstance(code_graph_delta, dict) else None,
            context_eval=context_eval if isinstance(context_eval, dict) else None,
            content_fingerprint=content_fingerprint
            if isinstance(content_fingerprint, str) and content_fingerprint
            else None,
            context=context if isinstance(context, dict) else None,
            capability_fingerprint=capability_fingerprint_value
            if isinstance(capability_fingerprint_value, str) and capability_fingerprint_value
            else None,
            route=route if isinstance(route, dict) else None,
            route_fingerprint=route_fingerprint_value
            if isinstance(route_fingerprint_value, str) and route_fingerprint_value
            else None,
            reused_evidence_ref=reused_evidence_ref
            if isinstance(reused_evidence_ref, str) and reused_evidence_ref
            else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_records(target: Path) -> list[core.OutcomeRecord]:
    rows = localio.read_jsonl_dicts(_records_path(target))
    records = [_record_from_dict(row) for row in rows]
    return [record for record in records if record is not None]


CANONICAL_EVIDENCE_SCHEMA_VERSION = 1


def _canonical_evidence_path(target: Path) -> Path:
    """Persisted ``evidence_ref -> canonical evidence_ref`` map for legacy ledger rows.

    Rows written before #634 stored the reused receipt path. Canonicalizing them
    used to re-open the receipt on every rank/score/reconcile and silently
    stopped collapsing once the receipt was pruned (#650). The map records each
    resolution once; later reads consult it before touching the receipts dir.
    """
    return _records_path(target).parent / "evidence-canonical.json"


def _load_canonical_evidence_map(target: Path) -> dict[str, str]:
    payload = localio.read_json_dict(_canonical_evidence_path(target))
    if not isinstance(payload, dict):
        return {}
    refs = payload.get("refs")
    if not isinstance(refs, dict):
        return {}
    return {str(key): str(value) for key, value in refs.items() if isinstance(key, str) and isinstance(value, str)}


def _save_canonical_evidence_map(target: Path, refs: dict[str, str]) -> None:
    payload = {"schema_version": CANONICAL_EVIDENCE_SCHEMA_VERSION, "refs": dict(sorted(refs.items()))}
    try:
        localio.write_json(_canonical_evidence_path(target), payload)
    except OSError:
        # Best-effort durability: a read-only target still scores correctly
        # this run; the next writable read persists the resolutions.
        return


def _resolve_canonical_verify_evidence_ref(target: Path, evidence_ref: str) -> str | None:
    """Follow ``reused_from`` on disk; ``None`` when the receipt cannot be read."""
    path = Path(evidence_ref).expanduser()
    payload = localio.read_json_dict(path)
    if not isinstance(payload, dict):
        return None
    from .work_cmd import verification as verify_mod

    return verify_mod.canonical_verify_evidence_ref(target, payload)


def _canonical_verify_evidence_ref(
    target: Path,
    evidence_ref: str,
    *,
    cache: dict[str, str] | None = None,
    resolved: dict[str, str] | None = None,
) -> str:
    """Follow ``reused_from`` when ``evidence_ref`` names a verify receipt path.

    ``cache`` is the persisted map consulted first; ``resolved`` collects new
    on-disk resolutions (identity mappings included, so an unchanged original
    receipt is never re-opened either) for the caller to persist.
    """
    if not evidence_ref:
        return evidence_ref
    if cache is not None and evidence_ref in cache:
        return cache[evidence_ref]
    if Path(evidence_ref).expanduser().name != "receipt.json":
        return evidence_ref
    canonical = _resolve_canonical_verify_evidence_ref(target, evidence_ref)
    if canonical is None:
        return evidence_ref
    if cache is not None:
        cache[evidence_ref] = canonical
    if resolved is not None:
        resolved[evidence_ref] = canonical
    return canonical


def _with_canonical_verify_evidence(
    target: Path,
    record: core.OutcomeRecord,
    *,
    cache: dict[str, str] | None = None,
    resolved: dict[str, str] | None = None,
) -> core.OutcomeRecord:
    canonical = _canonical_verify_evidence_ref(target, record.evidence_ref, cache=cache, resolved=resolved)
    if canonical == record.evidence_ref:
        return record
    return dataclasses.replace(record, evidence_ref=canonical)


def load_scoring_records(target: Path) -> list[core.OutcomeRecord]:
    """Load ledger rows with verify evidence canonicalized through ``reused_from``.

    Append-only disk rows stay untouched; scoring/rank/explain/reconcile see one
    signal for an original receipt and any reused twin. Resolutions are read
    from, and persisted to, ``memory/outcome/evidence-canonical.json`` so each
    distinct receipt is opened at most once across the ledger's lifetime and a
    legacy reused row keeps collapsing after its receipt is pruned (#650).
    """
    target = target.expanduser().resolve()
    cache = _load_canonical_evidence_map(target)
    resolved: dict[str, str] = {}
    records = [
        _with_canonical_verify_evidence(target, record, cache=cache, resolved=resolved)
        for record in load_records(target)
    ]
    if resolved:
        _save_canonical_evidence_map(target, cache)
    return records


def _last_record_digest(path: Path) -> str | None:
    return _validate_completed_ledger(path)


def _ledger_corrupt_message(line_no: int, kind: str, path: Path, *, detail: str = "") -> str:
    """Bounded capture/append error that points operators at ``outcome repair``."""
    suffix = f" ({detail})" if detail else ""
    return (
        f"ledger corrupt at line {line_no}: {kind}{suffix}; inspect with `brigade outcome doctor` before repair: {path}"
    )


def _validate_completed_ledger_bytes(raw: bytes, path: Path) -> str | None:
    """Validate every completed ledger row in ``raw`` and return the last signed digest."""
    if not raw:
        return None
    if not raw.endswith(b"\n"):
        raise OutcomeLedgerError(
            _ledger_corrupt_message(
                raw.count(b"\n") + 1,
                "incomplete trailing record",
                path,
            )
        )
    previous_digest: str | None = None
    for line_no, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            raise OutcomeLedgerError(_ledger_corrupt_message(line_no, "empty", path, detail="empty ledger line"))
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OutcomeLedgerError(
                _ledger_corrupt_message(line_no, "is not valid JSON", path, detail=str(exc))
            ) from exc
        if not isinstance(row, dict):
            raise OutcomeLedgerError(
                _ledger_corrupt_message(line_no, "is not an object", path, detail="ledger line is not an object")
            )
        recorded_digest = row.get("digest")
        if not isinstance(recorded_digest, str) or not recorded_digest:
            continue
        recomputed = localio.canonical_json_digest(row, exclude_keys={"digest"})
        if recomputed != recorded_digest:
            raise OutcomeLedgerError(_ledger_corrupt_message(line_no, "digest mismatch", path))
        if "prev_digest" not in row:
            previous_digest = recorded_digest
            continue
        actual_prev = row.get("prev_digest")
        if actual_prev != previous_digest:
            raise OutcomeLedgerError(
                _ledger_corrupt_message(
                    line_no,
                    "digest chain break",
                    path,
                    detail=f"expected prev_digest={previous_digest!r}, actual={actual_prev!r}",
                )
            )
        previous_digest = recorded_digest
    return previous_digest


def _validate_completed_ledger(path: Path) -> str | None:
    """Validate every completed ledger row and return the last signed digest."""
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OutcomeLedgerError(f"could not read outcome ledger: {path}: {exc}") from exc
    return _validate_completed_ledger_bytes(raw, path)


def _recoverable_trailing_record(payload: dict, prefix: bytes, path: Path) -> bool:
    recorded_digest = payload.get("digest")
    if not isinstance(recorded_digest, str) or not recorded_digest:
        return False
    if localio.canonical_json_digest(payload, exclude_keys={"digest"}) != recorded_digest:
        return False
    if "prev_digest" not in payload:
        return False
    try:
        expected_prev = _validate_completed_ledger_bytes(prefix, path) if prefix else None
    except OutcomeLedgerError:
        return False
    return payload.get("prev_digest") == expected_prev


def _windows_lock_seek(handle) -> None:
    handle.seek(0)


def _ensure_lock_sentinel(handle) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(_LOCK_SENTINEL_BYTE)
        handle.flush()


def _acquire_file_lock(handle, *, wait_seconds: float) -> None:
    if os.name == "nt":
        import msvcrt

        _windows_lock_seek(handle)
        if wait_seconds <= 0:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            except OSError as exc:
                raise BlockingIOError from exc
            return
        deadline = time.monotonic() + wait_seconds
        while True:
            _windows_lock_seek(handle)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError from None
                time.sleep(0.01)
    else:
        import fcntl

        flags = fcntl.LOCK_EX
        if wait_seconds <= 0:
            flags |= fcntl.LOCK_NB
            fcntl.flock(handle.fileno(), flags)
            return
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError from None
                time.sleep(0.01)


def _release_file_lock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        _windows_lock_seek(handle)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _records_append_lock(lock_path: Path, *, wait_seconds: float | None = None):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    timeout = _LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds
    try:
        handle = lock_path.open("a+b")
    except OSError as exc:
        raise OutcomeLedgerError(f"could not open outcome ledger lock: {lock_path}: {exc}") from exc
    acquired = False
    try:
        try:
            _ensure_lock_sentinel(handle)
            _acquire_file_lock(handle, wait_seconds=timeout)
        except (BlockingIOError, TimeoutError) as exc:
            raise OutcomeLedgerError(f"could not acquire outcome ledger lock: {lock_path}") from exc
        except OSError as exc:
            raise OutcomeLedgerError(f"could not acquire outcome ledger lock: {lock_path}: {exc}") from exc
        acquired = True
        yield
    finally:
        body_had_error = sys.exc_info()[0] is not None
        cleanup_error: OutcomeLedgerError | None = None
        if acquired:
            try:
                _release_file_lock(handle)
            except OSError as exc:
                cleanup_error = OutcomeLedgerError(f"could not release outcome ledger lock: {lock_path}: {exc}")
                cleanup_error.__cause__ = exc
        try:
            handle.close()
        except OSError as exc:
            if cleanup_error is None:
                cleanup_error = OutcomeLedgerError(f"could not close outcome ledger lock: {lock_path}: {exc}")
                cleanup_error.__cause__ = exc
        if not body_had_error and cleanup_error is not None:
            raise cleanup_error


def _recover_interrupted_append(path: Path) -> None:
    """Drop a partial trailing JSONL line left by an interrupted append."""
    if not path.is_file():
        return
    action = "recover"
    try:
        with path.open("r+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                return
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) == b"\n":
                return
            handle.seek(0)
            data = handle.read()
            last_newline = data.rfind(b"\n")
            if last_newline < 0:
                tail = data
                truncate_to = 0
            else:
                tail = data[last_newline + 1 :]
                truncate_to = last_newline + 1
            if not tail:
                return
            try:
                payload = json.loads(tail.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and _recoverable_trailing_record(payload, data[:truncate_to], path):
                action = "finalize"
                handle.seek(0, os.SEEK_END)
                handle.write(b"\n")
            else:
                handle.truncate(truncate_to)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        detail = (
            "could not finalize interrupted outcome ledger row"
            if action == "finalize"
            else "could not recover interrupted outcome ledger write"
        )
        raise OutcomeLedgerError(f"{detail}: {path}: {exc}") from exc


def _outcome_causal_receipt(record: core.OutcomeRecord, receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build a recorded companion from the already-resolved parent receipt."""

    if not isinstance(receipt, dict):
        return None
    parent_digest: str | None = None
    if record.source == "run":
        artifacts = receipt.get("artifacts")
        parent_id = Path(artifacts).name if isinstance(artifacts, str) and artifacts else None
        parent_kind = "run"
    else:
        parent_id = receipt.get("run_id")
        parent_kind = "verify"
    existing = receipt.get("causal_receipt")
    if isinstance(existing, dict) and not causal_receipt.validate_receipt(existing):
        parent_digest = causal_receipt.receipt_digest(existing)
    if not isinstance(parent_id, str) or not parent_id:
        return None
    try:
        return causal_receipt.recorded_outcome(
            subject_id=f"{parent_id}--{record.artifact_id}",
            parent_kind=parent_kind,
            parent_id=parent_id,
            parent_digest=parent_digest,
        )
    except ValueError:
        return None


def _record_payload(record: core.OutcomeRecord) -> dict:
    row = dataclasses.asdict(record)
    row["schema_version"] = receipt_schema.OUTCOME_RECORD_SCHEMA_VERSION
    if row.get("code_graph_delta") is None:
        row.pop("code_graph_delta", None)
    if row.get("context_eval") is None:
        row.pop("context_eval", None)
    if row.get("content_fingerprint") is None:
        row.pop("content_fingerprint", None)
    if row.get("context") is None:
        row.pop("context", None)
    if row.get("capability_fingerprint") is None:
        row.pop("capability_fingerprint", None)
    if row.get("route") is None:
        row.pop("route", None)
    if row.get("route_fingerprint") is None:
        row.pop("route_fingerprint", None)
    if row.get("reused_evidence_ref") is None:
        row.pop("reused_evidence_ref", None)
    return row


def append_records(
    target: Path,
    records: list[core.OutcomeRecord],
    *,
    lineage: list[dict[str, Any] | None] | None = None,
) -> None:
    """Append outcome records under an exclusive lock with interrupted-write recovery.

    Recovery is row-scoped, not transactional: every completed JSONL line that
    ends with a newline remains durable and stays in the digest chain. Only an
    incomplete trailing line is discarded or finalized on the next append.
    A multi-record ``append_records`` call is therefore not all-or-nothing; an
    interrupted batch may leave earlier rows from that call on disk while the
    partial tail is removed before validation and the next append proceeds.
    """
    resolved_target = target.expanduser().resolve()
    from .work_cmd import helpers as work_helpers

    repo_root = work_helpers._git_value(resolved_target, "rev-parse", "--show-toplevel")
    if repo_root is not None:
        resolved_root = Path(repo_root).resolve()
        if resolved_target != resolved_root and not (resolved_target / ".brigade" / "config.json").is_file():
            raise OutcomeLedgerError(
                f"outcome ledger target {resolved_target} is inside git repository {resolved_root} "
                f"but lacks .brigade/config.json; rerun with --target {resolved_root}"
            )
    path = _records_path(target)
    lock_path = _records_lock_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _records_append_lock(lock_path):
        _recover_interrupted_append(path)
        prev_digest = _validate_completed_ledger(path)
        try:
            with path.open("a", encoding="utf-8") as handle:
                for index, record in enumerate(records):
                    row = _record_payload(record)
                    companion = lineage[index] if lineage is not None and index < len(lineage) else None
                    if companion is not None:
                        causal_receipt.stamp_causal_receipt(row, companion)
                    row["prev_digest"] = prev_digest
                    row["digest"] = localio.canonical_json_digest(row, exclude_keys={"digest"})
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
                    prev_digest = row["digest"]
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise OutcomeLedgerError(f"could not append outcome ledger records: {path}: {exc}") from exc


def _compact_code_graph_delta(receipt: dict) -> dict | None:
    delta = receipt.get("code_graph_delta")
    if not isinstance(delta, dict):
        return None
    compact = {
        key: delta[key]
        for key in (
            "status",
            "summary",
            "changed_symbol_count",
            "edge_churn",
            "raw_counts",
            "stale_graph_used",
        )
        if key in delta
    }
    return compact or None


def _compact_context_eval(receipt: dict) -> dict | None:
    context_eval = receipt.get("context_eval")
    if not isinstance(context_eval, dict):
        return None
    return dict(context_eval)


def _run_receipts_root(target: Path) -> Path:
    return target / ".brigade" / "runs"


def _read_run_receipt(run_dir: Path) -> tuple[dict[str, Any] | None, Path]:
    run_json = run_dir / "run.json"
    if not run_json.is_file():
        return None, run_json
    try:
        payload = json.loads(run_json.read_text())
    except (OSError, json.JSONDecodeError):
        return None, run_json
    if not isinstance(payload, dict):
        return None, run_json
    return payload, run_json


def _resolve_run_receipt(target: Path, run_id: str) -> tuple[dict[str, Any] | None, Path | None, str | None]:
    root = _run_receipts_root(target)
    if not root.is_dir():
        return None, None, f"run receipt directory not found: {root}"
    raw = Path(run_id).expanduser()
    if raw.is_absolute() or len(raw.parts) > 1 or raw.exists():
        run_dir = raw.resolve()
        if not run_dir.is_dir():
            return None, None, f"run receipt directory not found: {run_dir}"
        payload, run_json = _read_run_receipt(run_dir)
        if payload is None:
            return None, None, f"run receipt not found or invalid: {run_json}"
        return payload, run_json, None
    runs: list[tuple[Path, dict[str, Any], Path]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        payload, run_json = _read_run_receipt(child)
        if payload is not None:
            runs.append((child, payload, run_json))
    runs.sort(key=lambda item: str(item[1].get("started_at") or item[0].name), reverse=True)
    if run_id == "latest":
        if not runs:
            return None, None, f"run receipt not found: {run_id}"
        _, payload, run_json = runs[0]
        return payload, run_json, None
    from . import node as node_mod
    from . import run_id as run_id_mod

    try:
        identity = node_mod.load_identity(target)
    except node_mod.NodeIdentityError:
        identity = None
    local_short = identity.short_id if identity is not None else None
    matches = [
        (payload, run_json)
        for child, payload, run_json in runs
        if run_id_mod.run_id_matches(child.name, run_id, local_short=local_short)
    ]
    if not matches:
        return None, None, f"run receipt not found: {run_id}"
    if len(matches) > 1:
        return None, None, f"run receipt id is ambiguous: {run_id}"
    return matches[0][0], matches[0][1], None


def _run_receipt_signal_status(receipt: dict) -> str:
    if receipt.get("dry_run") is True:
        return "dry-run"
    if receipt.get("read_only") is True:
        return "read-only"
    return str(receipt.get("status") or "")


def _load_worker_results(run_json: Path) -> list[dict[str, Any]]:
    worker_results_path = run_json.parent / "worker-results.json"
    if not worker_results_path.is_file():
        return []
    payload = localio.read_json_dict(worker_results_path)
    if payload is None:
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


def _worker_results_ground_truth(run_json: Path) -> dict[str, Any] | None:
    worker_results_path = run_json.parent / "worker-results.json"
    if not worker_results_path.is_file():
        return None
    payload = localio.read_json_dict(worker_results_path)
    if payload is None:
        return None
    ground_truth = payload.get("ground_truth")
    return ground_truth if isinstance(ground_truth, dict) else None


def worker_result_failure(result: dict[str, Any]):
    """Read-time worker failure attribution for a worker-results entry."""
    from .worker_failure import worker_result_failure as _worker_result_failure

    return _worker_result_failure(result)


def _verify_summary_failed_verification(summary: dict[str, Any]) -> bool:
    """Return True when a verify receipt blames the subject rather than the harness.

    The failure-class vocabulary is owned by ``verify_trial``, which is what stamps
    these receipts. Read it from there rather than matching literals, so adding a
    class there cannot silently reclassify runs here.
    """
    failure_class = summary.get("failure_class")
    if failure_class in verify_trial.VERIFY_SKILL_FAILURE_CLASSES:
        return True
    commands = summary.get("commands")
    if isinstance(commands, list):
        stamped = [command for command in commands if isinstance(command, dict) and command.get("failure_class")]
        if any(command.get("failure_class") in verify_trial.VERIFY_SKILL_FAILURE_CLASSES for command in stamped):
            return True
        if stamped:
            return False
    status = str(summary.get("status") or "")
    if status == "failed":
        return failure_class not in verify_trial.VERIFY_INFRASTRUCTURE_FAILURE_CLASSES
    return False


def _resolve_verify_summary(target: Path, summary: dict[str, Any]) -> dict[str, Any]:
    run_id = summary.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return summary
    from .work_cmd import verification as verify_mod

    receipt, error = verify_mod._resolve_verify_receipt(target, run_id)
    if receipt is None:
        return summary
    return receipt


def _run_has_verifier_failure(target: Path, run_json: Path, receipt: dict[str, Any]) -> bool:
    ground_truth = _worker_results_ground_truth(run_json)
    if ground_truth is not None and ("latest_verify" in ground_truth or "verify_receipts" in ground_truth):
        # The run recorded which verifications belong to it. Trust that record and never
        # fall back to the workspace-wide latest receipt, which may belong to a later,
        # unrelated verification run.
        latest_verify = ground_truth.get("latest_verify")
        if isinstance(latest_verify, dict):
            resolved = _resolve_verify_summary(target, latest_verify)
            if _verify_summary_failed_verification(resolved):
                return True
        verify_receipts = ground_truth.get("verify_receipts")
        if isinstance(verify_receipts, list):
            for item in verify_receipts:
                if isinstance(item, dict) and _verify_summary_failed_verification(
                    _resolve_verify_summary(target, item)
                ):
                    return True
        return False
    started_at = receipt.get("started_at")
    if not isinstance(started_at, str):
        return False
    from .work_cmd import verification as verify_mod

    verify_receipt, error = verify_mod._resolve_verify_receipt(target, "latest")
    if verify_receipt is None:
        return False
    verify_started = str(verify_receipt.get("started_at") or "")
    if verify_started < started_at:
        return False
    return _verify_summary_failed_verification(verify_receipt)


_RUN_INFRASTRUCTURE_FAILURE_KINDS = frozenset({"worker-failure", None})


def _run_infrastructure_only(receipt: dict[str, Any], worker_results: list[dict[str, Any]]) -> bool:
    """Return whether a failed/incomplete run scores neutral (infrastructure-only).

    Classification is read-time only; see docs/outcome-scoring.md. Run-level kinds
    are resolved by ``run_failure_is_infrastructure_at_read_time``; deferred kinds
    consult worker ``domain`` (``infrastructure`` neutralizes, ``model-output`` scores negative).
    """
    status = _run_receipt_signal_status(receipt)
    if status not in core.RUN_INFRASTRUCTURE_NEUTRAL_STATUSES:
        return False
    from .worker_failure import resolve_run_failure_taxonomy, run_failure_is_infrastructure_at_read_time

    failure_kind, failure_phase = resolve_run_failure_taxonomy(receipt)
    run_level_infrastructure = run_failure_is_infrastructure_at_read_time(failure_kind, failure_phase)
    if run_level_infrastructure is True:
        return True
    if run_level_infrastructure is False:
        return False
    if failure_kind not in _RUN_INFRASTRUCTURE_FAILURE_KINDS:
        return False
    failed_workers = [result for result in worker_results if result.get("ok") is False]
    if not failed_workers:
        return failure_kind == "worker-failure"
    for result in failed_workers:
        failure = worker_result_failure(result)
        if failure is None:
            return False
        if failure.domain.value != "infrastructure":
            return False
    return True


def _run_is_suspected_noop(receipt: dict[str, Any], effective_status: str) -> bool:
    """Return whether the run receipt records a seat that produced no deliverable (#703).

    Dry runs and read-only runs are expected to change nothing, so they keep their own
    neutral statuses and never reach this check.
    """
    if effective_status in ("dry-run", "read-only"):
        return False
    return receipt.get("suspected_noop") is True


def _run_capture_signal_value(
    target: Path,
    run_json: Path,
    receipt: dict[str, Any],
    effective_status: str,
) -> int:
    if _run_is_suspected_noop(receipt, effective_status):
        # #703: the seat exited clean but shipped nothing. That is a model-quality
        # failure, so the run scores against the seat instead of counting as a success.
        # ``suspected-noop-run`` carries the same direction in MODEL_QUALITY_FAILURE_KINDS
        # for receipts that name the kind directly.
        return core.signal_value("run", "failed")
    worker_results = _load_worker_results(run_json)
    infrastructure_only = _run_infrastructure_only(receipt, worker_results)
    verifier_failed = _run_has_verifier_failure(target, run_json, receipt)
    return core.run_capture_signal_value(
        effective_status,
        infrastructure_only=infrastructure_only,
        verifier_failed=verifier_failed,
    )


def _decisions_dir(target: Path) -> Path:
    return target / "memory" / "outcome" / "decisions"


def load_transitions(target: Path) -> list[core.StatusTransition]:
    """Read the decision receipts into the transition log that status.json folds.

    Each receipt written by ``reconcile --apply`` carries the artifact id, the
    status it moved to, and when. Malformed or partial receipts are skipped so
    one bad file cannot break the drift check.
    """
    decisions = _decisions_dir(target)
    if not decisions.is_dir():
        return []
    transitions: list[core.StatusTransition] = []
    for path in sorted(decisions.glob("*.json")):
        payload = localio.read_json_dict(path) or {}
        artifact_id = payload.get("artifact_id")
        new_status = payload.get("new_status")
        created_at = payload.get("created_at")
        route_policy = payload.get("route_policy")
        if artifact_id and new_status and created_at:
            transitions.append(
                core.StatusTransition(
                    str(artifact_id),
                    str(new_status),
                    str(created_at),
                    route_policy=route_policy if isinstance(route_policy, dict) else None,
                )
            )
    return transitions


def _status_drift(rebuilt: dict[str, dict], persisted: dict[str, dict]) -> list[dict]:
    """Return per-artifact differences between the rebuilt and persisted status."""
    drift: list[dict] = []
    for artifact_id in sorted(set(rebuilt) | set(persisted)):
        want = rebuilt.get(artifact_id)
        have = persisted.get(artifact_id)
        if want == have:
            continue
        if want is None:
            drift.append({"artifact_id": artifact_id, "issue": "only-in-status", "persisted": have})
        elif have is None:
            drift.append({"artifact_id": artifact_id, "issue": "missing-from-status", "rebuilt": want})
        else:
            drift.append({"artifact_id": artifact_id, "issue": "mismatch", "rebuilt": want, "persisted": have})
    return drift


def rebuild_status(*, target: Path, check: bool = False, json_output: bool = False) -> int:
    """Rebuild status.json from the decision receipts and compare to the persisted file.

    Read-only drift oracle: proves ``status.json`` is reproducible from the
    append-only transition log before anything is allowed to trust it. With
    ``--check`` it exits non-zero on any drift (a hand-edit, a partial write, a
    corrupted status file). It never rewrites status.json; the canonical flip
    (demoting status.json to a regenerated cache) is a deliberate later step.
    """
    target = target.expanduser().resolve()
    rebuilt = core.fold_status(load_transitions(target))
    persisted = load_status(target)
    drift = _status_drift(rebuilt, persisted)
    if json_output:
        payload = {
            "target": str(target),
            "reproducible": not drift,
            "rebuilt_count": len(rebuilt),
            "persisted_count": len(persisted),
            "drift": drift,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if (check and drift) else 0
    print(f"outcome rebuild-status: {target}")
    print(f"rebuilt={len(rebuilt)} persisted={len(persisted)} reproducible={not drift}")
    if not drift:
        print("drift: none")
        return 0
    for item in drift:
        print(f"- {item['artifact_id']} [{item['issue']}]")
    return 1 if check else 0


def fork(
    *,
    target: Path,
    out: Path,
    config: core.ReconcileConfig | None = None,
    json_output: bool = False,
) -> int:
    """Project what the ratchet would decide under a hypothetical config (read-only).

    Skills use the receipt-only scorecard dual gate (#503); cards still replay
    ``records.jsonl`` through the legacy scorer. Never reads or writes live
    ``status.json``.
    """
    target = target.expanduser().resolve()
    config = config or core.ReconcileConfig()
    records = load_scoring_records(target)
    cohorts_by_artifact = _fingerprint_cohorts_by_artifact(target, records)
    scorecards = _scorecards_by_artifact(target)
    kinds = _artifact_kinds(records, {}, scorecards)
    now = localio.utc_now()
    skill_decisions = scorecard_mod.project_scorecard_statuses(scorecards, config=config, now=now)
    card_decisions = core.project_statuses(
        {artifact_id: cohorts.current for artifact_id, cohorts in cohorts_by_artifact.items()},
        config=config,
        now=now,
    )
    artifacts: dict[str, dict[str, Any]] = {}
    for artifact_id, decision in sorted(skill_decisions.items()):
        card = scorecards[artifact_id]
        effectiveness = card.dimensions.get("effectiveness", {})
        artifacts[artifact_id] = {
            "action": decision.action,
            "new_status": decision.new_status,
            "reason": decision.reason,
            "policy_version": scorecard_mod.SCORECARD_POLICY_VERSION,
            "helped": int(effectiveness.get("helped", 0)),
            "hurt": int(effectiveness.get("hurt", 0)),
            "wilson": float(effectiveness.get("wilson", 0.0)),
            "utility_guardrails": card.utility_guardrails,
            "dimensions": card.dimensions,
        }
    for artifact_id in _reconcile_artifact_ids(kinds, {}, scorecards, cohorts_by_artifact):
        if kinds.get(artifact_id, "skill") != "skill" or artifact_id in artifacts:
            continue
        artifacts[artifact_id] = {
            "action": "hold",
            "new_status": "candidate",
            "reason": "withheld: missing scorecard",
            "helped": 0,
            "hurt": 0,
            "wilson": 0.0,
        }
    for artifact_id, decision in sorted(card_decisions.items()):
        if artifact_id in artifacts:
            continue
        if kinds.get(artifact_id, "skill") != "card":
            continue
        score_obj = cohorts_by_artifact[artifact_id].current
        artifacts[artifact_id] = {
            "action": decision.action,
            "new_status": decision.new_status,
            "reason": decision.reason,
            "score": score_obj.score,
            "helped": score_obj.helped,
            "hurt": score_obj.hurt,
        }
    projection = {
        "version": 1,
        "target": str(target),
        "config": dataclasses.asdict(config),
        "artifacts": artifacts,
    }
    out = out.expanduser()
    localio.write_json(out, projection)
    if json_output:
        print(json.dumps({"out": str(out), "projection": projection}, indent=2, sort_keys=True))
        return 0
    promoted = sum(1 for a in artifacts.values() if a["new_status"] == "promoted")
    print(f"outcome fork: {out}")
    print(f"artifacts={len(artifacts)} would-promote={promoted} (config: {dataclasses.asdict(config)})")
    return 0


def _load_projection(path: Path) -> dict[str, dict]:
    payload = localio.read_json_dict(path) or {}
    artifacts = payload.get("artifacts")
    return artifacts if isinstance(artifacts, dict) else {}


def diff(*, target: Path, fork_a: Path, fork_b: Path, json_output: bool = False) -> int:
    """Compare two fork projections: which artifacts land on a different status.

    Structural, read-only comparison of two ``outcome fork`` outputs, mirroring
    ActiveGraph's ``compute_diff`` (a set-diff over resulting state). Reports
    artifacts whose projected status differs and artifacts present in only one
    fork.
    """
    _ = target
    a = _load_projection(fork_a.expanduser())
    b = _load_projection(fork_b.expanduser())
    changed: list[dict] = []
    for artifact_id in sorted(set(a) | set(b)):
        ea = a.get(artifact_id)
        eb = b.get(artifact_id)
        if ea is None:
            changed.append({"artifact_id": artifact_id, "issue": "only-in-b", "b": eb})
        elif eb is None:
            changed.append({"artifact_id": artifact_id, "issue": "only-in-a", "a": ea})
        elif ea.get("new_status") != eb.get("new_status") or ea.get("action") != eb.get("action"):
            changed.append(
                {
                    "artifact_id": artifact_id,
                    "issue": "differs",
                    "a": {"action": ea.get("action"), "new_status": ea.get("new_status")},
                    "b": {"action": eb.get("action"), "new_status": eb.get("new_status")},
                }
            )
    if json_output:
        payload = {"fork_a": str(fork_a), "fork_b": str(fork_b), "identical": not changed, "changed": changed}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"outcome diff: {fork_a} <-> {fork_b}")
    if not changed:
        print("changed: none")
        return 0
    for item in changed:
        print(f"- {item['artifact_id']} [{item['issue']}]")
    return 0


def _scores_by_artifact(records: list[core.OutcomeRecord]) -> dict[str, core.OutcomeScore]:
    grouped = _records_by_artifact(records)
    return {artifact_id: core.score_records(artifact_id, recs) for artifact_id, recs in grouped.items()}


def _records_by_artifact(records: list[core.OutcomeRecord]) -> dict[str, list[core.OutcomeRecord]]:
    grouped: dict[str, list[core.OutcomeRecord]] = {}
    for record in records:
        grouped.setdefault(record.artifact_id, []).append(record)
    return grouped


def _graph_count_value(value) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return None


def _graph_delta_counts(records: list[core.OutcomeRecord]) -> dict[str, int] | None:
    deltas = [
        record.code_graph_delta for record in core.scored_records(records) if isinstance(record.code_graph_delta, dict)
    ]
    if not deltas:
        return None
    counts = {"graph_changing": 0, "graph_no_op": 0}
    for delta in deltas:
        if delta.get("stale_graph_used") is True:
            continue
        if delta.get("status") != "ok":
            continue
        changed_symbols = _graph_count_value(delta.get("changed_symbol_count"))
        edge_churn = _graph_count_value(delta.get("edge_churn"))
        if (changed_symbols is not None and changed_symbols > 0) or (edge_churn is not None and edge_churn > 0):
            counts["graph_changing"] += 1
        elif changed_symbols == 0 and edge_churn == 0:
            counts["graph_no_op"] += 1
    return counts


def _graph_delta_counts_by_artifact(records: list[core.OutcomeRecord]) -> dict[str, dict[str, int]]:
    return {
        artifact_id: counts
        for artifact_id, recs in _records_by_artifact(records).items()
        if (counts := _graph_delta_counts(recs)) is not None
    }


def _graph_delta_human_suffix(counts: dict[str, int] | None) -> str:
    if counts is None:
        return ""
    return f" graph: {counts['graph_changing']} changing / {counts['graph_no_op']} no-op"


def _brief_hit_rate_value(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _brief_hit_stats(records: list[core.OutcomeRecord]) -> dict[str, float | int] | None:
    """Aggregate context_eval.brief_hit_rate across scored records with a rate."""
    rates: list[float] = []
    for record in core.scored_records(records):
        context_eval = record.context_eval
        if not isinstance(context_eval, dict):
            continue
        rate = _brief_hit_rate_value(context_eval.get("brief_hit_rate"))
        if rate is None:
            continue
        rates.append(rate)
    if not rates:
        return None
    mean = round(sum(rates) / len(rates), 3)
    return {
        "brief_hit_rate": mean,
        "brief_hit_samples": len(rates),
        "brief_hit_min": round(min(rates), 3),
        "brief_hit_max": round(max(rates), 3),
    }


def _brief_hit_stats_by_artifact(records: list[core.OutcomeRecord]) -> dict[str, dict[str, float | int]]:
    return {
        artifact_id: stats
        for artifact_id, recs in _records_by_artifact(records).items()
        if (stats := _brief_hit_stats(recs)) is not None
    }


def _brief_hit_human_suffix(stats: dict[str, float | int] | None) -> str:
    if stats is None:
        return ""
    return f" brief_hit: {stats['brief_hit_rate']:.3f} (n={stats['brief_hit_samples']})"


def _fingerprint_decision_fields(cohorts: core.FingerprintCohorts) -> dict[str, Any]:
    """Audit fields recording how fingerprinting narrowed a ratchet decision.

    Empty unless the current cohort actually dropped proven-stale evidence, so a
    never-edited artifact's decision receipt and JSON stay byte-identical to the
    pre-fingerprint ratchet.
    """
    fingerprint = cohorts.current_fingerprint
    if fingerprint is None or not cohorts.stale_records:
        return {}
    return {
        "content_fingerprint": fingerprint,
        "lifetime_score": cohorts.lifetime.score,
        "lifetime_helped": cohorts.lifetime.helped,
        "lifetime_hurt": cohorts.lifetime.hurt,
        "stale_records": cohorts.stale_records,
        "legacy_records": cohorts.legacy_records,
    }


def _fingerprint_decision_suffix(cohorts: core.FingerprintCohorts) -> str:
    """Human tail noting a decision scored the current revision, not lifetime.

    Empty unless proven-stale evidence was dropped, so unedited artifacts keep
    the pre-fingerprint one-line output.
    """
    fingerprint = cohorts.current_fingerprint
    if fingerprint is None or not cohorts.stale_records:
        return ""
    lifetime = cohorts.lifetime
    return (
        f" [rev {fingerprint[:12]}; scored current text only, "
        f"lifetime score={lifetime.score:.3f} helped={lifetime.helped} hurt={lifetime.hurt}, "
        f"stale={cohorts.stale_records} legacy={cohorts.legacy_records}]"
    )


def _route_breakdown(records: list[core.OutcomeRecord]) -> list[dict] | None:
    """Split an artifact's scored records into routed vs unrouted cohorts (Phase 1,
    display-only). This is the receipt the route feature was missing: whether runs
    that followed a composed route verify greener than bare verify captures.

    Returns None when no record carries a route manifest, so a pre-route ledger's
    output is unchanged. Routed records group by route_fingerprint (path/size);
    unrouted records (a bare verify with no owning run) share one cohort. Not
    scored: Phase 1 records and surfaces, the ratchet ignores it.
    """
    scored = core.scored_records(records)
    if not any(isinstance(r.route, dict) for r in scored):
        return None
    groups: dict[str, dict] = {}
    for record in scored:
        route = record.route if isinstance(record.route, dict) else {}
        if route.get("followed") and record.route_fingerprint:
            key = record.route_fingerprint
            label = f"routed {route.get('path', '?')}/{route.get('size', '?')}"
        else:
            key = "unrouted"
            label = "unrouted"
        entry = groups.setdefault(key, {"key": key, "label": label, "helped": 0, "hurt": 0, "neutral": 0})
        if record.signal_value > 0:
            entry["helped"] += 1
        elif record.signal_value < 0:
            entry["hurt"] += 1
        else:
            entry["neutral"] += 1
    # unrouted last, then routed cohorts by fingerprint for a stable read
    return sorted(groups.values(), key=lambda e: (e["key"] == "unrouted", e["key"]))


def _capability_breakdown(records: list[core.OutcomeRecord]) -> list[dict] | None:
    """Group an artifact's scored records by capability cohort (Phase 1, display-only).

    Returns None when no record carries a capability fingerprint, so the output of
    a pre-context ledger is unchanged. Records without one fall into an "unknown"
    bucket rather than being dropped, so the counts always reconcile with the
    lifetime fold. This is informational: Phase 1 does not score on these cohorts.
    """
    scored = core.scored_records(records)
    if not any(r.capability_fingerprint for r in scored):
        return None
    groups: dict[str, dict] = {}
    for record in scored:
        fp = record.capability_fingerprint or "unknown"
        manifest = record.context if isinstance(record.context, dict) else {}
        label = f"{manifest.get('harness', 'unknown')}/{manifest.get('model_family', 'unknown')}"
        entry = groups.setdefault(
            fp, {"capability_fingerprint": fp, "label": label, "helped": 0, "hurt": 0, "neutral": 0}
        )
        if record.signal_value > 0:
            entry["helped"] += 1
        elif record.signal_value < 0:
            entry["hurt"] += 1
        else:
            entry["neutral"] += 1
    return sorted(
        groups.values(), key=lambda e: (e["capability_fingerprint"] != "unknown", e["capability_fingerprint"])
    )


def score(*, target: Path, artifact_id: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    scores = _scores_by_artifact(load_scoring_records(target))
    if artifact_id is not None:
        scores = {artifact_id: scores.get(artifact_id, core.score_records(artifact_id, []))}
    ordered = sorted(scores.values(), key=lambda item: item.artifact_id)
    if json_output:
        payload = {"target": str(target), "scores": [dataclasses.asdict(item) for item in ordered]}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"outcome score: {target}")
    if not ordered:
        print("scores: none")
        return 0
    for item in ordered:
        print(
            f"- {item.artifact_id} score={item.score:.3f} helped={item.helped} hurt={item.hurt} neutral={item.neutral}"
        )
    return 0


def explain(*, target: Path, artifact_id: str, json_output: bool = False) -> int:
    """Show the per-signal trail behind an artifact's score, fingerprint-aware.

    The default score covers only records whose content fingerprint matches the
    artifact's current text; the lifetime fold is still shown. Without a
    resolvable current fingerprint the two are identical and the output stays in
    its pre-fingerprint shape.
    """
    target = target.expanduser().resolve()
    records = [record for record in load_scoring_records(target) if record.artifact_id == artifact_id]
    kind = next((r.artifact_kind for r in records if r.artifact_kind), "skill")
    cohorts = core.split_by_fingerprint(artifact_id, records, artifact_fingerprint(target, artifact_id, kind))
    score_obj = cohorts.current
    trail = [
        {
            "ts": record.ts,
            "source": record.source,
            "signal_value": record.signal_value,
            "evidence_ref": record.evidence_ref,
            "task_id": record.task_id,
            "content_fingerprint": record.content_fingerprint,
            "cohort": core.fingerprint_cohort(record, cohorts.current_fingerprint),
        }
        for record in sorted(records, key=lambda record: record.ts)
    ]
    capability_breakdown = _capability_breakdown(records)
    route_breakdown = _route_breakdown(records)
    current_capability = capability_fingerprint(context_manifest())
    cap_cohorts = core.split_by_capability(
        artifact_id, core.current_content_records(records, cohorts.current_fingerprint), current_capability
    )
    if json_output:
        payload = {
            "target": str(target),
            "artifact_id": artifact_id,
            "content_fingerprint": cohorts.current_fingerprint,
            "score": dataclasses.asdict(score_obj),
            "lifetime_score": dataclasses.asdict(cohorts.lifetime),
            "stale_records": cohorts.stale_records,
            "legacy_records": cohorts.legacy_records,
            "current_capability": cap_cohorts.current_capability,
            "capability_score": cap_cohorts.shrunk_rate,
            "capability_helped": cap_cohorts.capability.helped,
            "capability_hurt": cap_cohorts.capability.hurt,
            "off_capability_records": cap_cohorts.off_capability_records,
            "capability_legacy_records": cap_cohorts.capability_legacy_records,
            "trail": trail,
        }
        if capability_breakdown is not None:
            payload["capability_breakdown"] = capability_breakdown
        if route_breakdown is not None:
            payload["route_breakdown"] = route_breakdown
        from . import scorecard as scorecard_mod

        scorecard_payload = scorecard_mod.explain_payload(target, artifact_id, artifact_kind=kind)
        if scorecard_payload is not None:
            payload["scorecard"] = scorecard_payload
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"outcome explain: {artifact_id}")
    current_fp = cohorts.current_fingerprint
    if current_fp is not None:
        print(f"fingerprint: {current_fp[:12]}")
        print(
            f"score: {score_obj.score:.3f} helped={score_obj.helped} hurt={score_obj.hurt} "
            f"neutral={score_obj.neutral} (current fingerprint)"
        )
        lifetime = cohorts.lifetime
        print(
            f"lifetime: {lifetime.score:.3f} helped={lifetime.helped} hurt={lifetime.hurt} "
            f"neutral={lifetime.neutral} (stale={cohorts.stale_records} legacy={cohorts.legacy_records})"
        )
    else:
        print(
            f"score: {score_obj.score:.3f} helped={score_obj.helped} hurt={score_obj.hurt} neutral={score_obj.neutral}"
        )
    if cap_cohorts.current_capability is not None and cap_cohorts.off_capability_records:
        cap = cap_cohorts.capability
        print(
            f"capability [{cap_cohorts.current_capability[:12]}]: {cap_cohorts.shrunk_rate:.3f} "
            f"helped={cap.helped} hurt={cap.hurt} "
            f"(off_cap={cap_cohorts.off_capability_records} cap_legacy={cap_cohorts.capability_legacy_records}, "
            f"shrunk toward pooled; retrieval only, not scored by the ratchet)"
        )
    if capability_breakdown is not None:
        print("capability cohorts (records seen per context):")
        for entry in capability_breakdown:
            print(
                f"- {entry['capability_fingerprint'][:12]} {entry['label']} "
                f"helped={entry['helped']} hurt={entry['hurt']} neutral={entry['neutral']}"
            )
    if route_breakdown is not None:
        print("route cohorts (did following the route verify greener? retrieval only, not scored):")
        for entry in route_breakdown:
            total = entry["helped"] + entry["hurt"]
            rate = f"{entry['helped'] / total:.3f}" if total else "n/a"
            print(
                f"- {entry['label']} helped={entry['helped']} hurt={entry['hurt']} "
                f"neutral={entry['neutral']} (help-rate {rate})"
            )
    if not trail:
        print("trail: none")
    else:
        for item in trail:
            tag = f" [{item['cohort']}]" if cohorts.pinned else ""
            print(f"- {item['ts']} {item['source']} {item['signal_value']:+d} ({item['evidence_ref']}){tag}")
    from . import scorecard as scorecard_mod

    scorecard_payload = scorecard_mod.explain_payload(target, artifact_id, artifact_kind=kind)
    if scorecard_payload is not None:
        print("scorecard (receipt-only):")
        dimensions = scorecard_payload["dimensions"]
        effectiveness = dimensions["effectiveness"]
        print(
            f"- effectiveness wilson={effectiveness['wilson']:.3f} "
            f"helped={effectiveness['helped']} hurt={effectiveness['hurt']} trials={effectiveness['trials']}"
        )
        if scorecard_payload.get("ineligible_summary"):
            reasons = ", ".join(f"{key}={value}" for key, value in scorecard_payload["ineligible_summary"].items())
            print(f"- ineligible: {reasons}")
        receipt_trail = scorecard_payload.get("receipt_trail") or []
        if not receipt_trail:
            print("- receipt trail: none")
        else:
            for item in receipt_trail:
                binding = item.get("subject_binding") or {}
                subject_id = binding.get("artifact_id", "?")
                print(
                    f"- {item.get('started_at') or '?'} {subject_id} "
                    f"eligible={item.get('eligible')} reason={item.get('reason')} "
                    f"effectiveness={item.get('effectiveness')}"
                )
    return 0


def capture(
    *,
    target: Path,
    artifact_id: str,
    artifact_kind: str = "skill",
    task_id: str | None = None,
    run_id: str | None = None,
    run_receipt: str | None = None,
    json_output: bool = False,
) -> int:
    """Correlate a verified receipt outcome into the digest-chained ledger.

    The signal is the receipt status (a real run result the model cannot author),
    not an LLM judgment. The caller names which artifact the run exercised, and
    appended records carry a tamper-evident prev_digest/digest chain.
    """
    target = target.expanduser().resolve()
    if run_id is not None and run_receipt is not None:
        print("error: pass either --run-id or --run-receipt, not both", file=sys.stderr)
        return 1
    if _artifact_id_has_controls(artifact_id):
        # Reject before any stdout or ledger write; strip Cc/Cf from the error text.
        kind_label = "skill" if artifact_kind == "skill" else "card"
        print(
            f"error: {kind_label} id contains control characters: {_warning_label(artifact_id)}",
            file=sys.stderr,
        )
        return 1
    if not _artifact_known(target, artifact_id, artifact_kind):
        print(_unknown_artifact_warning(target, artifact_id, artifact_kind), file=sys.stderr)
    source = "verify"
    effective_status = ""
    evidence_ref = ""
    signal: int | None = None
    ts = localio.utc_now_iso()
    code_graph_delta: dict[str, Any] | None = None
    context_eval: dict[str, Any] | None = None
    run_agent: dict[str, Any] | None = None
    route_run_payload: dict[str, Any] | None = None
    route_run_json: Path | None = None
    reused_evidence_ref: str | None = None
    if run_receipt is not None:
        receipt, run_json, error = _resolve_run_receipt(target, run_receipt)
        if receipt is None or run_json is None:
            print(f"error: {error}", file=sys.stderr)
            return 1
        source = "run"
        effective_status = _run_receipt_signal_status(receipt)
        evidence_ref = str(run_json)
        signal = _run_capture_signal_value(target, run_json, receipt, effective_status)
        ts = str(receipt.get("completed_at") or receipt.get("started_at") or localio.utc_now_iso())
        code_graph_delta = _compact_code_graph_delta(receipt)
        context_eval = _compact_context_eval(receipt)
        agent = receipt.get("agent")
        run_agent = agent if isinstance(agent, dict) else None
        route_run_payload = receipt
        route_run_json = run_json
    else:
        from .work_cmd import verification as verify_mod

        receipt, error = verify_mod._resolve_verify_receipt(target, run_id or "latest")
        if receipt is None:
            print(f"error: {error}", file=sys.stderr)
            return 1
        effective_status = str(receipt.get("status") or "")
        evidence_ref = verify_mod.canonical_verify_evidence_ref(target, receipt)
        own_ref = verify_mod._verify_receipt_evidence_ref(receipt)
        if own_ref and own_ref != evidence_ref:
            reused_evidence_ref = own_ref
        ts = str(receipt.get("completed_at") or receipt.get("started_at") or localio.utc_now_iso())
        code_graph_delta = _compact_code_graph_delta(receipt)
        signal = core.signal_value(source, effective_status)
    manifest = context_manifest(run_agent)
    route = route_manifest(route_run_payload, route_run_json)
    if signal is None:
        signal = core.signal_value(source, effective_status)
    record = core.OutcomeRecord(
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        task_id=task_id or "",
        source=source,
        signal_value=signal,
        evidence_ref=evidence_ref,
        ts=ts,
        code_graph_delta=code_graph_delta,
        context_eval=context_eval,
        content_fingerprint=artifact_fingerprint(target, artifact_id, artifact_kind),
        context=manifest,
        capability_fingerprint=capability_fingerprint(manifest),
        route=route,
        route_fingerprint=route_fingerprint(route),
        reused_evidence_ref=reused_evidence_ref,
    )
    companion = _outcome_causal_receipt(record, receipt)
    try:
        append_records(target, [record], lineage=[companion] if companion is not None else None)
    except OutcomeLedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps({"target": str(target), "record": _record_payload(record)}, indent=2, sort_keys=True))
        return 0
    print(f"outcome capture: {artifact_id}")
    print(f"source: {source} [{effective_status}] signal={record.signal_value:+d}")
    print(f"evidence: {record.evidence_ref}")
    if record.content_fingerprint:
        print(f"fingerprint: {record.content_fingerprint[:12]}")
    if record.capability_fingerprint:
        print(f"capability: {record.capability_fingerprint[:12]} ({manifest['harness']}/{manifest['model_family']})")
    if record.route_fingerprint:
        print(f"route: {record.route_fingerprint[:12]} ({route['path']}/{route['size']}, followed)")
    else:
        print("route: unrouted (no owning brigade run)")
    return 0


def _silently(fn, **kwargs) -> int:
    """Call a noisy command function while swallowing its stdout/stderr."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        return fn(**kwargs)


def _execute_skill_decision(target: Path, artifact_id: str, action: str) -> str:
    """Perform a decision's physical side effect for a skill artifact.

    install -> install across all harnesses (idempotent). rollback -> restore the
    last good snapshot per harness, or uninstall when a first install has no prior
    snapshot (the first-install-safe rule). Defensive: a skills failure is
    recorded, never raised, so one artifact cannot abort the autonomous run.
    """
    from . import skills_cmd

    try:
        if action == "install":
            if not skills_cmd._registry_entry_present(target, artifact_id):
                # The ledger named an artifact that was never accepted into the
                # registry, so there is nothing to install. Report it distinctly
                # instead of a generic rc failure; reconcile keeps it a candidate.
                return "install-skipped: not in registry"
            rc = _silently(
                skills_cmd.install, workspace=target, skill=artifact_id, harness="all", force=True, json_output=True
            )
            return "installed" if rc == 0 else f"install-failed(rc={rc})"
        if action == "rollback":
            outcomes: list[str] = []
            for harness in skills_cmd._install_targets(target):
                rc = _silently(
                    skills_cmd.rollback, workspace=target, skill=artifact_id, harness=harness, json_output=True
                )
                if rc == 0:
                    outcomes.append(f"{harness}:rollback")
                    continue
                rc = _silently(
                    skills_cmd.uninstall, workspace=target, skill=artifact_id, harness=harness, json_output=True
                )
                outcomes.append(f"{harness}:uninstall" if rc == 0 else f"{harness}:noop")
            return "reverted:" + ",".join(outcomes)
        return "noop"
    except Exception as exc:  # noqa: BLE001 - autonomy must survive any skills failure
        return f"error:{type(exc).__name__}"


def reconcile(
    *,
    target: Path,
    apply: bool = False,
    config: core.ReconcileConfig | None = None,
    json_output: bool = False,
) -> int:
    """Run the autonomous ratchet over every scored artifact.

    Dry-run by default (the canary posture): it reports what it would promote or
    roll back without writing. With ``apply`` it writes a decision receipt per
    transition, advances the persisted status, and performs the physical skill
    install/rollback. No human approval is consulted.

    Skills promote only through the receipt scorecard dual gate (#503); legacy
    ``records.jsonl`` signals never advance skill promotion. Cards still use the
    legacy scorer. Routing authority is stamped via ``route_policy.policy_version``
    on promotion; demotion drops broad authority even when physical rollback fails.
    """
    target = target.expanduser().resolve()
    config = config or core.ReconcileConfig()
    records = load_scoring_records(target)
    cohorts_by_artifact = _fingerprint_cohorts_by_artifact(target, records)
    scorecards = _scorecards_by_artifact(target)
    graph_counts = _graph_delta_counts_by_artifact(records)
    brief_stats = _brief_hit_stats_by_artifact(records)
    status_map = load_status(target)
    kinds = _artifact_kinds(records, status_map, scorecards)
    now = localio.utc_now()

    results: list[_ReconcileItem] = []
    reported: list[_ReconcileItem] = []
    for artifact_id in _reconcile_artifact_ids(kinds, status_map, scorecards, cohorts_by_artifact):
        entry = status_map.get(artifact_id) or {}
        prior_status = entry.get("status", "candidate")
        last_action_ts = localio.parse_iso_datetime(entry.get("last_action_ts"))
        artifact_kind = kinds.get(artifact_id, "skill")
        cohorts = cohorts_by_artifact.get(artifact_id)
        card = scorecards.get(artifact_id)
        decision = _decide_reconcile_item(
            artifact_id,
            artifact_kind,
            cohorts=cohorts,
            card=card,
            prior_status=prior_status,
            last_action_ts=last_action_ts,
            now=now,
            config=config,
        )
        item = _ReconcileItem(
            decision=decision,
            prior_status=prior_status,
            cohorts=cohorts,
            scorecard=card,
        )
        if decision.action != "hold":
            results.append(item)
        elif _surface_hold_decision(decision, prior_status):
            reported.append(item)

    applied: list[str] = []
    executions: dict[str, str] = {}
    effective_status: dict[str, str] = {}
    if apply and results:
        for item in results:
            decision = item.decision
            cohorts = item.cohorts
            prior_status = item.prior_status
            execution = "noop"
            if decision.action in ("install", "rollback"):
                if kinds.get(decision.artifact_id, "skill") == "skill":
                    execution = _execute_skill_decision(target, decision.artifact_id, decision.action)
                else:
                    execution = "skipped: card execution is v1.1"
            executions[decision.artifact_id] = execution
            install_failed = (
                decision.action == "install"
                and kinds.get(decision.artifact_id, "skill") == "skill"
                and execution != "installed"
            )
            # Rollback demotes routing authority immediately; physical revert is best-effort.
            if decision.action == "rollback":
                new_status = decision.new_status
            else:
                new_status = prior_status if install_failed else decision.new_status
            effective_status[decision.artifact_id] = new_status
            if cohorts is not None:
                score_payload: dict[str, Any] = dataclasses.asdict(cohorts.current)
            elif item.scorecard is not None:
                score_payload = {
                    "dimensions": item.scorecard.dimensions,
                    "utility_guardrails": item.scorecard.utility_guardrails,
                }
            else:
                score_payload = {}
            receipt = {
                "schema_version": receipt_schema.OUTCOME_DECISION_SCHEMA_VERSION,
                "artifact_id": decision.artifact_id,
                "action": decision.action,
                "prior_status": prior_status,
                "new_status": new_status,
                "decided_status": decision.new_status,
                "reason": decision.reason,
                "score": score_payload,
                "execution": execution,
                "created_at": now.isoformat(),
            }
            if cohorts is not None:
                receipt.update(_fingerprint_decision_fields(cohorts))
            receipt.update(_scorecard_decision_fields(item.scorecard))
            if (
                kinds.get(decision.artifact_id, "skill") == "skill"
                and new_status == "promoted"
                and not install_failed
                and decision.action in {"install", "bump"}
            ):
                receipt["route_policy"] = scorecard_mod.route_policy_marker_for_promotion()
            _write_decision_receipt(target, now, decision.artifact_id, receipt)
            status_map[decision.artifact_id] = _status_entry_for_transition(
                new_status=new_status,
                now=now,
                decision=decision,
                install_failed=install_failed,
                artifact_kind=kinds.get(decision.artifact_id, "skill"),
            )
            if not install_failed:
                applied.append(decision.artifact_id)
        localio.write_json(_status_path(target), {"version": 1, "artifacts": status_map})

    decisions_payload = []
    for item in [*results, *reported]:
        decision = item.decision
        cohorts = item.cohorts
        prior_status = item.prior_status
        payload_item: dict[str, Any] = {
            "artifact_id": decision.artifact_id,
            "action": decision.action,
            "prior_status": prior_status,
            "new_status": effective_status.get(decision.artifact_id, decision.new_status),
            "decided_status": decision.new_status,
            "reason": decision.reason,
            "execution": executions.get(decision.artifact_id, "dry-run"),
        }
        if cohorts is not None:
            payload_item["score"] = cohorts.current.score
            payload_item.update(_fingerprint_decision_fields(cohorts))
        elif item.scorecard is not None:
            payload_item["score"] = item.scorecard.dimensions.get("effectiveness", {}).get("wilson", 0.0)
            payload_item["dimensions"] = item.scorecard.dimensions
            payload_item["utility_guardrails"] = item.scorecard.utility_guardrails
        payload_item.update(_scorecard_decision_fields(item.scorecard))
        counts = graph_counts.get(decision.artifact_id)
        if counts is not None:
            payload_item.update(counts)
        stats = brief_stats.get(decision.artifact_id)
        if stats is not None:
            payload_item.update(stats)
        decisions_payload.append(payload_item)
    payload = {
        "target": str(target),
        "apply": apply,
        "decisions": decisions_payload,
        "applied": applied,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    mode = "apply" if apply else "dry-run"
    print(f"outcome reconcile: {target} ({mode})")
    output_items = [*results, *reported]
    if not output_items:
        print("decisions: none")
        return 0
    for item in output_items:
        decision = item.decision
        cohorts = item.cohorts
        prior_status = item.prior_status
        shown_status = effective_status.get(decision.artifact_id, decision.new_status)
        tail = f" -> {executions[decision.artifact_id]}" if apply and decision.artifact_id in executions else ""
        fingerprint_tail = _fingerprint_decision_suffix(cohorts) if cohorts is not None else ""
        graph_tail = _graph_delta_human_suffix(graph_counts.get(decision.artifact_id))
        brief_tail = _brief_hit_human_suffix(brief_stats.get(decision.artifact_id))
        print(
            f"- {decision.artifact_id} {prior_status} -> {shown_status} "
            f"[{decision.action}] {decision.reason}{fingerprint_tail}{graph_tail}{brief_tail}{tail}"
        )
    return 0


def _capability_human_suffix(cohorts: core.CapabilityCohorts) -> str:
    """Human tail showing the current-capability score, only when it diverges.

    Empty unless records under a different capability were actually set aside, so
    a pre-context ledger (or one run under a single capability) keeps the
    pre-phase-2 one-line output.
    """
    if cohorts.current_capability is None or not cohorts.off_capability_records:
        return ""
    cap = cohorts.capability
    return (
        f" [cap {cohorts.current_capability[:12]}: {cohorts.shrunk_rate:.3f} "
        f"helped={cap.helped} hurt={cap.hurt}, off_cap={cohorts.off_capability_records}]"
    )


def rank(
    *,
    target: Path,
    json_output: bool = False,
    by_capability: bool = False,
    recency_half_life_days: float | None = None,
    now: Any = None,
) -> int:
    """Rank learned artifacts by verified outcome, most-proven first.

    The blended retrieval score (rank_score) leaves room for confidence and
    keyword inputs that the live retrieval path supplies; on its own it orders
    by what a real signal has confirmed. When context_eval samples exist,
    mean brief_hit_rate is a secondary quality key so skills whose pre-run
    context named the files they actually touched rise among equal scores.
    Install/rollback thresholds still use verified exit-code signals only.

    Fingerprint-aware: the default score covers only records whose content
    fingerprint matches the artifact's current text, so an edited skill earns
    its rank back instead of coasting on signals for text that no longer
    exists. Lifetime counts stay visible alongside.

    Capability-aware (Phase 2): the current runtime capability (harness, model
    family, interpreter, platform) is resolved once, and each artifact also
    carries a capability-shrunk score over the content-current records earned
    under that capability, thin cohorts pulled toward the pooled rate. With
    ``by_capability`` the ranking sorts by that score instead; by default the
    pooled content score still orders the list and the ratchet is untouched.

    Recency-aware: with ``recency_half_life_days`` set, the score rank sorts by
    (pooled by default, capability with ``by_capability``) is computed over
    recency-weighted counts, so a signal's weight halves every half-life and
    credit under a drifted-away environment fades. Off by default (byte-identical
    output). The ratchet never uses recency; this is retrieval only.
    """
    target = target.expanduser().resolve()
    records = load_scoring_records(target)
    cohorts_by_artifact = _fingerprint_cohorts_by_artifact(target, records)
    current_capability = capability_fingerprint(context_manifest())
    now = now or localio.utc_now()
    half_life_seconds = recency_half_life_days * 86400.0 if recency_half_life_days else None
    weighting_now = now if half_life_seconds is not None else None
    records_by_artifact = _records_by_artifact(records)
    content_current_by_artifact = {
        artifact_id: core.current_content_records(records_by_artifact[artifact_id], fpc.current_fingerprint)
        for artifact_id, fpc in cohorts_by_artifact.items()
    }
    capability_cohorts = {
        artifact_id: core.split_by_capability(
            artifact_id,
            content_current_by_artifact[artifact_id],
            current_capability,
            now=weighting_now,
            half_life_seconds=half_life_seconds,
        )
        for artifact_id in cohorts_by_artifact
    }
    # The pooled score the default sort uses: recency-weighted when enabled, else
    # the plain content-current Wilson (so default output stays byte-identical).
    pooled_sort_score = {
        artifact_id: (
            core.recency_weighted_wilson(content_current_by_artifact[artifact_id], now, half_life_seconds)
            if half_life_seconds is not None
            else cohorts_by_artifact[artifact_id].current.score
        )
        for artifact_id in cohorts_by_artifact
    }
    graph_counts = _graph_delta_counts_by_artifact(records)
    brief_stats = _brief_hit_stats_by_artifact(records)
    from . import scorecard as scorecard_mod

    scorecards_by_artifact = {card.subject.artifact_id: card for card in scorecard_mod.build_scorecards(target)}

    def blended(artifact_id: str) -> float:
        return core.rank_score(confidence=0.0, outcome=pooled_sort_score[artifact_id], keyword=0.0)

    def sort_key(item: tuple[str, core.FingerprintCohorts]) -> tuple:
        artifact_id, _cohorts = item
        stats = brief_stats.get(artifact_id)
        # Missing brief samples sort after measured ones at the same Wilson score.
        hit = float(stats["brief_hit_rate"]) if stats is not None else -1.0
        if by_capability:
            # Order by the current-capability estimate, then by how much evidence
            # was actually earned under this capability (so a skill demonstrated
            # here outranks one only demonstrated elsewhere at an equal shrunk
            # estimate), then pooled, then brief hit.
            cap = capability_cohorts[artifact_id]
            on_capability = cap.capability.helped + cap.capability.hurt
            return (-cap.shrunk_rate, -on_capability, -blended(artifact_id), -hit, artifact_id)
        return (-blended(artifact_id), -hit, artifact_id)

    ordered = sorted(cohorts_by_artifact.items(), key=sort_key)
    ranking_payload = []
    for artifact_id, cohorts in ordered:
        current = cohorts.current
        lifetime = cohorts.lifetime
        cap = capability_cohorts[artifact_id]
        entry = {
            "artifact_id": artifact_id,
            "score": current.score,
            "rank_score": blended(artifact_id),
            "helped": current.helped,
            "hurt": current.hurt,
            "content_fingerprint": cohorts.current_fingerprint,
            "lifetime_score": lifetime.score,
            "lifetime_helped": lifetime.helped,
            "lifetime_hurt": lifetime.hurt,
            "stale_records": cohorts.stale_records,
            "legacy_records": cohorts.legacy_records,
            "capability_fingerprint": cap.current_capability,
            "capability_score": cap.shrunk_rate,
            "capability_helped": cap.capability.helped,
            "capability_hurt": cap.capability.hurt,
            "off_capability_records": cap.off_capability_records,
            "capability_legacy_records": cap.capability_legacy_records,
        }
        if half_life_seconds is not None:
            entry["recency_half_life_days"] = recency_half_life_days
            entry["recency_score"] = pooled_sort_score[artifact_id]
        counts = graph_counts.get(artifact_id)
        if counts is not None:
            entry.update(counts)
        stats = brief_stats.get(artifact_id)
        if stats is not None:
            entry.update(stats)
        scorecard_entry = scorecards_by_artifact.get(artifact_id)
        if scorecard_entry is not None:
            entry["policy_version"] = scorecard_mod.SCORECARD_POLICY_VERSION
            entry["dimensions"] = scorecard_entry.dimensions
            entry["utility_guardrails"] = scorecard_entry.utility_guardrails
            if scorecard_entry.ineligible_summary:
                entry["ineligible_summary"] = scorecard_entry.ineligible_summary
        ranking_payload.append(entry)

    ranked_ids = {artifact_id for artifact_id, _ in ordered}
    for artifact_id, scorecard_entry in sorted(scorecards_by_artifact.items()):
        if artifact_id in ranked_ids:
            continue
        entry = {
            "artifact_id": artifact_id,
            "score": 0.0,
            "rank_score": 0.0,
            "helped": 0,
            "hurt": 0,
            "content_fingerprint": scorecard_entry.subject.content_fingerprint,
            "lifetime_score": 0.0,
            "lifetime_helped": 0,
            "lifetime_hurt": 0,
            "stale_records": 0,
            "legacy_records": 0,
            "capability_fingerprint": current_capability,
            "capability_score": 0.0,
            "capability_helped": 0,
            "capability_hurt": 0,
            "off_capability_records": 0,
            "capability_legacy_records": 0,
            "policy_version": scorecard_mod.SCORECARD_POLICY_VERSION,
            "dimensions": scorecard_entry.dimensions,
            "utility_guardrails": scorecard_entry.utility_guardrails,
        }
        if scorecard_entry.ineligible_summary:
            entry["ineligible_summary"] = scorecard_entry.ineligible_summary
        ranking_payload.append(entry)
    payload = {
        "target": str(target),
        "ranking": ranking_payload,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    mode = ""
    if by_capability:
        mode += " (by capability)"
    if half_life_seconds is not None:
        mode += f" (recency {recency_half_life_days:g}d)"
    print(f"outcome rank: {target}{mode}")
    if not ordered:
        print("ranking: none")
        return 0
    for artifact_id, cohorts in ordered:
        current = cohorts.current
        graph_tail = _graph_delta_human_suffix(graph_counts.get(artifact_id))
        brief_tail = _brief_hit_human_suffix(brief_stats.get(artifact_id))
        capability_tail = _capability_human_suffix(capability_cohorts[artifact_id])
        recency_tail = f" recency={pooled_sort_score[artifact_id]:.3f}" if half_life_seconds is not None else ""
        fingerprint_tail = ""
        rank_fp = cohorts.current_fingerprint
        # Only a PROVEN-stale cohort earns the tail: at rollout (legacy records
        # only, nothing edited) the line stays byte-identical to the
        # pre-fingerprint output.
        if rank_fp is not None and cohorts.stale_records:
            lifetime = cohorts.lifetime
            fingerprint_tail = (
                f" [rev {rank_fp[:12]}; lifetime score={lifetime.score:.3f} "
                f"helped={lifetime.helped} hurt={lifetime.hurt}, "
                f"stale={cohorts.stale_records} legacy={cohorts.legacy_records}]"
            )
        print(
            f"- {artifact_id} score={current.score:.3f} helped={current.helped} "
            f"hurt={current.hurt}{recency_tail}{fingerprint_tail}{capability_tail}{graph_tail}{brief_tail}"
        )
    return 0


def doctor(*, target: Path, json_output: bool = False) -> int:
    from . import outcome_repair, receipts_cmd

    target = target.expanduser().resolve()
    payload = receipts_cmd.verify_payload(target)
    break_info = outcome_repair.diagnose_completed_ledger(_records_path(target))
    completed_ledger: dict[str, Any]
    if break_info is None:
        completed_ledger = {"status": "ok", "path": str(_records_path(target))}
    else:
        completed_ledger = {
            "status": "corrupt",
            "path": str(break_info.path),
            "kind": break_info.kind,
            "line_no": break_info.line_no,
            "expected_prev": break_info.expected_prev,
            "actual_prev": break_info.actual_prev,
            "suspected_cause": break_info.suspected_cause,
            "invalid_segment_start": break_info.invalid_segment_start,
            "invalid_segment_end": break_info.invalid_segment_end,
            "repair_command": "brigade outcome repair",
            "repair_requires_operator_confirm": True,
        }
    payload["completed_ledger"] = completed_ledger
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"outcome doctor: {target}")
    print(f"receipts: {receipts_cmd.summary_detail(target)}")
    if break_info is None:
        print("completed_ledger: ok")
    else:
        print(
            f"completed_ledger: CORRUPT line={break_info.line_no} kind={break_info.kind} "
            f"expected_prev={break_info.expected_prev!r} actual_prev={break_info.actual_prev!r}"
        )
        print(f"suspected_cause: {break_info.suspected_cause}")
        print("repair: brigade outcome repair (requires explicit operator confirmation)")
    return 0


def record(
    *,
    target: Path,
    artifact_id: str,
    source: str,
    status: str,
    evidence_ref: str = "",
    artifact_kind: str = "skill",
    task_id: str | None = None,
    json_output: bool = False,
) -> int:
    """Record an explicit, non-verify outcome signal (e.g. friction cleared/recurred).

    The weight comes from the fixed rule table, so a producer can feed the loop a
    real signal without an LLM judging it.
    """
    target = target.expanduser().resolve()
    manifest = context_manifest()
    new_record = core.OutcomeRecord(
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        task_id=task_id or "",
        source=source,
        signal_value=core.signal_value(source, status),
        evidence_ref=evidence_ref,
        ts=localio.utc_now_iso(),
        content_fingerprint=artifact_fingerprint(target, artifact_id, artifact_kind),
        context=manifest,
        capability_fingerprint=capability_fingerprint(manifest),
    )
    try:
        append_records(target, [new_record])
    except OutcomeLedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps({"target": str(target), "record": _record_payload(new_record)}, indent=2, sort_keys=True))
        return 0
    print(f"outcome record: {artifact_id}")
    print(f"source: {source} [{status}] signal={new_record.signal_value:+d}")
    return 0


def health(target: Path) -> dict:
    """Surface whether the receipt-only verified-learning loop is actually being fed.

    Scorecard eligibility is projected exclusively from verify receipts. Legacy
    ``records.jsonl`` rows remain audit-only and are never joined for scoring.
    """
    target = target.expanduser().resolve()
    from . import scorecard as scorecard_mod
    from .work_cmd import helpers as work_helpers

    records = load_records(target)
    scores = _scores_by_artifact(load_scoring_records(target))
    runs_root = work_helpers._verify_runs_root(target)
    verify_run_count = sum(1 for child in runs_root.iterdir() if child.is_dir()) if runs_root.is_dir() else 0
    record_count = len(records)
    promoted_count = sum(1 for entry in load_status(target).values() if entry.get("status") == "promoted")
    receipt_audit = scorecard_mod.receipt_scorecard_audit(target)

    issues: list[dict] = []
    if verify_run_count > 0 and receipt_audit["eligible"] == 0:
        issues.append(
            {
                "status": "warn",
                "name": "outcome_loop_half_fed",
                "detail": (
                    f"{verify_run_count} verify run(s) but 0 eligible receipt(s); "
                    "run `brigade work verify run --target . --manifest <id>` "
                    "with a tracked manifest under verify/manifests/ so receipts "
                    "carry verifier-authored subject_binding and check_role"
                ),
            }
        )
    elif verify_run_count == 0 and receipt_audit["total_receipts"] == 0:
        issues.append(
            {
                "status": "warn",
                "name": "outcome_loop_dormant",
                "detail": "no verify runs yet; the receipt-only verified-learning loop is not running",
            }
        )
    return {
        "records_path": str(_records_path(target)),
        "verify_run_count": verify_run_count,
        "record_count": record_count,
        "legacy_records_audit_only": True,
        "legacy_records_note": (
            "Outcome ledger rows in records.jsonl are audit-only; they cannot be backfilled into receipt scorecards."
        ),
        "scored_artifact_count": len(scores),
        "promoted_count": promoted_count,
        "attributed_receipt_count": receipt_audit["attributed"],
        "unattributed_receipt_count": receipt_audit["unattributed"],
        "eligible_receipt_count": receipt_audit["eligible"],
        "ineligible_receipt_count": receipt_audit["ineligible"],
        "attributed_ineligible_receipt_count": receipt_audit["attributed_ineligible"],
        "ineligibility_rate": receipt_audit["ineligibility_rate"],
        "leading_ineligibility_reason": receipt_audit["leading_ineligibility_reason"],
        "exploration_bands": receipt_audit["exploration_bands"],
        "latest_receipt_window": receipt_audit["latest_receipt_window"],
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "issues": issues,
    }


def backfill_scorecard(*, target: Path, json_output: bool = False) -> int:
    """Read-only audit of verify receipts for scorecard eligibility (#574)."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    from . import scorecard as scorecard_mod

    payload = scorecard_mod.backfill_scorecard_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"outcome backfill scorecard: {target}")
    print(
        "receipts: "
        f"total={payload['total_receipts']} "
        f"eligible={payload['eligible']} "
        f"unattributed={payload['unattributed']} "
        f"ineligible={payload['ineligible']} "
        f"ineligibility_rate={payload['ineligibility_rate']}"
    )
    bands = payload.get("exploration_bands") if isinstance(payload.get("exploration_bands"), dict) else {}
    if bands:
        print(
            "exploration_bands: "
            f"unseen={bands.get('unseen', 0)} "
            f"candidate={bands.get('candidate', 0)} "
            f"provisional={bands.get('provisional', 0)} "
            f"promoted={bands.get('promoted', 0)}"
        )
    if payload.get("leading_ineligibility_reason"):
        print(f"leading_ineligibility_reason: {payload['leading_ineligibility_reason']}")
    print(payload["legacy_records_note"])
    return 0
