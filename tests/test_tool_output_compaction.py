import hashlib
import json
from pathlib import Path

from brigade.tools_cmd import runs, safety


def _write_receipt(
    tmp_path: Path,
    *,
    stdout: str = "",
    stderr: str = "",
    policy_decision: dict[str, object] | None = None,
) -> dict[str, object]:
    return runs._write_run_receipt(
        tmp_path,
        call={"id": "call-1", "tool_id": "example", "family": "cli", "contract": {}},
        run_id="run-1",
        started_at="2026-08-09T00:00:00+00:00",
        completed_at="2026-08-09T00:00:01+00:00",
        duration_seconds=1.0,
        status="completed",
        exit_code=0,
        timed_out=False,
        stdout=stdout,
        stderr=stderr,
        argv=["example"],
        cwd=tmp_path,
        policy_decision=policy_decision or {"env": {}, "env_labels_used": []},
    )


def _expected_compaction(*, stdout_omitted: int = 0, stderr_omitted: int = 0) -> dict[str, object]:
    omitted_by_stream = {
        stream: count for stream, count in (("stdout", stdout_omitted), ("stderr", stderr_omitted)) if count
    }
    return {
        "reducer_id": runs._OUTPUT_REDUCER_ID,
        "rule_hash": runs._OUTPUT_REDUCER_RULE_HASH,
        "omitted_count": sum(omitted_by_stream.values()),
        "omitted_by_stream": omitted_by_stream,
        "unit": "characters",
        "raw_retrievable": True,
    }


def test_output_reducer_rule_hash_matches_documented_rules() -> None:
    expected = hashlib.sha256(
        json.dumps(runs._OUTPUT_REDUCER_RULES, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert runs._OUTPUT_REDUCER_RULE_HASH == expected
    assert runs._OUTPUT_REDUCER_RULES["trim_trailing_before_ellipsis"] is True
    assert runs._OUTPUT_REDUCER_RULES["redact_secret_patterns"] is True


def test_tool_receipt_records_deterministic_output_compaction(tmp_path: Path) -> None:
    stdout = "word " * 150
    stderr = "error " * 120

    receipt = _write_receipt(tmp_path, stdout=stdout, stderr=stderr)

    _, stdout_omitted = runs._compact_output_summary(stdout)
    _, stderr_omitted = runs._compact_output_summary(stderr)
    assert receipt["output_compaction"] == _expected_compaction(
        stdout_omitted=stdout_omitted,
        stderr_omitted=stderr_omitted,
    )
    stored = json.loads((tmp_path / ".brigade" / "tools" / "runs" / "run-1.json").read_text())
    assert stored["output_compaction"] == receipt["output_compaction"]
    assert (tmp_path / ".brigade" / "tools" / "runs" / "run-1.stdout.log").read_text() == stdout
    assert runs._run_public_summary(receipt)["output_compaction"] == receipt["output_compaction"]


def test_tool_receipt_omits_compaction_metadata_when_output_is_unchanged(tmp_path: Path) -> None:
    receipt = _write_receipt(tmp_path, stdout="short output")

    assert receipt["stdout_summary"] == "short output"
    assert "output_compaction" not in receipt


def test_output_compaction_records_raw_logs_as_retrievable(tmp_path: Path) -> None:
    receipt = _write_receipt(tmp_path, stdout="x" * 700)

    compaction = receipt["output_compaction"]
    assert isinstance(compaction, dict)
    assert compaction["raw_retrievable"] is True
    assert (tmp_path / ".brigade" / "tools" / "runs" / "run-1.stdout.log").is_file()
    assert (tmp_path / ".brigade" / "tools" / "runs" / "run-1.stderr.log").is_file()


def test_output_compaction_omitted_counts_measure_post_redaction_text(tmp_path: Path) -> None:
    secret = "super-secret-value"
    stdout = f"api_key={secret} " + ("payload " * 200)

    receipt = _write_receipt(
        tmp_path,
        stdout=stdout,
        policy_decision={"env": {"SAFE": secret}, "env_labels_used": ["SAFE"]},
    )

    raw_log = (tmp_path / ".brigade" / "tools" / "runs" / "run-1.stdout.log").read_text()
    assert secret not in raw_log
    assert "[redacted]" in raw_log

    redacted = safety._redact_text(raw_log, None)
    summary = str(receipt["stdout_summary"])
    omitted = int(receipt["output_compaction"]["omitted_by_stream"]["stdout"])  # type: ignore[index]
    assert omitted == len(redacted) - len(summary)
    assert omitted != len(stdout) - len(summary)


def test_output_compaction_whitespace_normalization_affects_omitted_counts(tmp_path: Path) -> None:
    stdout = "   " + ("word  " * 200)

    receipt = _write_receipt(tmp_path, stdout=stdout)

    summary = str(receipt["stdout_summary"])
    redacted = safety._redact_text(stdout, None)
    collapsed = " ".join(redacted.split())
    omitted = int(receipt["output_compaction"]["omitted_by_stream"]["stdout"])  # type: ignore[index]
    assert summary == runs._compact_output_summary(stdout)[0]
    assert omitted == len(redacted) - len(summary)
    assert len(collapsed) - len(summary) != omitted


def test_output_compaction_reconciles_with_on_disk_raw_log_lengths(tmp_path: Path) -> None:
    stdout = "word " * 150
    stderr = "error " * 120

    receipt = _write_receipt(tmp_path, stdout=stdout, stderr=stderr)

    for stream, raw_text in (("stdout", stdout), ("stderr", stderr)):
        raw_log = (tmp_path / ".brigade" / "tools" / "runs" / f"run-1.{stream}.log").read_text()
        assert raw_log == raw_text
        redacted = safety._redact_text(raw_log, None)
        summary = str(receipt[f"{stream}_summary"])
        omitted = int(receipt["output_compaction"]["omitted_by_stream"][stream])  # type: ignore[index]
        assert omitted == len(redacted) - len(summary)
        assert len(raw_log) + omitted != len(summary)
