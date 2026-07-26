from __future__ import annotations

import hashlib
import json

import pytest

from brigade import cli, localio, outcome as outcome_core
from brigade import telemetry_export

_RUN_ONLY_OTEL_ROW = json.loads(
    '{"adapter":"acpx","attributes":{"brigade.adapter.name":"acpx","brigade.seat.name":"worker","gen_ai.operation.name":"invoke_agent","gen_ai.provider.name":"x_ai","gen_ai.request.model":"grok-4.5","gen_ai.response.finish_reasons":["end_turn"],"gen_ai.response.model":"grok-4.5"},"duration_seconds":2.0,"effective_model":"grok-4.5","end_time":"2026-01-01T00:00:02Z","error_type":null,"exit_code":0,"mapping":{"mapping_version":1,"name":"otel-genai","upstream_revision":"opentelemetry-semconv-1.43.0"},"name":"brigade.run.worker","reasoning":null,"requested_model":"grok-4.5","schema":"brigade.otel_genai_projection.v1","seat":"worker","span_id":"dc8c20677f4f195d","start_time":"2026-01-01T00:00:00Z","status":"OK","stop_reason":"end_turn","trace_id":"9f319429aa7ee1e464fb955454290143"}'
)
_RUN_ONLY_OPENINFERENCE_ROW = json.loads(
    '{"adapter":"acpx","attributes":{"brigade.adapter.name":"acpx","brigade.seat.name":"worker","llm.model_name":"grok-4.5","openinference.span.kind":"AGENT"},"duration_seconds":2.0,"effective_model":"grok-4.5","end_time":"2026-01-01T00:00:02Z","error_type":null,"exit_code":0,"mapping":{"mapping_version":1,"name":"openinference","upstream_revision":"audited-2026-07-12"},"name":"brigade.run.worker","reasoning":null,"requested_model":"grok-4.5","schema":"brigade.openinference_projection.v1","seat":"worker","span_id":"dc8c20677f4f195d","start_time":"2026-01-01T00:00:00Z","status":"OK","stop_reason":"end_turn","trace_id":"9f319429aa7ee1e464fb955454290143"}'
)
_RUN_ONLY_OTEL_FAILED_ROW = json.loads(
    '{"adapter":"acpx","attributes":{"brigade.adapter.name":"acpx","brigade.seat.name":"worker","error.type":"process_error","gen_ai.operation.name":"invoke_agent","gen_ai.provider.name":"x_ai","gen_ai.request.model":"grok-4.5","gen_ai.response.finish_reasons":["end_turn"],"gen_ai.response.model":"grok-4.5"},"duration_seconds":2.0,"effective_model":"grok-4.5","end_time":"2026-01-01T00:00:02Z","error_type":"process_error","exit_code":1,"mapping":{"mapping_version":1,"name":"otel-genai","upstream_revision":"opentelemetry-semconv-1.43.0"},"name":"brigade.run.worker","reasoning":null,"requested_model":"grok-4.5","schema":"brigade.otel_genai_projection.v1","seat":"worker","span_id":"dc8c20677f4f195d","start_time":"2026-01-01T00:00:00Z","status":"ERROR","stop_reason":"end_turn","trace_id":"9f319429aa7ee1e464fb955454290143"}'
)
_RUN_ONLY_OPENINFERENCE_FAILED_ROW = json.loads(
    '{"adapter":"acpx","attributes":{"brigade.adapter.name":"acpx","brigade.seat.name":"worker","llm.model_name":"grok-4.5","openinference.span.kind":"AGENT"},"duration_seconds":2.0,"effective_model":"grok-4.5","end_time":"2026-01-01T00:00:02Z","error_type":"process_error","exit_code":1,"mapping":{"mapping_version":1,"name":"openinference","upstream_revision":"audited-2026-07-12"},"name":"brigade.run.worker","reasoning":null,"requested_model":"grok-4.5","schema":"brigade.openinference_projection.v1","seat":"worker","span_id":"dc8c20677f4f195d","start_time":"2026-01-01T00:00:00Z","status":"ERROR","stop_reason":"end_turn","trace_id":"9f319429aa7ee1e464fb955454290143"}'
)


def _run(tmp_path, *, run_id="run-1", verify_receipts=None):
    run = tmp_path / ".brigade" / "runs" / run_id
    run.mkdir(parents=True)
    workers = {
        "results": [
            {
                "worker": "worker",
                "ok": True,
                "text": "PRIVATE OUTPUT",
                "duration_seconds": 2.0,
                "transport": "acpx",
                "requested_model": "grok-4.5",
                "effective_model": "grok-4.5",
                "stop_reason": "end_turn",
                "exit_code": 0,
            }
        ],
    }
    if verify_receipts is not None:
        workers["ground_truth"] = {"available": True, "verify_receipts": verify_receipts}
    (run / "run.json").write_text(
        json.dumps({"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:02Z", "task": "SECRET"})
    )
    (run / "roster.json").write_text(json.dumps({"agents": {"worker": {"cli": "grok", "model": "grok-4.5"}}}))
    (run / "worker-results.json").write_text(json.dumps(workers))
    return run


def _verify_receipt(tmp_path, run_id, *, receipt_run_id=None, signed=True, **overrides):
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / run_id
    run_dir.mkdir(parents=True)
    receipt = {
        "run_id": receipt_run_id or run_id,
        "target": str(tmp_path),
        "status": "completed",
        "started_at": "2026-01-01T00:00:01Z",
        "completed_at": "2026-01-01T00:00:03Z",
        "duration_seconds": 2.0,
        "commands": [
            {
                "command": overrides.pop("command", "pytest -q"),
                "status": overrides.pop("status", "completed"),
                "exit_code": overrides.pop("exit_code", 0),
                "duration_seconds": overrides.pop("duration_seconds", 1.5),
                "started_at": overrides.pop("command_started_at", "2026-01-01T00:00:01Z"),
                "completed_at": overrides.pop("command_completed_at", "2026-01-01T00:00:02Z"),
            }
        ],
        **overrides,
    }
    if signed:
        receipt["digests"] = {
            "algorithm": "sha256",
            "logs": {},
            "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
        }
    localio.write_json(run_dir / "receipt.json", receipt)
    return run_dir / "receipt.json"


def _outcome_payload(*, evidence_ref, artifact_id="brigade-work", signal_value=1, source="verify", **extra):
    return {
        "artifact_id": artifact_id,
        "artifact_kind": "skill",
        "task_id": "task-1",
        "source": source,
        "signal_value": signal_value,
        "evidence_ref": evidence_ref,
        "ts": "2026-01-01T00:00:04Z",
        **extra,
    }


def _write_outcome_raw(tmp_path, *records):
    path = tmp_path / "memory" / "outcome" / "records.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records))


def _write_outcomes(tmp_path, *records, artifact_id="brigade-work"):
    path = tmp_path / "memory" / "outcome" / "records.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines, prev_digest = [], None
    for record in records:
        row = dict(record)
        for key, default in (
            ("artifact_id", artifact_id),
            ("artifact_kind", "skill"),
            ("task_id", "task-1"),
            ("source", "verify"),
            ("signal_value", 1),
            ("ts", "2026-01-01T00:00:04Z"),
        ):
            row.setdefault(key, default)
        row["prev_digest"] = prev_digest
        row["digest"] = localio.canonical_json_digest(row, exclude_keys={"digest"})
        prev_digest = row["digest"]
        lines.append(json.dumps(row, sort_keys=True))
    path.write_text("\n".join(lines) + "\n")
    (tmp_path / "memory" / "outcome" / "status.json").write_text(
        json.dumps(
            {"artifacts": {artifact_id: {"status": "promoted", "last_action_ts": "2026-01-01T00:00:05Z"}}},
            sort_keys=True,
        )
    )


def _rows(tmp_path, projection):
    return telemetry_export.records(tmp_path, projection)


def _find(rows, name, *, projection="otel-genai", tool=None):
    if tool is not None:
        name = f"execute_tool {tool}" if projection == "otel-genai" else tool
    return next(row for row in rows if row["name"] == name)


def _no_tool(rows, tool, projection="otel-genai"):
    name = f"execute_tool {tool}" if projection == "otel-genai" else tool
    assert all(row["name"] != name for row in rows)


def _linked_verify(tmp_path, verify_id, **receipt_overrides):
    _run(
        tmp_path,
        verify_receipts=[{"run_id": verify_id, "status": "completed", "commands": [{"command": "pytest -q"}]}],
    )
    return _verify_receipt(tmp_path, verify_id, **receipt_overrides)


def _parented_tool(rows, projection, tool):
    worker = _find(rows, "brigade.run.worker")
    child = _find(rows, "", projection=projection, tool=tool)
    assert child["trace_id"] == worker["trace_id"]
    assert child["parent_span_id"] == worker["span_id"]
    return child


@pytest.mark.parametrize(
    "projection,attrs,forbidden",
    [
        (
            "otel-genai",
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.provider.name": "x_ai",
                "gen_ai.request.model": "grok-4.5",
            },
            ("SECRET", "PRIVATE OUTPUT"),
        ),
        (
            "openinference",
            {"openinference.span.kind": "AGENT", "llm.model_name": "grok-4.5"},
            ("SECRET", "PRIVATE OUTPUT", "input.value", "output.value"),
        ),
    ],
)
def test_run_projection_content_free(tmp_path, capsys, projection, attrs, forbidden):
    _run(tmp_path)
    assert cli.main(["receipts", "export", projection, "--target", str(tmp_path)]) == 0
    raw = capsys.readouterr().out
    row = json.loads(raw)
    for key, value in attrs.items():
        assert row["attributes"][key] == value
    for token in forbidden:
        assert token not in raw


def _failed_run(tmp_path):
    _run(tmp_path)
    path = tmp_path / ".brigade" / "runs" / "run-1" / "worker-results.json"
    payload = json.loads(path.read_text())
    payload["results"][0].update(
        {"ok": False, "detail": "Bearer secret-token at /home/example/private", "exit_code": 1}
    )
    path.write_text(json.dumps(payload))


def test_failed_projection_uses_normalized_error_without_detail(tmp_path, capsys):
    _failed_run(tmp_path)
    assert cli.main(["receipts", "export", "otel-genai", "--target", str(tmp_path)]) == 0
    raw = capsys.readouterr().out
    row = json.loads(raw)
    assert row["error_type"] == "process_error"
    assert row["attributes"]["error.type"] == "process_error"
    assert "secret-token" not in raw and "/home/example" not in raw


def test_failed_run_only_projection_is_unchanged(tmp_path):
    _failed_run(tmp_path)
    otel_rows = _rows(tmp_path, "otel-genai")
    oi_rows = _rows(tmp_path, "openinference")
    assert otel_rows[0] == _RUN_ONLY_OTEL_FAILED_ROW
    assert oi_rows[0] == _RUN_ONLY_OPENINFERENCE_FAILED_ROW
    assert "error.type" not in oi_rows[0]["attributes"]


def test_multiprovider_cli_does_not_guess_provider(tmp_path, capsys):
    _run(tmp_path)
    (tmp_path / ".brigade" / "runs" / "run-1" / "roster.json").write_text(
        json.dumps({"agents": {"worker": {"cli": "opencode", "model": "anthropic/claude"}}})
    )
    assert cli.main(["receipts", "export", "otel-genai", "--target", str(tmp_path)]) == 0
    assert "gen_ai.provider.name" not in json.loads(capsys.readouterr().out)["attributes"]


def test_verify_tool_projection_contracts(tmp_path):
    verify_id = "20260101-000000-work-verify-abc"
    _linked_verify(
        tmp_path,
        verify_id,
        harness_session={"harness": "claude", "fingerprint": "0123456789abcdef"},
    )
    for projection, tool_attr, tool_id_attr in (
        ("otel-genai", "gen_ai.tool.name", "gen_ai.tool.call.id"),
        ("openinference", "tool.name", "tool.id"),
    ):
        rows = _rows(tmp_path, projection)
        verify = _parented_tool(rows, projection, "brigade.work.verify")
        assert verify["name"] == (
            "execute_tool brigade.work.verify" if projection == "otel-genai" else "brigade.work.verify"
        )
        assert verify["attributes"][tool_attr] == "brigade.work.verify"
        assert verify["attributes"][tool_id_attr] == f"{verify_id}:1"
    verify = _find(_rows(tmp_path, "otel-genai"), "", projection="otel-genai", tool="brigade.work.verify")
    assert verify["attributes"]["gen_ai.operation.name"] == "execute_tool"
    assert verify["start_time"] == "2026-01-01T00:00:01Z"
    assert verify["end_time"] == "2026-01-01T00:00:02Z"
    assert verify["duration_seconds"] == 1.5
    assert verify["attributes"]["brigade.receipt.id"] == verify_id
    assert verify["attributes"]["brigade.verify.command.executable"] == "pytest"
    assert verify["attributes"]["brigade.harness.session.harness"] == "claude"
    assert verify["attributes"]["brigade.harness.session.fingerprint"] == "0123456789abcdef"
    assert str(tmp_path) not in json.dumps(verify)

    zero_path = tmp_path / "zero-duration"
    zero_path.mkdir()
    _verify_receipt(zero_path, "20260101-000000-work-verify-zero", duration_seconds=0.0)
    assert (
        _find(_rows(zero_path, "otel-genai"), "", projection="otel-genai", tool="brigade.work.verify")[
            "duration_seconds"
        ]
        == 0.0
    )

    secret_path = tmp_path / "secret"
    secret_path.mkdir()
    _verify_receipt(
        secret_path,
        "20260101-000000-work-verify-secret",
        command="/private/bin/check --token secret-value",
    )
    secret = _find(_rows(secret_path, "otel-genai"), "", projection="otel-genai", tool="brigade.work.verify")
    secret_raw = json.dumps(secret)
    assert secret["attributes"]["brigade.verify.command.executable"] == "check"
    assert "/private" not in secret_raw and "secret-value" not in secret_raw

    orphan_path = tmp_path / "orphan"
    orphan_path.mkdir()
    orphan_id = "20260101-000000-work-verify-orphan"
    _verify_receipt(orphan_path, orphan_id)
    for projection in ("otel-genai", "openinference"):
        orphan = _find(_rows(orphan_path, projection), "", projection=projection, tool="brigade.work.verify")
        assert orphan["trace_id"] == telemetry_export._receipt_trace_id(orphan_id)
        assert orphan["parent_span_id"] is None

    ambiguous_path = tmp_path / "ambiguous"
    ambiguous_path.mkdir()
    ambiguous_id = "20260101-000000-work-verify-ambiguous"
    link = [{"run_id": ambiguous_id, "status": "completed", "commands": [{"command": "pytest -q"}]}]
    _run(ambiguous_path, run_id="run-a", verify_receipts=link)
    _run(ambiguous_path, run_id="run-b", verify_receipts=link)
    _verify_receipt(ambiguous_path, ambiguous_id)
    worker_a = _find(_rows(ambiguous_path, "otel-genai"), "brigade.run.worker")
    ambiguous = _find(_rows(ambiguous_path, "otel-genai"), "", projection="otel-genai", tool="brigade.work.verify")
    assert ambiguous["trace_id"] == telemetry_export._receipt_trace_id(ambiguous_id)
    assert ambiguous["parent_span_id"] is None
    assert ambiguous["trace_id"] != worker_a["trace_id"]

    sanitize_path = tmp_path / "sanitize"
    sanitize_path.mkdir()
    sanitize_id = "20260101-000000-work-verify-sanitize"
    _verify_receipt(
        sanitize_path,
        sanitize_id,
        signed=False,
        started_at={"nested": "2026-01-01T00:00:01Z"},
        harness_session={"harness": "/private/harness", "fingerprint": "not-hex"},
        commands=[
            {
                "command": "pytest -q",
                "status": "completed",
                "exit_code": 0,
                "started_at": {"nested": "2026-01-01T00:00:01Z"},
                "completed_at": "2026-01-01T00:00:02Z",
            }
        ],
    )
    _no_tool(_rows(sanitize_path, "otel-genai"), "brigade.work.verify")
    partial_id = f"{sanitize_id}-partial"
    _verify_receipt(
        sanitize_path,
        partial_id,
        signed=False,
        harness_session={"harness": "claude", "fingerprint": "0123456789abcdef"},
        commands=[
            {
                "command": "pytest -q",
                "status": "completed",
                "exit_code": 0,
                "started_at": "2026-01-01T00:00:01Z",
                "completed_at": "2026-01-01T00:00:02Z",
            }
        ],
    )
    partial = _find(_rows(sanitize_path, "otel-genai"), "", projection="otel-genai", tool="brigade.work.verify")
    partial_raw = json.dumps(partial)
    assert partial["start_time"] == "2026-01-01T00:00:01Z"
    assert partial["end_time"] == "2026-01-01T00:00:02Z"
    assert partial["attributes"]["brigade.harness.session.harness"] == "claude"
    assert partial["attributes"]["brigade.harness.session.fingerprint"] == "0123456789abcdef"
    assert "/private" not in partial_raw and "not-hex" not in partial_raw and "nested" not in partial_raw

    for index, mutation in enumerate(({"status": {"nested": "completed"}}, {"command": 123})):
        bad_path = tmp_path / f"verify-bad-{index}"
        bad_path.mkdir()
        bad_id = f"20260101-000000-work-verify-bad-{index}"
        command = {
            "command": "pytest -q",
            "status": "completed",
            "exit_code": 0,
            "duration_seconds": 1.0,
            "started_at": "2026-01-01T00:00:01Z",
            "completed_at": "2026-01-01T00:00:02Z",
            **mutation,
        }
        _verify_receipt(bad_path, bad_id, signed=False, commands=[command])
        _no_tool(_rows(bad_path, "otel-genai"), "brigade.work.verify")
    mismatch_path = tmp_path / "verify-mismatch"
    mismatch_path.mkdir()
    _verify_receipt(
        mismatch_path,
        "20260101-000000-work-verify-mismatch",
        signed=False,
        receipt_run_id="different-id",
    )
    _no_tool(_rows(mismatch_path, "otel-genai"), "brigade.work.verify")


def test_outcome_tool_projection_contracts(tmp_path):
    inherits_path = tmp_path / "inherits"
    inherits_path.mkdir()
    verify_id = "20260101-000000-work-verify-outcome"
    receipt_path = _linked_verify(inherits_path, verify_id)
    payload = _outcome_payload(evidence_ref=str(receipt_path))
    _write_outcomes(inherits_path, payload)
    digest = localio.canonical_json_digest({**payload, "prev_digest": None}, exclude_keys={"digest"})
    outcome_row = _parented_tool(_rows(inherits_path, "otel-genai"), "otel-genai", "brigade.outcome.capture")
    assert outcome_row["name"] == "execute_tool brigade.outcome.capture"
    attrs = outcome_row["attributes"]
    assert attrs["gen_ai.tool.call.id"] == digest == attrs["brigade.receipt.id"]
    assert attrs["brigade.outcome.artifact.id"] == "brigade-work"
    assert attrs["brigade.outcome.current.status"] == "promoted"
    expected = outcome_core.score_records(
        "brigade-work",
        [outcome_core.OutcomeRecord("brigade-work", "skill", "task-1", "verify", 1, str(receipt_path), payload["ts"])],
    )
    assert attrs["brigade.outcome.current.score"] == expected.score
    assert attrs["brigade.outcome.current.helped"] == expected.helped
    assert "task-1" not in json.dumps(outcome_row)
    _parented_tool(_rows(inherits_path, "openinference"), "openinference", "brigade.outcome.capture")

    cohort_path = tmp_path / "cohort"
    cohort_path.mkdir()
    skill_dir = cohort_path / ".brigade" / "skills" / "registry" / "brigade-work"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("# v1\n")
    old_fp = hashlib.sha256(skill_md.read_bytes()).hexdigest()
    cohort_receipt = _linked_verify(cohort_path, "20260101-000000-work-verify-cohort")
    skill_md.write_text("# v2\n")
    new_fp = hashlib.sha256(skill_md.read_bytes()).hexdigest()
    _write_outcomes(
        cohort_path,
        _outcome_payload(
            evidence_ref=str(cohort_receipt),
            signal_value=1,
            task_id="t-old",
            content_fingerprint=old_fp,
            ts="2026-06-20T00:00:00Z",
        ),
        _outcome_payload(
            evidence_ref=str(cohort_receipt),
            signal_value=-1,
            task_id="t-new",
            content_fingerprint=new_fp,
            ts="2026-06-20T01:00:00Z",
        ),
    )
    cohort_attrs = _find(_rows(cohort_path, "otel-genai"), "", projection="otel-genai", tool="brigade.outcome.capture")[
        "attributes"
    ]
    assert cohort_attrs["brigade.outcome.current.helped"] == 0
    assert cohort_attrs["brigade.outcome.current.hurt"] == 1
    assert cohort_attrs["brigade.outcome.current.score"] == outcome_core.wilson_lower_bound(0, 1)
    lifetime = outcome_core.score_records(
        "brigade-work",
        [
            outcome_core.OutcomeRecord(
                "brigade-work",
                "skill",
                "t-old",
                "verify",
                1,
                str(cohort_receipt),
                "2026-06-20T00:00:00Z",
                content_fingerprint=old_fp,
            ),
            outcome_core.OutcomeRecord(
                "brigade-work",
                "skill",
                "t-new",
                "verify",
                -1,
                str(cohort_receipt),
                "2026-06-20T01:00:00Z",
                content_fingerprint=new_fp,
            ),
        ],
    )
    assert lifetime.helped == 1 and lifetime.hurt == 1

    for signal_value, source in ((1, "run"), (-1, "verify")):
        case_path = tmp_path / f"case-{source}"
        case_path.mkdir()
        if source == "run":
            evidence_ref = str(_run(case_path) / "run.json")
        else:
            evidence_ref = str(_linked_verify(case_path, "20260101-000000-work-verify-fail"))
        _write_outcomes(
            case_path,
            _outcome_payload(evidence_ref=evidence_ref, signal_value=signal_value, source=source),
        )
        case_outcome = _parented_tool(_rows(case_path, "otel-genai"), "otel-genai", "brigade.outcome.capture")
        assert case_outcome["status"] == "OK"
        assert case_outcome["attributes"]["brigade.outcome.source"] == source
        assert case_outcome["attributes"]["brigade.outcome.signal"] == signal_value

    for case, projections, digest in (
        ("collision", ("otel-genai",), "USE_VERIFY_ID"),
        ("forged", ("otel-genai",), "a" * 64),
        ("digestless", ("otel-genai", "openinference"), None),
    ):
        case_path = tmp_path / f"identity-{case}"
        case_path.mkdir()
        identity_id = f"20260101-000000-work-verify-{case}"
        identity_receipt = _linked_verify(case_path, identity_id)
        identity_payload = _outcome_payload(evidence_ref=str(identity_receipt))
        if digest == "USE_VERIFY_ID":
            identity_payload["digest"] = identity_id
        elif digest is not None:
            identity_payload["digest"] = digest
        _write_outcome_raw(case_path, identity_payload)
        for projection in projections:
            rows = _rows(case_path, projection)
            tool_id_attr = "gen_ai.tool.call.id" if projection == "otel-genai" else "tool.id"
            outcome = _find(rows, "", projection=projection, tool="brigade.outcome.capture")
            assert "brigade.receipt.id" not in outcome["attributes"]
            correlation_id = outcome["attributes"][tool_id_attr]
            if case == "collision":
                verify = _find(rows, "", projection=projection, tool="brigade.work.verify")
                assert correlation_id != identity_id
                assert outcome["span_id"] != verify["span_id"]
                assert outcome["span_id"] != telemetry_export._receipt_span_id(identity_id, 1)
            elif case == "forged":
                assert correlation_id != digest
                assert correlation_id == telemetry_export._outcome_record_id(identity_payload, 1)
            else:
                assert telemetry_export._SHA256_HEX.fullmatch(correlation_id)
            assert outcome["span_id"] == telemetry_export._outcome_span_id(correlation_id, 1)

    dedup_path = tmp_path / "dedup"
    dedup_path.mkdir()
    dedup_receipt = _linked_verify(dedup_path, "20260101-000000-work-verify-dedup")
    first = _outcome_payload(evidence_ref=str(dedup_receipt), task_id="task-first", signal_value=1)
    first["prev_digest"] = None
    dedup_digest = localio.canonical_json_digest(first, exclude_keys={"digest"})
    first["digest"] = dedup_digest
    _write_outcome_raw(dedup_path, first, dict(first))
    (dedup_path / "memory" / "outcome" / "status.json").write_text(
        json.dumps(
            {"artifacts": {"brigade-work": {"status": "promoted", "last_action_ts": "2026-01-01T00:00:05Z"}}},
            sort_keys=True,
        )
    )
    outcomes = [row for row in _rows(dedup_path, "otel-genai") if row["name"] == "execute_tool brigade.outcome.capture"]
    assert len(outcomes) == 1
    assert outcomes[0]["attributes"]["gen_ai.tool.call.id"] == dedup_digest
    assert outcomes[0]["attributes"]["brigade.outcome.signal"] == 1

    extra_path_root = tmp_path / "extra-path"
    extra_path_root.mkdir()
    run_dir = _run(extra_path_root, run_id="extra-path-run")
    extra_path = run_dir / "extra" / "run.json"
    extra_path.parent.mkdir()
    extra_path.write_text((run_dir / "run.json").read_text())
    _write_outcomes(extra_path_root, _outcome_payload(evidence_ref=str(extra_path)))
    _no_tool(_rows(extra_path_root, "otel-genai"), "brigade.outcome.capture")

    for index, mutation in enumerate(
        (
            {"signal_value": {"bad": True}},
            {"artifact_id": {"nested": "x"}},
            {"artifact_kind": ""},
            {"task_id": 1},
            {"source": None},
            {"evidence_ref": ""},
            {"ts": ["2026-01-01T00:00:04Z"]},
            {"signal_value": 2},
            {"signal_value": True},
        )
    ):
        bad_path = tmp_path / f"bad-outcome-{index}"
        bad_path.mkdir()
        bad_receipt = _verify_receipt(bad_path, f"20260101-000000-work-verify-bad-{index}")
        bad_payload = _outcome_payload(evidence_ref=str(bad_receipt))
        bad_payload.update(mutation)
        _write_outcome_raw(bad_path, bad_payload)
        _no_tool(_rows(bad_path, "otel-genai"), "brigade.outcome.capture")


def test_run_only_projection_is_unchanged(tmp_path):
    _run(tmp_path)
    otel_rows = _rows(tmp_path, "otel-genai")
    oi_rows = _rows(tmp_path, "openinference")
    assert otel_rows[0] == _RUN_ONLY_OTEL_ROW
    assert oi_rows[0] == _RUN_ONLY_OPENINFERENCE_ROW
    verify_id = "20260101-000000-work-verify-extra"
    _run(
        tmp_path,
        run_id="run-2",
        verify_receipts=[{"run_id": verify_id, "status": "completed", "commands": [{"command": "pytest -q"}]}],
    )
    _verify_receipt(tmp_path, verify_id)
    _write_outcomes(
        tmp_path,
        _outcome_payload(evidence_ref=str(tmp_path / ".brigade" / "work" / "verify-runs" / verify_id / "receipt.json")),
    )
    otel_with_extra = _rows(tmp_path, "otel-genai")
    oi_with_extra = _rows(tmp_path, "openinference")
    assert otel_with_extra[0] == _RUN_ONLY_OTEL_ROW
    assert oi_with_extra[0] == _RUN_ONLY_OPENINFERENCE_ROW
    assert len(otel_with_extra) > 1 and len(oi_with_extra) > 1
