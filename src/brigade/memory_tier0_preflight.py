"""Observational Tier 0 diagnostics. No report confers promotion authority."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import config as brigade_config
from . import memory_cmd, memory_proposal_io as io, memory_proposals as legacy
from . import memory_proposal_v2 as v2, memory_proposal_v2_ops as delta
from . import memory_reflect as reflect, memory_reflect_candidates as derive
from .memory_evidence_log import LOG_PATH, MAX_SCAN_BYTES
from .memory_proposal_v2_reflect import translation_payload

SCHEMA = "brigade.memory-tier0-preflight.v1"
POLICY_SCHEMA = "brigade.memory-tier0-preflight-policy.v1"
CLASSES = ("reinforce", "stale-newer-evidence", "duplicate")
DEFAULTS: dict[str, Any] = dict(
    schema=POLICY_SCHEMA,
    enabled_classes=[],
    minimum_evidence_ids=3,
    minimum_source_groups=2,
    max_changed_paths=1,
    cooldown_seconds=86400,
)
AUTHORITY = ("evidence_authority", "owner_authority", "retrieval_eval", "cooldown_history")


class Refusal(ValueError):
    """Only stable, non-input reason codes cross the public boundary."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _json(raw: bytes) -> Any:
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: _invalid())


def _invalid() -> Any:
    raise ValueError("invalid JSON constant")


def _safe_name(value: str) -> str:
    io.relative(value)
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ValueError("unsafe identifier")
    return value


class Snapshot:
    """Bound exact bytes and recheck them without changing modes or creating state."""

    def __init__(self, target: Path):
        self.target = target
        self.inputs: dict[Path, tuple[bytes | None, int, bool]] = {}

    def read(self, path: Path, limit: int, *, private: bool = False) -> bytes | None:
        raw = io.read_bytes(path, limit=limit, private=private, absent=True)
        if path in self.inputs and self.inputs[path][0] != raw:
            raise Refusal("input_drift")
        self.inputs[path] = raw, limit, private
        return raw

    def unchanged(self) -> bool:
        return all(
            io.read_bytes(path, limit=limit, private=private, absent=True) == raw
            for path, (raw, limit, private) in self.inputs.items()
        )


def _check(report: dict[str, Any], gate: str, code: str | None = None, *, state: str = "held") -> None:
    code = code or gate + "_satisfied"
    report["checks"].append(
        dict(
            gate=gate,
            state="satisfied" if code == gate + "_satisfied" else state,
            code=code,
            explanation=(
                "No authority contract binds this candidate and exact delta. Promotion remains held."
                if gate in AUTHORITY
                else code.replace("_", " ").capitalize() + "."
            ),
        )
    )


def _policy(snapshot: Snapshot, name: str | None) -> tuple[dict[str, Any], str | None]:
    if name is None:
        return dict(DEFAULTS), None
    try:
        raw = snapshot.read(snapshot.target / _safe_name(name), 16 * 1024)
        if raw is None:
            raise ValueError("missing policy")
        value = _json(raw)
        if not isinstance(value, dict) or set(value) - DEFAULTS.keys() or value.get("schema") != POLICY_SCHEMA:
            raise ValueError("invalid policy schema")
        policy = DEFAULTS | value
        classes = policy["enabled_classes"]
        if (
            not isinstance(classes, list)
            or len(classes) > 3
            or any(type(c) is not str or c not in CLASSES for c in classes)
            or len(set(classes)) != len(classes)
        ):
            raise ValueError("invalid classes")
        for key, low, high in (
            ("minimum_evidence_ids", 3, 64),
            ("minimum_source_groups", 2, 64),
            ("max_changed_paths", 1, 8),
            ("cooldown_seconds", 1, 31536000),
        ):
            if type(policy[key]) is not int or not low <= policy[key] <= high:
                raise ValueError("invalid policy limit")
        return policy, io.sha256(raw)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise Refusal("policy_invalid") from None


def _candidate(snapshot: Snapshot, candidate_id: str) -> tuple[dict[str, Any], str]:
    target = snapshot.target
    candidate = reflect.inspect_payload(target=target, command="show", candidate_id=candidate_id)
    allowed = {
        "schema",
        "kind",
        "card_ids",
        "card_paths",
        "base_sha256",
        "evidence_ids",
        "evidence_sources",
        "proof_count",
        "proof_sources",
        "review_evidence_ids",
        "contrary_ids",
        "unresolved_evidence_ids",
        "tier",
        "scope",
        "diff",
        "summary",
        "candidate_id",
        "outcome_bindings",
        "semantic_contradiction",
    }
    if set(candidate) - allowed or type(candidate.get("tier")) is not int:
        raise Refusal("candidate_invalid")
    for key in ("evidence_ids", "contrary_ids", "review_evidence_ids", "unresolved_evidence_ids", "card_ids"):
        values = candidate[key]
        if not isinstance(values, list) or len(values) > io.MAX_LIST or any(type(v) is not str for v in values):
            raise Refusal("candidate_invalid")
    for name in candidate["card_paths"].values():
        _safe_name(name)
    for run in reflect._runs(target)[0]:
        if run["status"] != "completed":
            continue
        if candidate in reflect._candidates(target, run):
            root = target / reflect.ROOT / run["run_id"]
            receipt = snapshot.read(root / "receipt.json", io.MAX_STATE_BYTES, private=True)
            rows = snapshot.read(root / "candidates.jsonl", io.MAX_STATE_BYTES, private=True)
            _json(receipt or b"")
            parsed = [_json(line) for line in (rows or b"").splitlines()]
            if candidate not in parsed:
                raise Refusal("input_drift")
            return candidate, run["run_id"]
    raise Refusal("candidate_invalid")


def _corpus(snapshot: Snapshot, care: Any) -> list[str]:
    paths = memory_cmd._iter_cards(snapshot.target, care)
    if len(paths) > io.MAX_FILES:
        raise Refusal("binding_incomplete")
    size = 0
    names = []
    for path in paths:
        names.append(_safe_name(path.relative_to(snapshot.target).as_posix()))
        raw = snapshot.read(path, io.MAX_CARD_BYTES)
        if raw is None:
            raise Refusal("input_drift")
        size += len(raw)
        if size > io.MAX_CORPUS_BYTES:
            raise Refusal("binding_incomplete")
    return sorted(names)


def _evidence(
    snapshot: Snapshot, candidate: dict[str, Any], sources: list[dict[str, Any]], ledger: Any
) -> dict[str, str]:
    refs = {derive._evidence_id(ref) for ref in candidate["evidence_ids"]}
    for source in sources:
        refs.update(derive._evidence_id(ref) for ref in memory_cmd._evidence_refs(source["metadata"]))
        rows = [row for key in (source["id"], *source["aliases"]) for row in ledger.by_identity.get(key, [])]
        newest = sorted(rows, key=lambda row: (derive._sort_timestamp(row["ts"]), row["evidence_id"]), reverse=True)
        refs.update(list(dict.fromkeys(derive._evidence_id(row["evidence_id"]) for row in newest))[: io.MAX_LIST])
    if len(refs) > io.MAX_LIST:
        raise Refusal("binding_incomplete")
    roots = derive._evidence_roots(snapshot.target)
    raw_inputs: dict[str, bytes] = {}
    total = 0
    # Check the aggregate before hashing any evidence or invoking composition.
    for ref in sorted(refs):
        bundle = memory_cmd._miseledger_evidence_id(ref)
        if bundle:
            path = memory_cmd._miseledger_evidence_path(bundle)
        else:
            _safe_name(ref)
            if not any(ref.startswith(root + "/") for root in roots):
                raise Refusal("binding_incomplete")
            path = snapshot.target / ref
        if path is None:
            raise Refusal("binding_incomplete")
        raw = snapshot.read(path, io.MAX_INPUT_BYTES)
        if raw is None:
            raise Refusal("binding_incomplete")
        total += len(raw)
        if total > io.MAX_CORPUS_BYTES:
            raise Refusal("binding_incomplete")
        raw_inputs[ref] = raw
    return {ref: io.sha256(raw) for ref, raw in raw_inputs.items()}


def _delta(
    report: dict[str, Any],
    snapshot: Snapshot,
    care: Any,
    binding: dict[str, Any],
    candidate: dict[str, Any],
    policy: dict[str, Any],
    sources: list[dict[str, Any]],
) -> None:
    if any(delta.anchor_text(s["text"], s["path"]) != s["text"] for s in sources):
        _check(report, "delta", "anchoring_requires_review")
    try:
        packet = translation_payload(target=snapshot.target, candidate_id=candidate["candidate_id"])
        if packet["run_id"] != report["run_id"] or packet["candidate"] != candidate:
            raise Refusal("input_drift")
        composed = v2.compose(
            target=snapshot.target,
            care=care,
            binding=binding,
            source_paths=packet["source_paths"],
            ops=packet["ops"],
            anchor_blocks=True,
            reason="Diagnostic evaluation only.",
            proposal_id="preflight-"
            + io.sha256(io.json_bytes([candidate["candidate_id"], report["policy_sha256"]]))[:24],
            reflect=dict(run_id=packet["run_id"], candidate_id=candidate["candidate_id"]),
            expected_source_sha256=packet["expected_source_sha256"],
            strict_ops=True,
        )
    except Refusal:
        raise
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        _check(report, "delta", "delta_invalid")
        return
    changes = composed["mutations"]
    report["changed_path_count"] = len({m["path"] for m in changes})
    report["delta_sha256"] = io.sha256(
        io.json_bytes(
            dict(
                ops=composed["ops"],
                mutations=[{k: m[k] for k in ("path", "before_sha256", "after_sha256")} for m in changes],
            )
        )
    )
    review = not changes or any(
        op["type"] not in ("append_evidence", "merge_exact_duplicates") for op in composed["ops"]
    )
    allowed = {"evidence", "proof_count", "last_consolidated"}
    for mutation in changes:
        before, after = mutation["before"], mutation["after"]
        if mutation["path"] not in candidate["card_paths"].values() or before is None or after is None:
            review = True
            continue
        before_meta, after_meta = legacy._meta(before), legacy._meta(after)
        if before[legacy._body_offset(before) :] != after[legacy._body_offset(after) :] or {
            k: v for k, v in before_meta.items() if k not in allowed
        } != {k: v for k, v in after_meta.items() if k not in allowed}:
            review = True
    if review:
        _check(report, "delta", "delta_requires_review")
    if report["changed_path_count"] > policy["max_changed_paths"]:
        _check(report, "delta", "blast_cap_exceeded")
    if not any(c["gate"] == "delta" for c in report["checks"]):
        _check(report, "delta")


def preflight_payload(*, target: Path, candidate_id: str, policy_path: str | None = None) -> dict[str, Any]:
    """Return sanitized holds or refusal. This API performs no writes of any kind."""
    report: dict[str, Any] = dict(
        schema=SCHEMA,
        status="held",
        eligible=False,
        diagnostic_only=True,
        candidate_id=None,
        run_id=None,
        policy_sha256=None,
        candidate_sha256=None,
        source_sha256={},
        evidence_sha256={},
        config_digests={},
        workspace_fingerprint=None,
        evidence_log_sha256="incomplete",
        delta_sha256=None,
        changed_path_count=0,
        evaluation_sha256=None,
        checks=[],
    )
    stage = "candidate_invalid"
    try:
        with io.directory(target):
            pass
        target = target.absolute()
        snapshot = Snapshot(target)
        policy, report["policy_sha256"] = _policy(snapshot, policy_path)
        if not isinstance(candidate_id, str) or not re.fullmatch(r"candidate-[0-9a-f]{24}", candidate_id):
            raise Refusal("candidate_invalid")
        report["candidate_id"] = candidate_id
        candidate, report["run_id"] = _candidate(snapshot, candidate_id)
        report["candidate_sha256"] = io.sha256(io.json_bytes(candidate))
        if policy_path is None:
            _check(report, "policy", "policy_missing")
        elif candidate["kind"] not in policy["enabled_classes"]:
            _check(report, "policy", "class_disabled")
        else:
            _check(report, "policy")
        if candidate["tier"] != 0 or candidate["kind"] not in CLASSES:
            _check(report, "candidate", "tier_requires_review")
        elif any(candidate[key] for key in ("contrary_ids", "review_evidence_ids", "unresolved_evidence_ids")):
            _check(report, "candidate", "candidate_evidence_requires_review")
        else:
            _check(report, "candidate")
        stage = "binding_incomplete"
        for name in (
            brigade_config.CONFIG_REL_PATH,
            *[f"{d}/config.json" for d in brigade_config.LEGACY_WORKSPACE_DIRNAMES],
            memory_cmd.CONFIG_REL_PATH,
            ".brigade/handoff-sources.json",
        ):
            raw = snapshot.read(target / name, io.MAX_INPUT_BYTES)
            if name.endswith(".json") and raw is not None:
                _json(raw)
        care, binding = legacy._configuration(target)
        report["config_digests"] = binding["config_digests"]
        report["workspace_fingerprint"] = binding["workspace_fingerprint"]
        names = _corpus(snapshot, care)
        try:
            raw_log = snapshot.read(target / LOG_PATH, MAX_SCAN_BYTES)
            for line in (raw_log or b"").splitlines():
                _json(line)
        except (ValueError, UnicodeError, RecursionError):
            raise Refusal("evidence_window_incomplete") from None
        report["evidence_log_sha256"] = io.sha256(raw_log) if raw_log is not None else "absent"
        ledger = reflect._evidence_log(target)
        if (
            ledger.summary["scan_window_truncated"]
            or ledger.summary["invalid_warning_count"]
            or ledger.summary["unsupported"]
        ):
            raise Refusal("evidence_window_incomplete")
        stage = "card_trust_denied"
        sources, _ = v2._sources(target, care, list(candidate["card_paths"].values()))
        stage = "binding_incomplete"
        report["source_sha256"] = {s["path"]: s["sha256"] for s in sources}
        drift = any(candidate["base_sha256"].get(s["id"]) != s["sha256"] for s in sources)
        _check(report, "snapshot", "base_drift" if drift else None)
        report["evidence_sha256"] = _evidence(snapshot, candidate, sources, ledger)
        if not drift:
            _delta(report, snapshot, care, binding, candidate, policy, sources)
        else:
            _check(report, "delta", "delta_invalid", state="not_evaluated")
        groups = derive._resolved(target, candidate["evidence_ids"], derive._evidence_roots(target))
        count = len(set(candidate["evidence_ids"]) & report["evidence_sha256"].keys())
        _check(report, "evidence_floor", "evidence_floor_unmet" if count < policy["minimum_evidence_ids"] else None)
        _check(
            report,
            "source_group_floor",
            "source_group_floor_unmet" if len(set(groups.values())) < policy["minimum_source_groups"] else None,
        )
        _check(
            report,
            "host",
            "path_read_only"
            if any(memory_cmd.read_only_pattern(s["path"], care.read_only_paths) for s in sources)
            else None,
        )
        complete = all("provenance" in s["metadata"] or "metadata" in s["metadata"] for s in sources)
        _check(report, "card_trust", None if complete else "card_trust_unverifiable", state="unverifiable")
        if (
            _candidate(snapshot, candidate_id) != (candidate, report["run_id"])
            or not snapshot.unchanged()
            or names != sorted(p.relative_to(target).as_posix() for p in memory_cmd._iter_cards(target, care))
            or binding != legacy._configuration(target)[1]
        ):
            raise Refusal("input_drift")
        if report["delta_sha256"] is not None:
            report["evaluation_sha256"] = io.sha256(
                io.json_bytes(
                    {
                        "schema": SCHEMA,
                        "policy": report["policy_sha256"] or "absent",
                        "candidate": report["candidate_sha256"],
                        "sources": report["source_sha256"],
                        "evidence": report["evidence_sha256"],
                        "config": binding,
                        "ledger": report["evidence_log_sha256"],
                        "delta": report["delta_sha256"],
                        "inputs": {
                            str(path): io.sha256(raw) if raw is not None else "absent"
                            for path, (raw, _, _) in snapshot.inputs.items()
                        },
                    }
                )
            )
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
        code = str(exc) if isinstance(exc, Refusal) else stage
        report["status"] = "refused"
        report["evaluation_sha256"] = None
        for check in report["checks"]:
            if check["state"] == "satisfied":
                check["state"] = "not_evaluated"
        gate = (
            "policy"
            if code == "policy_invalid"
            else "snapshot"
            if code in ("input_drift", "binding_incomplete", "evidence_window_incomplete")
            else "card_trust"
            if code == "card_trust_denied"
            else "candidate"
        )
        _check(report, gate, code)
    if report["candidate_sha256"] is not None:
        for gate in AUTHORITY:
            _check(report, gate, gate + "_unbound", state="unverifiable")
    order = {
        name: index
        for index, name in enumerate(
            (
                "policy",
                "candidate",
                "snapshot",
                "delta",
                "evidence_floor",
                "source_group_floor",
                "host",
                "card_trust",
                *AUTHORITY,
            )
        )
    }
    report["checks"].sort(key=lambda check: order[check["gate"]])
    return report
