"""Privacy-safe projections of Brigade run receipts into telemetry conventions."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import causal_receipt, localio, outcome
from .outcome_cmd import _fingerprint_cohorts_by_artifact, _record_from_dict
from .selection import KNOWN_HARNESSES

OTEL_MAPPING = {
    "name": "otel-genai",
    "mapping_version": 1,
    "upstream_revision": "opentelemetry-semconv-1.43.0",
}
OPENINFERENCE_MAPPING = {
    "name": "openinference",
    "mapping_version": 1,
    "upstream_revision": "audited-2026-07-12",
}
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT_HEX = re.compile(r"^[0-9a-f]{16}$")
_OUTCOME_SCALAR_KEYS = ("artifact_id", "artifact_kind", "task_id", "source", "evidence_ref", "ts")


def _object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _ids(run_id: str, seat: str, ordinal: int) -> tuple[str, str]:
    trace = hashlib.sha256(f"brigade:{run_id}".encode()).hexdigest()[:32]
    span = hashlib.sha256(f"brigade:{run_id}:{seat}:{ordinal}".encode()).hexdigest()[:16]
    return trace, span


def _receipt_trace_id(receipt_id: str) -> str:
    return hashlib.sha256(f"brigade:receipt:{receipt_id}".encode()).hexdigest()[:32]


def _receipt_span_id(receipt_id: str, ordinal: int) -> str:
    return hashlib.sha256(f"brigade:receipt:{receipt_id}:{ordinal}".encode()).hexdigest()[:16]


def _outcome_record_id(record: dict[str, Any], line_no: int) -> str:
    digest = _valid_outcome_digest(record)
    if digest is not None:
        return digest
    identity = ":".join((str(line_no), record["ts"], record["artifact_id"], record["evidence_ref"]))
    return hashlib.sha256(f"brigade:outcome:{identity}".encode()).hexdigest()


def _valid_outcome_digest(record: dict[str, Any]) -> str | None:
    digest = record.get("digest")
    if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest):
        return None
    expected = localio.canonical_json_digest(record, exclude_keys={"digest"})
    if digest != expected:
        return None
    return digest


def _valid_outcome_fields(payload: dict[str, Any]) -> bool:
    for key in _OUTCOME_SCALAR_KEYS:
        if not isinstance(payload.get(key), str) or not payload[key]:
            return False
    signal = payload.get("signal_value")
    return isinstance(signal, int) and not isinstance(signal, bool) and signal in (-1, 0, 1)


def _outcome_span_id(record_id: str, ordinal: int) -> str:
    return hashlib.sha256(f"brigade:outcome:span:{record_id}:{ordinal}".encode()).hexdigest()[:16]


def _provider(cli: str) -> str | None:
    return {"codex": "openai", "claude": "anthropic", "grok": "x_ai"}.get(cli)


def _error_type(result: dict[str, Any]) -> str | None:
    if result.get("ok") is True:
        return None
    if result.get("timed_out") is True:
        return "timeout"
    exit_code = result.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return "process_error"
    return "worker_error"


def _resolve_under_target(target: Path, ref: str) -> Path | None:
    if not ref:
        return None
    path = Path(ref)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(target)
        except ValueError:
            return None
    return Path(ref)


def _as_num(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _as_str(value: Any, *, iso: bool = False) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if iso and localio.parse_iso_datetime(value) is None:
        return None
    return value


def _valid_harness_session(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    harness = value.get("harness")
    fingerprint = value.get("fingerprint")
    if not isinstance(harness, str) or not harness or harness not in KNOWN_HARNESSES:
        return None
    if not isinstance(fingerprint, str) or not _FINGERPRINT_HEX.fullmatch(fingerprint):
        return None
    return harness, fingerprint


def _verify_executable(command: str) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts:
        return None
    argv = list(parts)
    while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        argv.pop(0)
    if not argv:
        return None
    return Path(argv[0]).name or None


def _base(run_id: str, run: dict[str, Any], seat: str, result: dict[str, Any], ordinal: int) -> dict[str, Any]:
    trace_id, span_id = _ids(run_id, seat, ordinal)
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "name": "brigade.run.worker",
        "start_time": run.get("started_at"),
        "end_time": run.get("finished_at"),
        "duration_seconds": result.get("duration_seconds"),
        "status": "OK" if result.get("ok") is True else "ERROR",
        "error_type": _error_type(result),
        "seat": seat,
        "adapter": result.get("transport") or "cli",
        "requested_model": result.get("requested_model"),
        "effective_model": result.get("effective_model"),
        "reasoning": result.get("reasoning"),
        "stop_reason": result.get("stop_reason"),
        "exit_code": result.get("exit_code"),
    }


def _tool_row(
    projection: str,
    base: dict[str, Any],
    *,
    tool_name: str,
    tool_call_id: str,
    brigade_attrs: dict[str, Any],
) -> dict[str, Any]:
    otel = projection == "otel-genai"
    mapping = OTEL_MAPPING if otel else OPENINFERENCE_MAPPING
    schema = "brigade.otel_genai_projection.v1" if otel else "brigade.openinference_projection.v1"
    attributes = (
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": tool_name,
            "gen_ai.tool.call.id": tool_call_id,
            **brigade_attrs,
        }
        if otel
        else {"openinference.span.kind": "TOOL", "tool.name": tool_name, "tool.id": tool_call_id, **brigade_attrs}
    )
    if base["error_type"] is not None:
        attributes["error.type"] = base["error_type"]
    return {
        "schema": schema,
        "mapping": mapping,
        "trace_id": base["trace_id"],
        "span_id": base["span_id"],
        "parent_span_id": base.get("parent_span_id"),
        "name": f"execute_tool {tool_name}" if otel else tool_name,
        "start_time": base["start_time"],
        "end_time": base["end_time"],
        "duration_seconds": base["duration_seconds"],
        "status": base["status"],
        "error_type": base["error_type"],
        "attributes": attributes,
    }


def _run_row(base: dict[str, Any], cli: str, projection: str) -> dict[str, Any]:
    otel = projection == "otel-genai"
    attributes: dict[str, Any] = {
        "brigade.seat.name": base["seat"],
        "brigade.adapter.name": base["adapter"],
    }
    if otel:
        attributes["gen_ai.operation.name"] = "invoke_agent"
        if (provider := _provider(cli)) is not None:
            attributes["gen_ai.provider.name"] = provider
        if base["requested_model"] is not None:
            attributes["gen_ai.request.model"] = base["requested_model"]
        if base["effective_model"] is not None:
            attributes["gen_ai.response.model"] = base["effective_model"]
        if base["stop_reason"] is not None:
            attributes["gen_ai.response.finish_reasons"] = [base["stop_reason"]]
        if base["error_type"] is not None:
            attributes["error.type"] = base["error_type"]
    else:
        attributes["openinference.span.kind"] = "AGENT"
        if (model := base["effective_model"] or base["requested_model"]) is not None:
            attributes["llm.model_name"] = model
    schema = "brigade.otel_genai_projection.v1" if otel else "brigade.openinference_projection.v1"
    mapping = OTEL_MAPPING if otel else OPENINFERENCE_MAPPING
    return {"schema": schema, "mapping": mapping, **base, "attributes": attributes}


def _lineage_parent_ids(
    payload: dict[str, Any],
    *,
    kind: str,
    manifest: Mapping[str, Any] | None = None,
    artifacts_dir: Path | None = None,
    resolve: Callable[[str, str], Mapping[str, Any] | None] | None = None,
) -> list[str]:
    receipt = causal_receipt.read_causal_receipt(payload)
    if receipt is None or causal_receipt.validate_receipt(receipt):
        return []
    parents = causal_receipt.lineage_parents(
        receipt,
        manifest=manifest,
        resolve=resolve,
        artifacts_dir=artifacts_dir,
    )
    ids: list[str] = []
    for parent in parents:
        if parent.get("kind") != kind:
            continue
        parent_id = parent.get("id")
        if isinstance(parent_id, str) and parent_id:
            ids.append(parent_id)
    return ids


def _outcome_parentage(
    target: Path,
    record: dict[str, Any],
    run_anchors: dict[str, tuple[str, str]],
    verify_parents: dict[str, tuple[str, str]],
) -> tuple[str, str | None] | None:
    run_ids = _lineage_parent_ids(record, kind="run")
    if len(run_ids) == 1:
        return run_anchors.get(run_ids[0])
    verify_ids = _lineage_parent_ids(record, kind="verify")
    if len(verify_ids) == 1:
        verify_id = verify_ids[0]
        anchor = verify_parents.get(verify_id)
        if anchor is None:
            verify_receipt = _object(target / ".brigade" / "work" / "verify-runs" / verify_id / "receipt.json")
            if verify_receipt is not None:
                run_ids = _lineage_parent_ids(verify_receipt, kind="run")
                if len(run_ids) == 1:
                    anchor = run_anchors.get(run_ids[0])
        return anchor if anchor is not None else (_receipt_trace_id(verify_id), None)
    rel = _resolve_under_target(target, record["evidence_ref"])
    if rel is None:
        return None
    parts = rel.parts
    if len(parts) == 4 and parts[0] == ".brigade" and parts[1] == "runs" and parts[-1] == "run.json":
        return run_anchors.get(parts[2])
    if (
        len(parts) == 5
        and parts[0] == ".brigade"
        and parts[1] == "work"
        and parts[2] == "verify-runs"
        and parts[-1] == "receipt.json"
    ):
        verify_id = parts[3]
        anchor = verify_parents.get(verify_id)
        return anchor if anchor is not None else (_receipt_trace_id(verify_id), None)
    return None


def _project_runs(
    target: Path, projection: str
) -> tuple[list[dict[str, Any]], dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    rows: list[dict[str, Any]] = []
    run_anchors: dict[str, tuple[str, str]] = {}
    verify_parents: dict[str, tuple[str, str]] = {}
    ambiguous_verify: set[str] = set()
    root = target / ".brigade" / "runs"
    for run_dir in sorted(root.iterdir()) if root.is_dir() else []:
        if not run_dir.is_dir():
            continue
        run = _object(run_dir / "run.json")
        roster = _object(run_dir / "roster.json")
        workers = _object(run_dir / "worker-results.json")
        if run is None or roster is None or workers is None:
            continue
        agents = roster.get("agents")
        if not isinstance(agents, dict):
            continue
        anchor: tuple[str, str] | None = None
        for ordinal, result in enumerate(workers.get("results", []), start=1):
            if not isinstance(result, dict) or not isinstance(result.get("worker"), str):
                continue
            seat = result["worker"]
            agent = agents.get(seat)
            if not isinstance(agent, dict):
                continue
            cli = str(agent.get("cli") or "unknown")
            base = _base(run_dir.name, run, seat, result, ordinal)
            if anchor is None:
                anchor = (base["trace_id"], base["span_id"])
            rows.append(_run_row(base, cli, projection))
        if anchor is not None:
            run_anchors[run_dir.name] = anchor
            ground_truth = workers.get("ground_truth")
            if isinstance(ground_truth, dict):
                verify_receipts = ground_truth.get("verify_receipts")
                if isinstance(verify_receipts, list):
                    for entry in verify_receipts:
                        if not isinstance(entry, dict):
                            continue
                        run_id = entry.get("run_id")
                        if not isinstance(run_id, str) or not run_id or run_id in ambiguous_verify:
                            continue
                        existing = verify_parents.get(run_id)
                        if existing is None:
                            verify_parents[run_id] = anchor
                        elif existing != anchor:
                            ambiguous_verify.add(run_id)
                            verify_parents.pop(run_id, None)
    return rows, run_anchors, verify_parents


def _project_verify(
    target: Path,
    projection: str,
    verify_parents: dict[str, tuple[str, str]],
    run_anchors: dict[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    target_name = target.name
    root = target / ".brigade" / "work" / "verify-runs"
    for run_dir in sorted(root.iterdir()) if root.is_dir() else []:
        if not run_dir.is_dir():
            continue
        receipt = _object(run_dir / "receipt.json")
        if receipt is None:
            continue
        receipt_id = receipt.get("run_id")
        if not isinstance(receipt_id, str) or not receipt_id or receipt_id != run_dir.name:
            continue
        commands = receipt.get("commands")
        if not isinstance(commands, list):
            continue
        lineage_runs = _lineage_parent_ids(receipt, kind="run")
        lineage_anchor = None
        if run_anchors is not None and len(lineage_runs) == 1:
            lineage_anchor = run_anchors.get(lineage_runs[0])
            if lineage_anchor is not None:
                verify_parents[receipt_id] = lineage_anchor
        anchor = lineage_anchor if lineage_anchor is not None else verify_parents.get(receipt_id)
        trace_id, parent_span_id = anchor if anchor is not None else (_receipt_trace_id(receipt_id), None)
        harness_session = _valid_harness_session(receipt.get("harness_session"))
        harness = fingerprint = None
        if harness_session is not None:
            harness, fingerprint = harness_session
        receipt_status = _as_str(receipt.get("status"))
        receipt_started_at = _as_str(receipt.get("started_at"), iso=True)
        receipt_completed_at = _as_str(receipt.get("completed_at"), iso=True)
        for ordinal, command in enumerate(commands, start=1):
            if not isinstance(command, dict):
                continue
            raw_command = command.get("command")
            if not isinstance(raw_command, str) or not raw_command:
                continue
            if (executable := _verify_executable(raw_command)) is None:
                continue
            if "status" in command:
                status = _as_str(command.get("status"))
                if status is None:
                    continue
            else:
                status = receipt_status
                if status is None:
                    continue
            start_time = _as_str(command.get("started_at"), iso=True) or receipt_started_at
            end_time = _as_str(command.get("completed_at"), iso=True) or receipt_completed_at
            if start_time is None or end_time is None:
                continue
            exit_code = command.get("exit_code")
            duration = _as_num(command.get("duration_seconds"))
            if duration is None:
                duration = _as_num(receipt.get("duration_seconds"))
            base = {
                "trace_id": trace_id,
                "span_id": _receipt_span_id(receipt_id, ordinal),
                "parent_span_id": parent_span_id,
                "name": "brigade.work.verify",
                "start_time": start_time,
                "end_time": end_time,
                "duration_seconds": duration,
                "status": "OK" if status == "completed" else "ERROR",
                "error_type": None if status == "completed" else "process_error",
            }
            brigade_attrs: dict[str, Any] = {
                "brigade.receipt.id": receipt_id,
                "brigade.target.repo": target_name,
                "brigade.verify.command.executable": executable,
                "brigade.verify.status": status,
            }
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                brigade_attrs["brigade.verify.exit_code"] = exit_code
            if harness is not None:
                brigade_attrs["brigade.harness.session.harness"] = harness
            if fingerprint is not None:
                brigade_attrs["brigade.harness.session.fingerprint"] = fingerprint
            rows.append(
                _tool_row(
                    projection,
                    base,
                    tool_name="brigade.work.verify",
                    tool_call_id=f"{receipt_id}:{ordinal}",
                    brigade_attrs=brigade_attrs,
                )
            )
    return rows


def _project_outcomes(
    target: Path,
    projection: str,
    run_anchors: dict[str, tuple[str, str]],
    verify_parents: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    path = target / "memory" / "outcome" / "records.jsonl"
    if not path.is_file():
        return rows
    status_payload = localio.read_json_dict(target / "memory" / "outcome" / "status.json") or {}
    status_by_artifact = status_payload.get("artifacts")
    if not isinstance(status_by_artifact, dict):
        status_by_artifact = {}
    entries: list[tuple[int, dict[str, Any], outcome.OutcomeRecord]] = []
    for line_no, line in enumerate(localio.read_jsonl_dicts(path), start=1):
        if not _valid_outcome_fields(line):
            continue
        record = _record_from_dict(line)
        if record is None:
            continue
        entries.append((line_no, line, record))
    cohorts_by_artifact = _fingerprint_cohorts_by_artifact(target, [record for _, _, record in entries])
    target_name = target.name
    seen_digests: set[str] = set()
    for line_no, line, record in entries:
        digest = _valid_outcome_digest(line)
        if digest is not None:
            if digest in seen_digests:
                continue
            seen_digests.add(digest)
        parentage = _outcome_parentage(target, line, run_anchors, verify_parents)
        if parentage is None:
            continue
        trace_id, parent_span_id = parentage
        record_id = _outcome_record_id(line, line_no)
        artifact_id = record.artifact_id
        cohorts = cohorts_by_artifact.get(artifact_id)
        score_obj = cohorts.current if cohorts is not None else outcome.score_records(artifact_id, [])
        promotion = status_by_artifact.get(artifact_id)
        current_status = promotion.get("status") if isinstance(promotion, dict) else None
        base = {
            "trace_id": trace_id,
            "span_id": _outcome_span_id(record_id, 1),
            "parent_span_id": parent_span_id,
            "name": "brigade.outcome.capture",
            "start_time": record.ts,
            "end_time": record.ts,
            "duration_seconds": None,
            "status": "OK",
            "error_type": None,
        }
        brigade_attrs: dict[str, Any] = {
            "brigade.target.repo": target_name,
            "brigade.outcome.artifact.id": artifact_id,
            "brigade.outcome.artifact.kind": record.artifact_kind,
            "brigade.outcome.source": record.source,
            "brigade.outcome.signal": record.signal_value,
            "brigade.outcome.current.score": score_obj.score,
            "brigade.outcome.current.helped": score_obj.helped,
            "brigade.outcome.current.hurt": score_obj.hurt,
            "brigade.outcome.current.neutral": score_obj.neutral,
        }
        if digest is not None:
            brigade_attrs["brigade.receipt.id"] = digest
        if isinstance(current_status, str) and current_status:
            brigade_attrs["brigade.outcome.current.status"] = current_status
        rows.append(
            _tool_row(
                projection,
                base,
                tool_name="brigade.outcome.capture",
                tool_call_id=record_id,
                brigade_attrs=brigade_attrs,
            )
        )
    return rows


def records(target: Path, projection: str) -> list[dict[str, Any]]:
    target = target.expanduser().resolve()
    rows, run_anchors, verify_parents = _project_runs(target, projection)
    rows.extend(_project_verify(target, projection, verify_parents, run_anchors))
    rows.extend(_project_outcomes(target, projection, run_anchors, verify_parents))
    return rows


def export(*, target: Path, projection: str, out: str = "-") -> int:
    rows = records(target, projection)
    if not rows:
        print("error: no Brigade worker receipts found", file=sys.stderr)
        return 1
    rendered = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    if out == "-":
        sys.stdout.write(rendered)
    else:
        localio.write_text_atomic(Path(out).expanduser(), rendered)
    return 0
