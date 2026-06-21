"""Outcome ledger persistence and read-side CLI (score, explain).

The ledger lives under ``memory/outcome/`` so it is git-tracked and portable
(readable without Brigade, movable across harnesses), unlike the gitignored
``.brigade/`` correlation buffers. Scores are derived from the records on every
read, so the audit trail and the score can never drift apart.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import sys
from pathlib import Path

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


def _record_from_dict(payload: dict) -> core.OutcomeRecord | None:
    try:
        return core.OutcomeRecord(
            artifact_id=str(payload["artifact_id"]),
            artifact_kind=str(payload.get("artifact_kind", "")),
            task_id=str(payload.get("task_id", "")),
            source=str(payload.get("source", "")),
            signal_value=int(payload.get("signal_value", 0)),
            evidence_ref=str(payload.get("evidence_ref", "")),
            ts=str(payload.get("ts", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_records(target: Path) -> list[core.OutcomeRecord]:
    rows = localio.read_jsonl_dicts(_records_path(target))
    records = [_record_from_dict(row) for row in rows]
    return [record for record in records if record is not None]


def append_records(target: Path, records: list[core.OutcomeRecord]) -> None:
    path = _records_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(dataclasses.asdict(record), sort_keys=True) + "\n")


def _scores_by_artifact(records: list[core.OutcomeRecord]) -> dict[str, core.OutcomeScore]:
    grouped: dict[str, list[core.OutcomeRecord]] = {}
    for record in records:
        grouped.setdefault(record.artifact_id, []).append(record)
    return {artifact_id: core.score_records(artifact_id, recs) for artifact_id, recs in grouped.items()}


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
    target = target.expanduser().resolve()
    records = [record for record in load_records(target) if record.artifact_id == artifact_id]
    score_obj = core.score_records(artifact_id, records)
    trail = [
        {
            "ts": record.ts,
            "source": record.source,
            "signal_value": record.signal_value,
            "evidence_ref": record.evidence_ref,
            "task_id": record.task_id,
        }
        for record in sorted(records, key=lambda record: record.ts)
    ]
    if json_output:
        payload = {
            "target": str(target),
            "artifact_id": artifact_id,
            "score": dataclasses.asdict(score_obj),
            "trail": trail,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"outcome explain: {artifact_id}")
    print(f"score: {score_obj.score:.3f} helped={score_obj.helped} hurt={score_obj.hurt} neutral={score_obj.neutral}")
    if not trail:
        print("trail: none")
        return 0
    for item in trail:
        print(f"- {item['ts']} {item['source']} {item['signal_value']:+d} ({item['evidence_ref']})")
    return 0


def capture(
    *,
    target: Path,
    artifact_id: str,
    artifact_kind: str = "skill",
    task_id: str | None = None,
    run_id: str = "latest",
    json_output: bool = False,
) -> int:
    """Correlate a verify run's exit-code outcome into a signed record.

    The signal is the run's status (a real exit code the model cannot author),
    not an LLM judgment. The caller names which artifact the run exercised.
    """
    target = target.expanduser().resolve()
    from .work_cmd import verification as verify_mod

    receipt, error = verify_mod._resolve_verify_receipt(target, run_id)
    if receipt is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    status = str(receipt.get("status") or "")
    record = core.OutcomeRecord(
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        task_id=task_id or "",
        source="verify",
        signal_value=core.signal_value("verify", status),
        evidence_ref=str(Path(str(receipt.get("path", ""))) / "receipt.json"),
        ts=str(receipt.get("completed_at") or receipt.get("started_at") or localio.utc_now_iso()),
    )
    append_records(target, [record])
    if json_output:
        print(json.dumps({"target": str(target), "record": dataclasses.asdict(record)}, indent=2, sort_keys=True))
        return 0
    print(f"outcome capture: {artifact_id}")
    print(f"source: verify [{status}] signal={record.signal_value:+d}")
    print(f"evidence: {record.evidence_ref}")
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
    """
    target = target.expanduser().resolve()
    config = config or core.ReconcileConfig()
    records = load_records(target)
    scores = _scores_by_artifact(records)
    kinds: dict[str, str] = {}
    for record in records:
        kinds.setdefault(record.artifact_id, record.artifact_kind or "skill")
    status_map = load_status(target)
    now = localio.utc_now()

    results: list[tuple[core.Decision, core.OutcomeScore, str]] = []
    for artifact_id, score_obj in sorted(scores.items()):
        entry = status_map.get(artifact_id) or {}
        prior_status = entry.get("status", "candidate")
        last_action_ts = localio.parse_iso_datetime(entry.get("last_action_ts"))
        decision = core.decide(
            score_obj,
            current_status=prior_status,
            last_action_ts=last_action_ts,
            now=now,
            config=config,
        )
        if decision.action != "hold":
            results.append((decision, score_obj, prior_status))

    applied: list[str] = []
    executions: dict[str, str] = {}
    if apply and results:
        for decision, score_obj, prior_status in results:
            execution = "noop"
            if decision.action in ("install", "rollback"):
                if kinds.get(decision.artifact_id, "skill") == "skill":
                    execution = _execute_skill_decision(target, decision.artifact_id, decision.action)
                else:
                    execution = "skipped: card execution is v1.1"
            executions[decision.artifact_id] = execution
            receipt = {
                "artifact_id": decision.artifact_id,
                "action": decision.action,
                "prior_status": prior_status,
                "new_status": decision.new_status,
                "reason": decision.reason,
                "score": dataclasses.asdict(score_obj),
                "execution": execution,
                "created_at": now.isoformat(),
            }
            localio.write_json(_decision_path(target, now, decision.artifact_id), receipt)
            status_map[decision.artifact_id] = {"status": decision.new_status, "last_action_ts": now.isoformat()}
            applied.append(decision.artifact_id)
        localio.write_json(_status_path(target), {"version": 1, "artifacts": status_map})

    payload = {
        "target": str(target),
        "apply": apply,
        "decisions": [
            {
                "artifact_id": decision.artifact_id,
                "action": decision.action,
                "prior_status": prior_status,
                "new_status": decision.new_status,
                "reason": decision.reason,
                "score": score_obj.score,
                "execution": executions.get(decision.artifact_id, "dry-run"),
            }
            for decision, score_obj, prior_status in results
        ],
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
    for decision, _score_obj, prior_status in results:
        print(f"- {decision.artifact_id} {prior_status} -> {decision.new_status} [{decision.action}] {decision.reason}")
    return 0


def rank(*, target: Path, json_output: bool = False) -> int:
    """Rank learned artifacts by verified outcome, most-proven first.

    The blended retrieval score (rank_score) leaves room for confidence and
    keyword inputs that the live retrieval path supplies; on its own it orders
    by what a real signal has confirmed.
    """
    target = target.expanduser().resolve()
    scores = _scores_by_artifact(load_records(target))

    def blended(item: core.OutcomeScore) -> float:
        return core.rank_score(confidence=0.0, outcome=item.score, keyword=0.0)

    ordered = sorted(scores.values(), key=lambda item: (-blended(item), item.artifact_id))
    payload = {
        "target": str(target),
        "ranking": [
            {
                "artifact_id": item.artifact_id,
                "score": item.score,
                "rank_score": blended(item),
                "helped": item.helped,
                "hurt": item.hurt,
            }
            for item in ordered
        ],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"outcome rank: {target}")
    if not ordered:
        print("ranking: none")
        return 0
    for item in ordered:
        print(f"- {item.artifact_id} score={item.score:.3f} helped={item.helped} hurt={item.hurt}")
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
    new_record = core.OutcomeRecord(
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        task_id=task_id or "",
        source=source,
        signal_value=core.signal_value(source, status),
        evidence_ref=evidence_ref,
        ts=localio.utc_now_iso(),
    )
    append_records(target, [new_record])
    if json_output:
        print(json.dumps({"target": str(target), "record": dataclasses.asdict(new_record)}, indent=2, sort_keys=True))
        return 0
    print(f"outcome record: {artifact_id}")
    print(f"source: {source} [{status}] signal={new_record.signal_value:+d}")
    return 0
