"""Unreadable verification history must not prevent inspection or a new run."""

import json
import sys

import pytest

from brigade import cli


def _corrupt_receipts():
    cases = [
        pytest.param(b'{"run_id": "broken", "value": "\xff"}', id="invalid-utf8"),
        pytest.param(
            (b'{"value":' + b"[" * 20000 + b"0" + b"]" * 20000 + b"}"),
            id="deep-json",
        ),
        pytest.param(b'{"value":' + b"[" * 129 + b"0" + b"]" * 129 + b"}", id="render-depth"),
        pytest.param(b'{"run_id":', id="truncated-json"),
        pytest.param(b"[]", id="non-object"),
    ]
    digit_limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if digit_limit:
        cases.append(pytest.param(b'{"value":' + b"9" * (digit_limit + 1) + b"}", id="huge-integer"))
    return cases


@pytest.mark.parametrize("raw", _corrupt_receipts())
@pytest.mark.parametrize("json_output", [False, True], ids=["text", "json"])
def test_corrupt_receipt_does_not_block_history_or_new_verification(tmp_path, capsys, raw, json_output):
    root = tmp_path / ".brigade" / "work" / "verify-runs"
    valid_dir = root / "20260101-000000-work-verify-valid"
    valid_dir.mkdir(parents=True)
    valid = {
        "run_id": valid_dir.name,
        "status": "completed",
        "started_at": "2026-01-01T00:00:00+00:00",
        "commands": [],
    }
    valid_bytes = json.dumps(valid).encode("utf-8")
    (valid_dir / "receipt.json").write_bytes(valid_bytes)
    broken_dir = root / "20260102-000000-work-verify-broken"
    broken_dir.mkdir()
    broken_path = broken_dir / "receipt.json"
    broken_path.write_bytes(raw)

    def invoke(*args):
        return cli.main(["work", "verify", *args, "--target", str(tmp_path), *(["--json"] if json_output else [])])

    assert invoke("runs") == 0
    out = capsys.readouterr().out
    if json_output:
        assert [run["run_id"] for run in json.loads(out)["runs"]] == [valid_dir.name]
    else:
        assert valid_dir.name in out
        assert broken_dir.name not in out

    for run_id in ("latest", valid_dir.name):
        assert invoke("show", run_id) == 0
        out = capsys.readouterr().out
        if json_output:
            assert json.loads(out)["run_id"] == valid_dir.name
        else:
            assert f"work verify run: {valid_dir.name}" in out

    assert invoke("show", broken_dir.name) == 1
    assert f"verification run not found: {broken_dir.name}" in capsys.readouterr().err

    assert invoke("plan", "--command", f"{sys.executable} --version") == 0
    out = capsys.readouterr().out
    if json_output:
        assert json.loads(out)["evidence"]["latest_verify"]["run_id"] == valid_dir.name

    assert invoke("run", "--argv-json", json.dumps([sys.executable, "--version"]), "--capture", "brigade-work") == 0
    out = capsys.readouterr().out
    if json_output:
        receipt = json.loads(out)
        assert receipt["status"] == "completed"
        assert receipt["commands"][0]["exit_code"] == 0
        assert receipt["outcome_capture"]["artifact_id"] == "brigade-work"
        assert receipt["miseledger_indexing"]["error_count"] == 1
        assert receipt["evidence"]["latest_verify"]["run_id"] == valid_dir.name
    else:
        assert "status: completed" in out

    # History inspection and recovery must preserve both original artifacts.
    assert broken_path.read_bytes() == raw
    assert (valid_dir / "receipt.json").read_bytes() == valid_bytes


def test_verification_history_preserves_supported_nested_metadata(tmp_path, capsys):
    nested = 0
    for _ in range(127):
        nested = [nested]
    payload = {
        "run_id": "supported-nesting",
        "commands": [],
        "nested": nested,
        "literal": "[" * 300 + '\\"' + "]" * 300,
    }
    path = tmp_path / ".brigade/work/verify-runs/supported-nesting/receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload))
    assert cli.main(["work", "verify", "show", "latest", "--target", str(tmp_path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["nested"] == nested
    assert result["literal"] == payload["literal"]
