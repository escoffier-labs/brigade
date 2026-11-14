import json
import os
import stat
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from brigade import agent_identity, guard, observed_checks
from tests._home import set_home


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
        ("make unknown", "make"),
        ("npm run unknown", "npm"),
        ("cargo unknown", "cargo"),
        ("pnpm run check:invalid!", "pnpm"),
        ("make test/path", "make"),
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
    set_home(monkeypatch, tmp_path / "home")
    # content-guard: allow bare-token-prefix
    command = 'curl -H "Authorization: Bearer ghp_' + "A" * 36 + '" ' + str(tmp_path / "home" / "x")
    command += " && pytest " + "x" * 2000
    stored = observed_checks.redact_command(command)
    assert "ghp_" not in stored and str(tmp_path / "home") not in stored and len(stored) <= 1000


@pytest.fixture(autouse=True)
def isolated_digest_state(tmp_path_factory, monkeypatch):
    tmp_path = tmp_path_factory.mktemp("identity-state")
    set_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))


def test_append_and_read_round_trip(tmp_path):
    check = _check(tmp_path)
    assert observed_checks.append_check(tmp_path, check)
    assert observed_checks.read_checks(tmp_path) == ([check], 0)
    assert check.tool == "pytest" and check.command_digest == observed_checks.command_digest("pytest -q", tmp_path)


def test_rotation_keeps_one_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(observed_checks, "ROTATE_BYTES", 200)
    checks = [_check(tmp_path, command=f"pytest {i}") for i in range(10)]
    for check in checks:
        assert observed_checks.append_check(tmp_path, check)
    path = observed_checks.digest_key_path(tmp_path).with_name(observed_checks.CHECKS_FILE)
    assert path.exists() and path.with_name(path.name + ".1").exists()
    assert not path.with_name(path.name + ".2").exists()
    assert observed_checks.read_checks(tmp_path) == (list(reversed(checks[-2:])), 0)


def test_read_skips_corrupt_lines_and_counts_them(tmp_path):
    check = _check(tmp_path)
    observed_checks.append_check(tmp_path, check)
    path = observed_checks.digest_key_path(tmp_path).with_name(observed_checks.CHECKS_FILE)
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
        ("command_digest", ""),
        ("ts", "yesterday"),
    ]:
        assert observed_checks.ObservedCheck.from_dict(dict(data, **{key: value})) is None


def test_observed_file_is_not_a_receipt(tmp_path):
    observed_checks.append_check(tmp_path, _check(tmp_path))
    assert not (tmp_path / ".brigade/work/verify-runs").exists()
    assert (
        not {"digests", "signature", "previous", "output", "output_hash", "output_size"}
        & _check(tmp_path).to_dict().keys()
    )


def test_observed_file_passes_guard_scan(tmp_path):
    # content-guard: allow bare-token-prefix
    command = 'pytest -H "Authorization: Bearer ghp_' + "A" * 36 + '"'
    observed_checks.append_check(tmp_path, _check(tmp_path, command))
    path = observed_checks.digest_key_path(tmp_path).with_name(observed_checks.CHECKS_FILE)
    assert guard.scan_text(path.read_text()).findings == []


def test_private_creation_and_existing_mode_preserved(tmp_path):
    path = tmp_path / "checks.jsonl"
    assert observed_checks.append_jsonl(path, {"one": 1})
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == (stat.S_IRUSR | stat.S_IWUSR)
    path.chmod(0o600)
    before = stat.S_IMODE(path.stat().st_mode)
    assert observed_checks.append_jsonl(path, {"two": 2})
    assert stat.S_IMODE(path.stat().st_mode) == before


def test_obsolete_rotation_lock_does_not_control_writers(tmp_path, monkeypatch):
    monkeypatch.setattr(observed_checks, "ROTATE_BYTES", 1)
    path = tmp_path / "checks.jsonl"
    observed_checks.append_jsonl(path, {"one": 1})
    lock = path.with_name(path.name + ".rotate.lock")
    lock.write_text("obsolete lock")
    assert observed_checks.append_jsonl(path, {"two": 2})
    assert path.with_name(path.name + ".1").exists()
    assert observed_checks.append_jsonl(path, {"three": 3})
    assert lock.read_text() == "obsolete lock"
    assert observed_checks.read_jsonl(path) == ([{"three": 3}, {"two": 2}], 0)


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


def test_direct_record_rejects_invalid_digest(tmp_path):
    assert not observed_checks.append_check(tmp_path, replace(_check(tmp_path), command_digest="raw"))


def test_legacy_command_records_are_skipped(tmp_path):
    check = _check(tmp_path)
    path = observed_checks.digest_key_path(tmp_path).with_name(observed_checks.CHECKS_FILE)
    legacy = check.to_dict()
    legacy["command"] = "pytest"
    assert observed_checks.append_jsonl(path, legacy)
    del legacy["command_digest"]
    assert observed_checks.append_jsonl(path, legacy)
    assert observed_checks.append_check(tmp_path, check)
    assert observed_checks.read_checks(tmp_path) == ([check], 2)
    assert observed_checks.read_jsonl(path) == ([check.to_dict()], 2)


def test_digest_hashes_only_redacted_input(tmp_path, monkeypatch):
    import hashlib
    import hmac

    key = bytes(range(32))
    monkeypatch.setattr(observed_checks, "_digest_key", lambda _: key)
    monkeypatch.setattr(observed_checks, "redact_command", lambda _: "masked")
    assert _check(tmp_path, "raw").command_digest == hmac.new(key, b"masked", hashlib.sha256).hexdigest()[:16]


def test_secret_value_changes_preserve_digest(tmp_path):
    assert observed_checks.command_digest("TOKEN=one pytest", tmp_path) == observed_checks.command_digest(
        "TOKEN=two pytest", tmp_path
    )


@pytest.mark.parametrize(
    "command,tool,kind",
    [
        ("python -m mypy src", "mypy", "types"),
        ("python3 -m mypy src", "mypy", "types"),
        ("python -m ruff check .", "ruff check", "lint"),
        ("python -m pytest", "pytest", "tests"),
        ("uv run mypy src", "mypy", "types"),
        ("uv run ruff check .", "ruff check", "lint"),
        ("npx eslint .", "eslint", "lint"),
        ("npm run build", "npm:build", "build"),
        ("pnpm build", "pnpm:build", "build"),
        ("yarn build", "yarn:build", "build"),
    ],
)
def test_wrapped_tool_classification(tmp_path, command, tool, kind):
    check = _check(tmp_path, command)
    assert check.tool == tool
    assert observed_checks.check_kind(check.tool) == kind


def test_real_size_two_rotations_keep_newest_records(tmp_path):
    from dataclasses import replace

    check = _check(tmp_path)
    assert observed_checks.append_check(tmp_path, check)
    path = observed_checks.digest_key_path(tmp_path).with_name(observed_checks.CHECKS_FILE)
    # Bulk uses the exact serialization from real producing functions. Public
    # appends straddle each real 4 MiB boundary, without changing either limit.
    line = (json.dumps(check.to_dict(), ensure_ascii=True, allow_nan=False) + "\n").encode()
    count = observed_checks.ROTATE_BYTES // len(line) + 1
    newest = replace(check, duration_ms=123)
    for _ in range(2):
        with path.open("wb") as stream:
            stream.write(line * count)
        assert observed_checks.append_check(tmp_path, newest)
    assert observed_checks.append_check(tmp_path, replace(newest, duration_ms=456))
    records, skipped = observed_checks.read_checks(tmp_path)
    assert skipped == 0
    assert len(records) == observed_checks.MAX_RECORDS
    assert [r.duration_ms for r in records[:3]] == [456, 123, None]
    assert path.with_name(path.name + ".1").exists()
    assert not path.with_name(path.name + ".2").exists()


@pytest.mark.parametrize("generation", ["checks.jsonl", "checks.jsonl.1"])
def test_torn_generation_does_not_block_append(tmp_path, generation):
    check = _check(tmp_path)
    assert observed_checks.append_check(tmp_path, check)
    path = observed_checks.digest_key_path(tmp_path).with_name(generation)
    with path.open("wb") as stream:
        stream.write((json.dumps(check.to_dict()) + "\n").encode() + b'{"torn":')
    assert observed_checks.append_check(tmp_path, check)
    records, skipped = observed_checks.read_checks(tmp_path)
    assert len(records) == (2 if generation == "checks.jsonl" else 3)
    assert skipped == 1


def test_reader_skips_oversized_line_between_valid_records(tmp_path):
    check = _check(tmp_path)
    assert observed_checks.append_check(tmp_path, check)
    path = observed_checks.digest_key_path(tmp_path).with_name(observed_checks.CHECKS_FILE)
    with path.open("ab") as stream:
        stream.write(b"x" * (observed_checks.MAX_LINE_BYTES + 1) + b"\n")
    assert observed_checks.append_check(tmp_path, check)
    assert observed_checks.read_checks(tmp_path) == ([check, check], 1)


def test_append_does_not_open_or_parse_previous_generation(tmp_path, monkeypatch):
    check = _check(tmp_path)
    assert observed_checks.append_check(tmp_path, check)
    path = observed_checks.digest_key_path(tmp_path).with_name(observed_checks.CHECKS_FILE)
    path.with_name(path.name + ".1").write_bytes(b'{"torn":')
    original = observed_checks._open_regular

    def opening(parent, name, flags):
        assert not name.endswith(".1")
        return original(parent, name, flags)

    def decode(*args):
        pytest.fail("appending must not parse generations")

    monkeypatch.setattr(observed_checks, "_decode", decode)
    monkeypatch.setattr(observed_checks, "_open_regular", opening)
    assert observed_checks.append_check(tmp_path, check)
