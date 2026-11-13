import json
import os
import stat
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from brigade import agent_identity, guard, observed_checks
from brigade.install import build_gitignore_block
from brigade.selection import Selection


def _check(target, command="pytest -q"):
    return observed_checks.build_check(
        target=target,
        command=command,
        status="ok",
        identity=agent_identity.AgentIdentity("codex", "hook-event", "model-x", "hook-event"),
        session_fingerprint=agent_identity.session_fingerprint("codex", "sample"),
    )


@pytest.mark.parametrize(
    ("command", "tool"),
    [
        ("python -m pytest -q tests/x.py", "pytest"),
        ("uv run pytest", "pytest"),
        ("poetry run python -m pytest", "pytest"),
        ("npx vitest run", "vitest"),
        ("npm test", "npm:test"),
        ("pnpm run check:types", "pnpm:check:types"),
        ("go test ./...", "go test"),
        ("cargo clippy", "cargo clippy"),
        ("make check", "make check"),
        ("ruff check .", "ruff check"),
        ("ruff format --check .", "ruff format"),
        ("mypy", "mypy"),
        ("./scripts/verify-focused tests/x.py", "scripts/verify-focused"),
        ("cd sub && FOO=1 pytest", "pytest"),
        ("echo hi", "other"),
        ("uv --project sub run --locked pytest", "pytest"),
        ("npx --yes vitest", "vitest"),
        ("python -I -m pytest", "pytest"),
        ("if pytest; then echo yes; fi", "pytest"),
    ],
)
def test_normalize_tool(command, tool):
    assert observed_checks.normalize_tool(command) == tool


@pytest.mark.parametrize(
    ("tool", "kind"),
    [
        ("pytest", "tests"),
        ("npm:test", "tests"),
        ("pnpm:test:unit", "tests"),
        ("ruff check", "lint"),
        ("eslint", "lint"),
        ("cargo clippy", "lint"),
        ("mypy", "types"),
        ("tsc", "types"),
        ("make check", "checks"),
        ("scripts/verify", "checks"),
        ("other", "checks"),
        ("npm:build", "build"),
    ],
)
def test_check_kind(tool, kind):
    assert observed_checks.check_kind(tool) == kind


def test_command_is_redacted_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    # content-guard: allow bare-token-prefix
    command = 'curl -H "Authorization: Bearer ghp_' + "A" * 36 + '" ' + str(tmp_path / "home" / "x")
    command += " && pytest " + "x" * 2000
    stored = observed_checks.redact_command(command)
    assert "ghp_" not in stored and str(tmp_path / "home") not in stored and len(stored) <= 1000


def test_append_and_read_round_trip(tmp_path):
    check = _check(tmp_path)
    assert observed_checks.append_check(tmp_path, check)
    assert observed_checks.read_checks(tmp_path) == ([check], 0)
    assert check.tool == "pytest" and check.command == "pytest -q"


def test_rotation_keeps_one_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(observed_checks, "ROTATE_BYTES", 200)
    checks = [replace(_check(tmp_path), command=f"pytest {i}") for i in range(10)]
    for check in checks:
        assert observed_checks.append_check(tmp_path, check)
    path = tmp_path / observed_checks.OBSERVED_DIR_REL / observed_checks.CHECKS_FILE
    assert path.exists() and path.with_name(path.name + ".1").exists()
    assert not path.with_name(path.name + ".2").exists()
    assert observed_checks.read_checks(tmp_path) == (checks[-2:], 0)


def test_read_skips_corrupt_lines_and_counts_them(tmp_path):
    check = _check(tmp_path)
    observed_checks.append_check(tmp_path, check)
    path = tmp_path / observed_checks.OBSERVED_DIR_REL / observed_checks.CHECKS_FILE
    with path.open("ab") as stream:
        stream.write(b'{"half": \nnull\n[]\n\xff\n')
        stream.write((json.dumps(check.to_dict()) + "\n").encode())
        stream.write(b'{"partial":')
    assert observed_checks.read_checks(tmp_path) == ([check, check], 5)


@pytest.mark.parametrize("value", [None, "", [], {}, 1, True])
def test_record_reader_rejects_unexpected_shapes(tmp_path, value):
    assert observed_checks.ObservedCheck.from_dict(value) is None


def test_record_reader_validates_every_field(tmp_path):
    data = _check(tmp_path).to_dict()
    for key in data:
        missing = dict(data)
        del missing[key]
        assert observed_checks.ObservedCheck.from_dict(missing) is None
        wrong = dict(data, **{key: []})
        assert observed_checks.ObservedCheck.from_dict(wrong) is None
    for key, value in [
        ("status", "passed"),
        ("schema", "other"),
        ("model_source", "guess"),
        ("exit_code", True),
        ("duration_ms", -1),
        ("command", ""),
        ("ts", "yesterday"),
    ]:
        assert observed_checks.ObservedCheck.from_dict(dict(data, **{key: value})) is None


def test_append_failure_returns_false_without_raising(tmp_path):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade/work").write_text("blocked")
    assert not observed_checks.append_check(tmp_path, _check(tmp_path))


def test_observed_file_is_not_a_receipt(tmp_path):
    observed_checks.append_check(tmp_path, _check(tmp_path))
    assert not (tmp_path / ".brigade/work/verify-runs").exists()
    assert (
        not {"digests", "signature", "previous", "output", "output_hash", "output_size"}
        & _check(tmp_path).to_dict().keys()
    )


def test_observed_file_passes_guard_scan_and_is_gitignored(tmp_path):
    # content-guard: allow bare-token-prefix
    command = 'pytest -H "Authorization: Bearer ghp_' + "A" * 36 + '"'
    observed_checks.append_check(tmp_path, _check(tmp_path, command))
    path = tmp_path / observed_checks.OBSERVED_DIR_REL / observed_checks.CHECKS_FILE
    assert guard.scan_text(path.read_text()).findings == []
    assert ".brigade/work/" in build_gitignore_block(Selection(depth="workspace", harnesses=("codex",)))


def test_private_creation_and_existing_mode_preserved(tmp_path):
    path = tmp_path / "checks.jsonl"
    assert observed_checks.append_jsonl(path, {"one": 1})
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o600)
    before = stat.S_IMODE(path.stat().st_mode)
    assert observed_checks.append_jsonl(path, {"two": 2})
    assert stat.S_IMODE(path.stat().st_mode) == before


def test_rotation_lock_and_stale_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(observed_checks, "ROTATE_BYTES", 1)
    path = tmp_path / "checks.jsonl"
    observed_checks.append_jsonl(path, {"one": 1})
    lock = path.with_name(path.name + ".rotate.lock")
    lock.touch()
    assert observed_checks.append_jsonl(path, {"two": 2})
    assert not path.with_name(path.name + ".1").exists()
    os.utime(lock, (time.time() - 120,) * 2)
    assert observed_checks.append_jsonl(path, {"three": 3})
    assert not lock.exists()
    assert observed_checks.read_jsonl(path) == ([{"one": 1}, {"two": 2}, {"three": 3}], 0)


def _append_many(path):
    return all(observed_checks.append_jsonl(Path(path), {"index": i}) for i in range(30))


def test_concurrent_writers_never_corrupt_lines(tmp_path):
    path = tmp_path / "checks.jsonl"
    with ProcessPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(_append_many, [str(path)] * 4))
    records, skipped = observed_checks.read_jsonl(path)
    assert len(records) == 120 and skipped == 0


def test_missing_file_reads_empty(tmp_path):
    assert observed_checks.read_checks(tmp_path) == ([], 0)


def test_deeply_nested_corrupt_line_is_skipped(tmp_path, monkeypatch):
    path = tmp_path / "checks.jsonl"
    path.write_bytes(b'{"nested":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}\n")

    def decoder_limit(_line):
        raise RecursionError("decoder nesting limit")

    monkeypatch.setattr(observed_checks.json, "loads", decoder_limit)
    assert observed_checks.read_jsonl(path) == ([], 1)


def test_direct_record_command_is_redacted_before_append(tmp_path):
    # content-guard: allow bare-token-prefix
    raw = 'pytest -H "Authorization: Bearer ghp_' + "A" * 36 + '"'
    check = replace(_check(tmp_path), command=raw)
    assert observed_checks.append_check(tmp_path, check)
    stored, skipped = observed_checks.read_checks(tmp_path)
    assert skipped == 0
    assert stored[0].command == observed_checks.redact_command(raw)
    assert "ghp_" not in stored[0].command
