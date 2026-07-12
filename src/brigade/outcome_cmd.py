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
import sys
from pathlib import Path
from typing import Any

from . import localio, outcome as core


def _records_path(target: Path) -> Path:
    return target / "memory" / "outcome" / "records.jsonl"


def _status_path(target: Path) -> Path:
    return target / "memory" / "outcome" / "status.json"


def _decision_path(target: Path, now, artifact_id: str) -> Path:
    stamp = now.strftime("%Y%m%d-%H%M%S")
    slug = localio.slugify(artifact_id, fallback="artifact")
    return target / "memory" / "outcome" / "decisions" / f"{stamp}-{slug}.json"


def load_status(target: Path) -> dict[str, dict]:
    payload = localio.read_json_dict(_status_path(target)) or {}
    artifacts = payload.get("artifacts")
    return artifacts if isinstance(artifacts, dict) else {}


def _known_skill_names(target: Path) -> list[str]:
    """Skill ids the target actually has: wired into a harness or in the registry."""
    names: set[str] = set()
    for skill_md in target.glob(".*/skills/*/SKILL.md"):
        names.add(skill_md.parent.name)
    registry = target / ".brigade" / "skills" / "registry"
    if registry.is_dir():
        for child in registry.iterdir():
            if child.is_dir():
                names.add(child.name)
    return sorted(names)


def _known_card_names(target: Path) -> list[str]:
    cards = target / "memory" / "cards"
    if not cards.is_dir():
        return []
    return sorted(p.stem for p in cards.glob("*.md"))


def _artifact_known(target: Path, artifact_id: str, kind: str) -> bool:
    if kind == "card":
        return artifact_id in _known_card_names(target)
    return artifact_id in _known_skill_names(target)


def _artifact_content_path(target: Path, artifact_id: str, kind: str) -> Path | None:
    """Locate the artifact text a fingerprint should pin.

    Harness-installed copy first: a verified run exercises the installed skill,
    not the registry master, so when the two drift the installed text is what
    the signal is evidence about. The registry copy is the fallback for skills
    known but not yet installed. capture and rank/explain both resolve through
    here, so "current cohort" always means the text that actually runs.
    """
    if kind == "card":
        card = target / "memory" / "cards" / f"{artifact_id}.md"
        return card if card.is_file() else None
    for skill_md in sorted(target.glob(f".*/skills/{artifact_id}/SKILL.md")):
        if skill_md.is_file():
            return skill_md
    from . import skills_cmd

    registry_md = skills_cmd._skill_md_path(skills_cmd._skill_path(target, artifact_id))
    if registry_md.is_file():
        return registry_md
    return None


# Files inside a skill bundle that are not part of its logic: OS cruft and the
# install-time metadata sidecar (which changes on every install, not on edit).
_BUNDLE_IGNORED_NAMES = frozenset({".DS_Store", "skill.json"})


def _bundle_fingerprint(skill_dir: Path) -> str | None:
    """sha256 over a skill's whole bundle, reducing to sha256(SKILL.md) for a lone file.

    CocoIndex's logic_tracking walks the fingerprint through nested calls so
    editing a helper invalidates its callers. A skill is a directory, not just
    SKILL.md, so a bundled helper is that skill's "helper": hashing only SKILL.md
    leaves a signal vouching for a bundle whose script has since changed. This
    folds every bundle file (path + content) into the fingerprint.

    A skill whose only content file is SKILL.md hashes to *exactly*
    ``sha256(SKILL.md)`` - byte-identical to the pre-bundle fingerprint - so
    existing single-file records are never invalidated. Only a genuinely
    multi-file bundle takes the composite path.
    """
    files = sorted(p for p in skill_dir.rglob("*") if p.is_file() and p.name not in _BUNDLE_IGNORED_NAMES)
    if not files:
        return None
    skill_md = skill_dir / "SKILL.md"
    try:
        if files == [skill_md]:
            return hashlib.sha256(skill_md.read_bytes()).hexdigest()
        digest = hashlib.sha256()
        for path in files:
            rel = path.relative_to(skill_dir).as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
            digest.update(b"\0")
        return digest.hexdigest()
    except OSError:
        return None


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
    """
    from brigade.memory_doctor.parsing import extract_wiki_links

    stem_by_lower = {p.stem.lower(): p.stem for p in cards_dir.glob("*.md")}
    visited = {root_id.lower()}
    queue = [root_id]
    reached: set[str] = set()
    while queue:
        current = queue.pop()
        card_path = cards_dir / f"{current}.md"
        if not card_path.is_file():
            continue
        for raw in extract_wiki_links(card_path.read_text(errors="replace")):
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
    try:
        self_bytes = card_path.read_bytes()
        closure = _linked_card_closure(cards_dir, card_path.stem)
        if not closure:
            return hashlib.sha256(self_bytes).hexdigest()
        digest = hashlib.sha256()
        digest.update(hashlib.sha256(self_bytes).hexdigest().encode("ascii"))
        digest.update(b"\0")
        for stem in closure:
            digest.update(stem.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256((cards_dir / f"{stem}.md").read_bytes()).hexdigest().encode("ascii"))
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
    return _bundle_fingerprint(path.parent)


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
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_records(target: Path) -> list[core.OutcomeRecord]:
    rows = localio.read_jsonl_dicts(_records_path(target))
    records = [_record_from_dict(row) for row in rows]
    return [record for record in records if record is not None]


def _last_record_digest(path: Path) -> str | None:
    for row in reversed(localio.read_jsonl_dicts(path)):
        digest = row.get("digest")
        if isinstance(digest, str) and digest:
            return digest
    return None


def _record_payload(record: core.OutcomeRecord) -> dict:
    row = dataclasses.asdict(record)
    if row.get("code_graph_delta") is None:
        row.pop("code_graph_delta", None)
    if row.get("context_eval") is None:
        row.pop("context_eval", None)
    if row.get("content_fingerprint") is None:
        row.pop("content_fingerprint", None)
    return row


def append_records(target: Path, records: list[core.OutcomeRecord]) -> None:
    path = _records_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    prev_digest = _last_record_digest(path)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            row = _record_payload(record)
            row["prev_digest"] = prev_digest
            row["digest"] = localio.canonical_json_digest(row, exclude_keys={"digest"})
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            prev_digest = row["digest"]


def _compact_code_graph_delta(receipt: dict) -> dict | None:
    delta = receipt.get("code_graph_delta")
    if not isinstance(delta, dict):
        return None
    compact = {
        key: delta[key]
        for key in ("status", "summary", "changed_symbol_count", "edge_churn", "raw_counts")
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
    matches = [(payload, run_json) for child, payload, run_json in runs if child.name.startswith(run_id)]
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
        if artifact_id and new_status and created_at:
            transitions.append(core.StatusTransition(str(artifact_id), str(new_status), str(created_at)))
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
    """Project what the ratchet would decide over the current signal log under a config.

    A read-only fork: it replays ``records.jsonl`` through the scorer and the
    ratchet from a clean baseline under the given config, writing the resulting
    per-artifact projection to ``out``. It never reads or writes the live
    status.json, so two forks under different configs can be compared with
    ``outcome diff`` to see how a rule change would move promotions.

    Uses the same current-fingerprint cohort score as ``reconcile``, so a fork
    projection previews the ratchet the live command would actually run.
    """
    target = target.expanduser().resolve()
    config = config or core.ReconcileConfig()
    cohorts_by_artifact = _fingerprint_cohorts_by_artifact(target, load_records(target))
    scores = {artifact_id: cohorts.current for artifact_id, cohorts in cohorts_by_artifact.items()}
    decisions = core.project_statuses(scores, config=config, now=localio.utc_now())
    artifacts = {
        artifact_id: {
            "action": decision.action,
            "new_status": decision.new_status,
            "reason": decision.reason,
            "score": scores[artifact_id].score,
            "helped": scores[artifact_id].helped,
            "hurt": scores[artifact_id].hurt,
        }
        for artifact_id, decision in sorted(decisions.items())
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


def score(*, target: Path, artifact_id: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    scores = _scores_by_artifact(load_records(target))
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
    records = [record for record in load_records(target) if record.artifact_id == artifact_id]
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
    if json_output:
        payload = {
            "target": str(target),
            "artifact_id": artifact_id,
            "content_fingerprint": cohorts.current_fingerprint,
            "score": dataclasses.asdict(score_obj),
            "lifetime_score": dataclasses.asdict(cohorts.lifetime),
            "stale_records": cohorts.stale_records,
            "legacy_records": cohorts.legacy_records,
            "trail": trail,
        }
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
    if not trail:
        print("trail: none")
        return 0
    for item in trail:
        tag = f" [{item['cohort']}]" if cohorts.pinned else ""
        print(f"- {item['ts']} {item['source']} {item['signal_value']:+d} ({item['evidence_ref']}){tag}")
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
    if not _artifact_known(target, artifact_id, artifact_kind):
        known = _known_skill_names(target) if artifact_kind == "skill" else _known_card_names(target)
        hint = ", ".join(known) if known else "none"
        print(
            f"warning: '{artifact_id}' is not a known installed {artifact_kind}; recording anyway. "
            f"Capture against a real {artifact_kind} id (or `brigade-work` itself) to keep ranking "
            f"trustworthy. known {artifact_kind}s: {hint}",
            file=sys.stderr,
        )
    source = "verify"
    effective_status = ""
    evidence_ref = ""
    ts = localio.utc_now_iso()
    code_graph_delta: dict[str, Any] | None = None
    context_eval: dict[str, Any] | None = None
    if run_receipt is not None:
        receipt, run_json, error = _resolve_run_receipt(target, run_receipt)
        if receipt is None or run_json is None:
            print(f"error: {error}", file=sys.stderr)
            return 1
        source = "run"
        effective_status = _run_receipt_signal_status(receipt)
        evidence_ref = str(run_json)
        ts = str(receipt.get("completed_at") or receipt.get("started_at") or localio.utc_now_iso())
        code_graph_delta = _compact_code_graph_delta(receipt)
        context_eval = _compact_context_eval(receipt)
    else:
        from .work_cmd import verification as verify_mod

        receipt, error = verify_mod._resolve_verify_receipt(target, run_id or "latest")
        if receipt is None:
            print(f"error: {error}", file=sys.stderr)
            return 1
        effective_status = str(receipt.get("status") or "")
        evidence_ref = str(Path(str(receipt.get("path", ""))) / "receipt.json")
        ts = str(receipt.get("completed_at") or receipt.get("started_at") or localio.utc_now_iso())
        code_graph_delta = _compact_code_graph_delta(receipt)
    record = core.OutcomeRecord(
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        task_id=task_id or "",
        source=source,
        signal_value=core.signal_value(source, effective_status),
        evidence_ref=evidence_ref,
        ts=ts,
        code_graph_delta=code_graph_delta,
        context_eval=context_eval,
        content_fingerprint=artifact_fingerprint(target, artifact_id, artifact_kind),
    )
    append_records(target, [record])
    if json_output:
        print(json.dumps({"target": str(target), "record": _record_payload(record)}, indent=2, sort_keys=True))
        return 0
    print(f"outcome capture: {artifact_id}")
    print(f"source: {source} [{effective_status}] signal={record.signal_value:+d}")
    print(f"evidence: {record.evidence_ref}")
    if record.content_fingerprint:
        print(f"fingerprint: {record.content_fingerprint[:12]}")
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
            if not skills_cmd._skill_path(target, artifact_id).is_dir():
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

    Fingerprint-aware: the promote/rollback decision scores the CURRENT-fingerprint
    cohort, not the lifetime ledger, so an edited skill must re-earn its promotion
    against the text that now ships instead of coasting on signals for text that no
    longer exists. Grandfathering keeps this non-disruptive: a never-edited artifact
    has no proven-stale records, so ``current`` equals lifetime and the decision is
    byte-identical to the pre-fingerprint ratchet. The decision RULES (thresholds,
    cooldown, forward-only ratchet) are untouched; only the score fed in narrows.
    """
    target = target.expanduser().resolve()
    config = config or core.ReconcileConfig()
    records = load_records(target)
    cohorts_by_artifact = _fingerprint_cohorts_by_artifact(target, records)
    graph_counts = _graph_delta_counts_by_artifact(records)
    brief_stats = _brief_hit_stats_by_artifact(records)
    kinds: dict[str, str] = {}
    for record in records:
        kinds.setdefault(record.artifact_id, record.artifact_kind or "skill")
    status_map = load_status(target)
    now = localio.utc_now()

    results: list[tuple[core.Decision, core.FingerprintCohorts, str]] = []
    for artifact_id, cohorts in sorted(cohorts_by_artifact.items()):
        entry = status_map.get(artifact_id) or {}
        prior_status = entry.get("status", "candidate")
        last_action_ts = localio.parse_iso_datetime(entry.get("last_action_ts"))
        decision = core.decide(
            cohorts.current,
            current_status=prior_status,
            last_action_ts=last_action_ts,
            now=now,
            config=config,
        )
        if decision.action != "hold":
            results.append((decision, cohorts, prior_status))

    applied: list[str] = []
    executions: dict[str, str] = {}
    effective_status: dict[str, str] = {}
    if apply and results:
        for decision, cohorts, prior_status in results:
            score_obj = cohorts.current
            execution = "noop"
            if decision.action in ("install", "rollback"):
                if kinds.get(decision.artifact_id, "skill") == "skill":
                    execution = _execute_skill_decision(target, decision.artifact_id, decision.action)
                else:
                    execution = "skipped: card execution is v1.1"
            executions[decision.artifact_id] = execution
            # An install that did not physically install must not advance status to
            # 'promoted'. The forward-only ratchet never re-emits install for a
            # 'promoted' artifact, so a false promotion would permanently hide the
            # failure. Keep it a 'candidate' (stamp last_action_ts for cooldown) so a
            # later accept + reconcile retries. Cards are exempt: their promotion is
            # status-only (physical card execution is v1.1), so a card never "fails".
            install_failed = (
                decision.action == "install"
                and kinds.get(decision.artifact_id, "skill") == "skill"
                and execution != "installed"
            )
            new_status = prior_status if install_failed else decision.new_status
            effective_status[decision.artifact_id] = new_status
            receipt = {
                "artifact_id": decision.artifact_id,
                "action": decision.action,
                "prior_status": prior_status,
                "new_status": new_status,
                "decided_status": decision.new_status,
                "reason": decision.reason,
                "score": dataclasses.asdict(score_obj),
                "execution": execution,
                "created_at": now.isoformat(),
            }
            receipt.update(_fingerprint_decision_fields(cohorts))
            localio.write_json(_decision_path(target, now, decision.artifact_id), receipt)
            status_map[decision.artifact_id] = {"status": new_status, "last_action_ts": now.isoformat()}
            if not install_failed:
                applied.append(decision.artifact_id)
        localio.write_json(_status_path(target), {"version": 1, "artifacts": status_map})

    decisions_payload = []
    for decision, cohorts, prior_status in results:
        item = {
            "artifact_id": decision.artifact_id,
            "action": decision.action,
            "prior_status": prior_status,
            "new_status": effective_status.get(decision.artifact_id, decision.new_status),
            "decided_status": decision.new_status,
            "reason": decision.reason,
            "score": cohorts.current.score,
            "execution": executions.get(decision.artifact_id, "dry-run"),
        }
        item.update(_fingerprint_decision_fields(cohorts))
        counts = graph_counts.get(decision.artifact_id)
        if counts is not None:
            item.update(counts)
        stats = brief_stats.get(decision.artifact_id)
        if stats is not None:
            item.update(stats)
        decisions_payload.append(item)
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
    if not results:
        print("decisions: none")
        return 0
    for decision, cohorts, prior_status in results:
        shown_status = effective_status.get(decision.artifact_id, decision.new_status)
        # On --apply, surface the physical execution so the output is never
        # byte-identical to a dry-run that did nothing.
        tail = f" -> {executions[decision.artifact_id]}" if apply and decision.artifact_id in executions else ""
        fingerprint_tail = _fingerprint_decision_suffix(cohorts)
        graph_tail = _graph_delta_human_suffix(graph_counts.get(decision.artifact_id))
        brief_tail = _brief_hit_human_suffix(brief_stats.get(decision.artifact_id))
        print(
            f"- {decision.artifact_id} {prior_status} -> {shown_status} "
            f"[{decision.action}] {decision.reason}{fingerprint_tail}{graph_tail}{brief_tail}{tail}"
        )
    return 0


def rank(*, target: Path, json_output: bool = False) -> int:
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
    """
    target = target.expanduser().resolve()
    records = load_records(target)
    cohorts_by_artifact = _fingerprint_cohorts_by_artifact(target, records)
    graph_counts = _graph_delta_counts_by_artifact(records)
    brief_stats = _brief_hit_stats_by_artifact(records)

    def blended(cohorts: core.FingerprintCohorts) -> float:
        return core.rank_score(confidence=0.0, outcome=cohorts.current.score, keyword=0.0)

    def sort_key(item: tuple[str, core.FingerprintCohorts]) -> tuple:
        artifact_id, cohorts = item
        stats = brief_stats.get(artifact_id)
        # Missing brief samples sort after measured ones at the same Wilson score.
        hit = float(stats["brief_hit_rate"]) if stats is not None else -1.0
        return (-blended(cohorts), -hit, artifact_id)

    ordered = sorted(cohorts_by_artifact.items(), key=sort_key)
    ranking_payload = []
    for artifact_id, cohorts in ordered:
        current = cohorts.current
        lifetime = cohorts.lifetime
        entry = {
            "artifact_id": artifact_id,
            "score": current.score,
            "rank_score": blended(cohorts),
            "helped": current.helped,
            "hurt": current.hurt,
            "content_fingerprint": cohorts.current_fingerprint,
            "lifetime_score": lifetime.score,
            "lifetime_helped": lifetime.helped,
            "lifetime_hurt": lifetime.hurt,
            "stale_records": cohorts.stale_records,
            "legacy_records": cohorts.legacy_records,
        }
        counts = graph_counts.get(artifact_id)
        if counts is not None:
            entry.update(counts)
        stats = brief_stats.get(artifact_id)
        if stats is not None:
            entry.update(stats)
        ranking_payload.append(entry)
    payload = {
        "target": str(target),
        "ranking": ranking_payload,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"outcome rank: {target}")
    if not ordered:
        print("ranking: none")
        return 0
    for artifact_id, cohorts in ordered:
        current = cohorts.current
        graph_tail = _graph_delta_human_suffix(graph_counts.get(artifact_id))
        brief_tail = _brief_hit_human_suffix(brief_stats.get(artifact_id))
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
            f"hurt={current.hurt}{fingerprint_tail}{graph_tail}{brief_tail}"
        )
    return 0


def doctor(*, target: Path, json_output: bool = False) -> int:
    from . import receipts_cmd

    return receipts_cmd.doctor(target=target, json_output=json_output)


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
    new_record = core.OutcomeRecord(
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        task_id=task_id or "",
        source=source,
        signal_value=core.signal_value(source, status),
        evidence_ref=evidence_ref,
        ts=localio.utc_now_iso(),
        content_fingerprint=artifact_fingerprint(target, artifact_id, artifact_kind),
    )
    append_records(target, [new_record])
    if json_output:
        print(json.dumps({"target": str(target), "record": _record_payload(new_record)}, indent=2, sort_keys=True))
        return 0
    print(f"outcome record: {artifact_id}")
    print(f"source: {source} [{status}] signal={new_record.signal_value:+d}")
    return 0


def health(target: Path) -> dict:
    """Surface whether the verified-learning loop is actually being fed.

    The loop is invisible in ``brigade work brief`` otherwise: an adopter cannot
    tell that verify runs are piling up while the outcome ledger stays empty
    (loop half-fed) or that neither exists yet (loop dormant).
    """
    target = target.expanduser().resolve()
    from .work_cmd import helpers as work_helpers

    records = load_records(target)
    scores = _scores_by_artifact(records)
    runs_root = work_helpers._verify_runs_root(target)
    verify_run_count = sum(1 for child in runs_root.iterdir() if child.is_dir()) if runs_root.is_dir() else 0
    record_count = len(records)
    promoted_count = sum(1 for entry in load_status(target).values() if entry.get("status") == "promoted")

    issues: list[dict] = []
    if verify_run_count > 0 and record_count == 0:
        issues.append(
            {
                "status": "warn",
                "name": "outcome_loop_half_fed",
                "detail": (
                    f"{verify_run_count} verify run(s) but 0 outcome record(s); "
                    "run `brigade outcome capture <skill>` (or `verify run --capture <skill>`) after verifying"
                ),
            }
        )
    elif verify_run_count == 0 and record_count == 0:
        issues.append(
            {
                "status": "warn",
                "name": "outcome_loop_dormant",
                "detail": "no verify runs or outcome records yet; the verified-learning loop is not running",
            }
        )
    return {
        "records_path": str(_records_path(target)),
        "verify_run_count": verify_run_count,
        "record_count": record_count,
        "scored_artifact_count": len(scores),
        "promoted_count": promoted_count,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "issues": issues,
    }
