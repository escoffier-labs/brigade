"""Resource bounds for signed agent-change approval references."""

from __future__ import annotations

import gc
import io
import os
import subprocess
import sys
import json
import tracemalloc
from collections.abc import Mapping
from contextlib import redirect_stdout
from pathlib import Path
from pathlib import PureWindowsPath

import pytest

from brigade import agent_change_approval_verify, approval_verification, attestation, cli

from tests import test_agent_change as fixtures
from tests.test_agent_change_approval_integration import _append_v2_decision


def _verify_statement(
    target: Path, run_dir: Path, key: Path, statement: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, object]]:
    envelope = attestation.create_envelope(statement, key)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    return result, json.loads(capsys.readouterr().out)


def _approval_references(statement: Mapping[str, object]) -> list[dict[str, object]]:
    predicate = statement["predicate"]
    assert isinstance(predicate, dict)
    references = predicate["references"]
    assert isinstance(references, list)
    return [
        reference
        for reference in references
        if isinstance(reference, dict) and reference.get("kind") == "human-approval"
    ]


def _add_approval_padding(run_dir: Path, nonce: str, padding_bytes: int) -> None:
    path = run_dir / "approvals" / f"{nonce}.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["opaqueMetadata"] = "x" * padding_bytes
    path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")


def _resource_fixture(tmp_path: Path, *, padding_bytes: int) -> tuple[Path, Path, Path, dict[str, object]]:
    tmp_path.mkdir()
    target, run_dir, key, _request = fixtures._build_full_run(tmp_path)
    _add_approval_padding(run_dir, "02" * 16, padding_bytes)
    for value in range(3, 35):
        nonce = f"{value:02x}" * 16
        _append_v2_decision(target, run_dir, decision="allow", nonce=nonce)
        _add_approval_padding(run_dir, nonce, padding_bytes)
    return target, run_dir, key, fixtures.agent_change.build_statement(target, run_dir.name)


def _measure_public_verifier(index_path: str, target: str, *, retain_history: bool) -> None:
    arguments = ["receipts", "verify-agent-change", index_path, "--target", target, "--json"]
    # Warm lazy imports and CLI caches outside tracing in this fresh process.
    with redirect_stdout(io.StringIO()):
        warm_result = cli.main(arguments)
        assert warm_result == 0
    retained: list[object] = []
    if retain_history:
        original_load = agent_change_approval_verify._load_selected_approval

        def retain_envelope(path: Path, nonce: str) -> agent_change_approval_verify._LoadedApproval | None:
            loaded = original_load(path, nonce)
            retained.append(loaded)
            return loaded

        agent_change_approval_verify._load_selected_approval = retain_envelope
    output = io.StringIO()
    was_collecting = gc.isenabled()
    gc.collect()
    try:
        gc.disable()
        tracemalloc.start()
        baseline, _previous_peak = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        with redirect_stdout(output):
            result = cli.main(arguments)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        if was_collecting:
            gc.enable()
    print(json.dumps([result, json.loads(output.getvalue()), peak - baseline]))


def _peak_verification_memory(
    target: Path,
    run_dir: Path,
    key: Path,
    statement: dict[str, object],
    *,
    retain_history: bool = False,
) -> tuple[int, dict[str, object], int]:
    envelope = attestation.create_envelope(statement, key)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join([str(root / "src"), environment.get("PYTHONPATH", "")])
    # A full shard can trigger a multi-megabyte CPython intern-table allocation
    # inside pathlib. Give each fixture the same fresh, warmed process instead.
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.test_agent_change_approval_resources",
            str(index_path),
            str(target),
            str(int(retain_history)),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    result, output, peak = json.loads(completed.stdout)
    return result, output, peak


def test_mixed_hash_peer_preserves_the_valid_current_observation_and_matching_duplicates_conflict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key, _request = fixtures._build_full_run(tmp_path)
    statement = fixtures.agent_change.build_statement(target, run_dir.name)
    approvals = _approval_references(statement)
    assert len(approvals) == 1
    wrong_hash = dict(approvals[0])
    wrong_hash["payloadSha256"] = "0" * 64
    predicate = statement["predicate"]
    assert isinstance(predicate, dict)
    references = predicate["references"]
    assert isinstance(references, list)
    references.append(wrong_hash)

    result, output = _verify_statement(target, run_dir, key, statement, capsys)

    observed = [reference for reference in output["references"] if reference["kind"] == "human-approval"]
    required = next(item for item in output["required_set"] if item["kind"] == "human-approval")
    assert result == 1
    assert output["status"] == "INVALID"
    assert observed[0]["binding"] == "bound"
    assert observed[0]["approval_state"] == "current"
    assert observed[0]["policy_outcome"] == "pass"
    assert observed[1]["binding"] == "conflicted"
    assert required["count"] == 1

    matching_statement = fixtures.agent_change.build_statement(target, run_dir.name)
    matching_predicate = matching_statement["predicate"]
    assert isinstance(matching_predicate, dict)
    matching_references = matching_predicate["references"]
    assert isinstance(matching_references, list)
    matching_references.append(dict(_approval_references(matching_statement)[0]))

    result, output = _verify_statement(target, run_dir, key, matching_statement, capsys)

    observed = [reference for reference in output["references"] if reference["kind"] == "human-approval"]
    required = next(item for item in output["required_set"] if item["kind"] == "human-approval")
    assert result == 1
    assert output["status"] == "INVALID"
    assert all(reference["binding"] == "conflicted" for reference in observed)
    assert required["count"] == 0


def _assert_historical_padding_peak(tmp_path: Path, *, retain_history: bool = False) -> None:
    plain = _resource_fixture(tmp_path / "plain", padding_bytes=0)
    padded = _resource_fixture(tmp_path / "padded", padding_bytes=128 * 1024)

    plain_result, plain_output, plain_peak = _peak_verification_memory(*plain, retain_history=retain_history)
    padded_result, padded_output, padded_peak = _peak_verification_memory(*padded, retain_history=retain_history)

    assert plain_result == padded_result == 0
    assert plain_output["status"] == padded_output["status"] == "COMPLETE-OK"
    assert padded_peak - plain_peak < 768 * 1024, "historical approval padding exceeds peak bound"


def test_historical_approval_padding_has_a_bounded_public_verifier_peak(tmp_path: Path) -> None:
    _assert_historical_padding_peak(tmp_path)


def test_public_verifier_peak_detects_retained_historical_envelopes(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="historical approval padding exceeds peak bound"):
        _assert_historical_padding_peak(tmp_path, retain_history=True)


def test_repeated_valid_descriptor_is_invalid_without_repeating_first_window_load_or_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target, run_dir, key, _request = fixtures._build_full_run(tmp_path)
    statement = fixtures.agent_change.build_statement(target, run_dir.name)
    predicate = statement["predicate"]
    assert isinstance(predicate, dict)
    references = predicate["references"]
    assert isinstance(references, list)
    descriptor = dict(_approval_references(statement)[0])
    references.extend(dict(descriptor) for _ in range(31))
    loads = 0
    validations = 0
    original_load = agent_change_approval_verify._load_selected_approval
    original_validate = approval_verification.validate_approval_artifact

    def count_load(path: Path, nonce: str) -> object:
        nonlocal loads
        loads += 1
        return original_load(path, nonce)

    def count_validation(*args: object, **kwargs: object) -> object:
        nonlocal validations
        validations += 1
        return original_validate(*args, **kwargs)

    monkeypatch.setattr("brigade.agent_change_approval_verify._load_selected_approval", count_load)
    monkeypatch.setattr(
        "brigade.agent_change_approval_verify.approval_verification.validate_approval_artifact", count_validation
    )

    result, output = _verify_statement(target, run_dir, key, statement, capsys)

    approvals = [reference for reference in output["references"] if reference["kind"] == "human-approval"]
    required = next(item for item in output["required_set"] if item["kind"] == "human-approval")
    assert result == 1
    assert output["status"] == "INVALID"
    assert all(reference["binding"] == "conflicted" for reference in approvals)
    assert required["count"] == 0
    assert loads <= 1
    assert validations <= 1


def test_historical_envelope_replacement_before_final_reread_is_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target, run_dir, key, _request = fixtures._build_full_run(tmp_path)
    _append_v2_decision(target, run_dir, decision="allow", nonce="03" * 16)
    statement = fixtures.agent_change.build_statement(target, run_dir.name)
    historical_path = run_dir / "approvals" / f"{'02' * 16}.json"
    current_path = run_dir / "approvals" / f"{'03' * 16}.json"
    original_load = agent_change_approval_verify._load_selected_approval
    historical_processed = False
    replaced = False

    def replace_historical(path: Path, nonce: str) -> object:
        nonlocal historical_processed, replaced
        loaded = original_load(path, nonce)
        if path == historical_path:
            historical_processed = True
        if path == current_path and historical_processed and not replaced:
            replaced = True
            historical_path.write_text("{}", encoding="utf-8")
        return loaded

    monkeypatch.setattr("brigade.agent_change_approval_verify._load_selected_approval", replace_historical)
    result, output = _verify_statement(target, run_dir, key, statement, capsys)

    assert historical_processed
    assert replaced
    assert result == 1
    assert output["status"] == "INVALID"


def test_historical_reread_preserves_the_validated_posix_locator_on_windows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target, run_dir, key, _request = fixtures._build_full_run(tmp_path)
    _append_v2_decision(target, run_dir, decision="allow", nonce="03" * 16)
    statement = fixtures.agent_change.build_statement(target, run_dir.name)
    historical_path = run_dir / "approvals" / f"{'02' * 16}.json"
    original_load = agent_change_approval_verify._load_selected_approval
    path_type = type(historical_path)
    original_relative_to = path_type.relative_to
    emulating_windows = False

    def windows_relative_to(path: Path, *other: Path) -> Path:
        relative = original_relative_to(path, *other)
        if path == historical_path:
            return PureWindowsPath(*relative.parts)  # type: ignore[return-value]
        return relative

    def emulate_windows_after_history(path: Path, nonce: str) -> object:
        nonlocal emulating_windows
        loaded = original_load(path, nonce)
        if path == historical_path and not emulating_windows:
            emulating_windows = True
            monkeypatch.setattr(path_type, "relative_to", windows_relative_to)
        return loaded

    monkeypatch.setattr("brigade.agent_change_approval_verify._load_selected_approval", emulate_windows_after_history)
    result, output = _verify_statement(target, run_dir, key, statement, capsys)

    assert emulating_windows
    assert result == 0
    assert output["status"] == "COMPLETE-OK"


if __name__ == "__main__":
    _measure_public_verifier(sys.argv[1], sys.argv[2], retain_history=bool(int(sys.argv[3])))
