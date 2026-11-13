"""Storage boundary regressions for observed checks."""

import hashlib
import json
import os
import secrets
import stat
import subprocess
from dataclasses import replace

import pytest

from brigade import agent_identity, observed_checks as checks
from tests._home import set_home


COMMAND = "pytest " + f"{secrets.randbelow(10000):04d}"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path_factory, monkeypatch):
    tmp_path = tmp_path_factory.mktemp("identity-state")
    set_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))


def record(target, **kwargs):
    return checks.build_check(
        target=target,
        command=COMMAND,
        status="ok",
        identity=agent_identity.AgentIdentity("codex", "env", "model-x", "env"),
        session_fingerprint="a" * 16,
        **kwargs,
    )


def test_workspace_digest(tmp_path):
    first = record(tmp_path / "one")
    assert first.command_digest == record(tmp_path / "one").command_digest
    assert first.command_digest != record(tmp_path / "two").command_digest
    assert first.command_digest != hashlib.sha256(checks.redact_command(COMMAND).encode()).hexdigest()[:16]


@pytest.mark.parametrize("value", ["first", "password-value", "token-value"])
def test_attached_password_masked(value):
    first = value + secrets.token_hex(8)
    second = secrets.token_hex(8)
    assert checks.redact_command("mysql -p" + first) == checks.redact_command("mysql -p" + second)


def test_key_location_mode_and_failure(tmp_path, monkeypatch):
    record(tmp_path)
    path = checks.digest_key_path(tmp_path)
    assert path is not None and not path.is_relative_to(tmp_path)
    assert len(path.read_bytes()) == 32
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    monkeypatch.setattr(checks, "digest_key_path", lambda _: None)
    check = record(tmp_path)
    assert check.command_digest == checks.DIGEST_UNAVAILABLE
    assert checks.append_check(tmp_path, check)
    assert checks.read_checks(tmp_path) == ([check], 0)


@pytest.mark.parametrize("field", ["harness", "model", "agent_type"])
def test_identity_build_and_reader(tmp_path, field):
    check = record(tmp_path)
    identity = agent_identity.AgentIdentity("codex", "env", "model-x", "env")
    kwargs = dict(
        target=tmp_path,
        command="pytest",
        status="ok",
        identity=replace(identity, **{field: "bad/value"}),
        session_fingerprint="a" * 16,
    )
    assert getattr(checks.build_check(**kwargs), field) == "unknown"
    assert checks.ObservedCheck.from_dict(dict(check.to_dict(), **{field: "bad/value"})) is None


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("session_fingerprint", "raw"),
        ("session_fingerprint", "A" * 16),
        ("tree_fingerprint", "raw"),
        ("tree_fingerprint", "A" * 40),
    ],
)
def test_fingerprint_rejected(tmp_path, monkeypatch, field, bad):
    check = record(tmp_path)
    assert checks.ObservedCheck.from_dict(dict(check.to_dict(), **{field: bad})) is None
    if field == "tree_fingerprint":
        monkeypatch.setattr(checks.localio, "tree_fingerprint", lambda _: bad)
    with pytest.raises(ValueError):
        checks.build_check(
            target=tmp_path,
            command="pytest",
            status="ok",
            identity=agent_identity.AgentIdentity("codex", "env", "model-x", "env"),
            session_fingerprint=bad if field == "session_fingerprint" else "a" * 16,
        )


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "device"])
@pytest.mark.parametrize("name", ["checks.jsonl", "checks.jsonl.1"])
def test_refuse_special_files(tmp_path, kind, name):
    path = tmp_path / "observed" / "checks.jsonl"
    path.parent.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    before = outside.stat().st_mode
    entry = path.with_name(name)
    try:
        if kind == "symlink":
            entry.symlink_to(outside)
        elif kind == "hardlink":
            os.link(outside, entry)
        elif kind == "fifo":
            if not hasattr(os, "mkfifo"):
                pytest.skip("platform cannot create FIFOs")
            os.mkfifo(entry)
        elif kind == "device":
            if not hasattr(os, "mknod"):
                pytest.skip("platform cannot create devices")
            os.mknod(entry, stat.S_IFCHR | 0o600, os.makedev(1, 3))
        else:
            entry.mkdir()
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform cannot create {kind}: {exc}")
    assert not checks.append_jsonl(path, {"record": 1})
    assert outside.read_text() == "untouched" and outside.stat().st_mode == before
    assert checks.read_jsonl(path)[1] >= 1


def test_symlinked_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "observed"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform cannot create directory symlinks: {exc}")
    before = outside.stat().st_mode
    assert not checks.append_jsonl(link / "checks.jsonl", {"record": 1})
    assert list(outside.iterdir()) == [] and outside.stat().st_mode == before


def test_rotation_preserves_unrelated_generation(tmp_path, monkeypatch):
    path = tmp_path / "checks.jsonl"
    assert checks.append_jsonl(path, {"record": 1})
    older = path.with_name(path.name + ".1")
    older.write_text("unrelated")
    monkeypatch.setattr(checks, "ROTATE_BYTES", 1)
    assert not checks.append_jsonl(path, {"record": 2})
    assert older.read_text() == "unrelated"


@pytest.mark.parametrize("case", ["file", "line", "field", "count"])
def test_reader_bounds(tmp_path, monkeypatch, case):
    check = record(tmp_path)
    path = tmp_path / checks.OBSERVED_DIR_REL / checks.CHECKS_FILE
    assert checks.append_check(tmp_path, check)
    if case == "file":
        monkeypatch.setattr(checks, "MAX_FILE_BYTES", 10)
    elif case == "line":
        monkeypatch.setattr(checks, "MAX_LINE_BYTES", 10)
    elif case == "count":
        monkeypatch.setattr(checks, "MAX_RECORDS", 1)
        assert checks.append_check(tmp_path, check)
    else:
        path.write_text(json.dumps(dict(check.to_dict(), model="x" * 10000)) + "\n")
    records, skipped = checks.read_checks(tmp_path)
    assert skipped >= 1
    assert len(records) <= (1 if case == "count" else 0)


def test_brownfield_ignore(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert checks.append_check(tmp_path, record(tmp_path))
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    result = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"], check=True, capture_output=True, text=True
    )
    assert not result.stdout
    ignore = tmp_path / checks.OBSERVED_DIR_REL / ".gitignore"
    assert ignore.read_bytes() == b"*\n"
    if os.name != "nt":
        assert stat.S_IMODE(ignore.stat().st_mode) == 0o600


@pytest.mark.parametrize("operation", ["read", "create"])
def test_key_io_failure_still_records(tmp_path, monkeypatch, operation):
    if operation == "read":
        record(tmp_path)
    original = checks._open_regular

    def denied(parent, name, flags):
        if name.endswith(".key"):
            raise PermissionError("key access refused")
        return original(parent, name, flags)

    monkeypatch.setattr(checks, "_open_regular", denied)
    check = record(tmp_path)
    assert check.command_digest == checks.DIGEST_UNAVAILABLE
    assert checks.append_check(tmp_path, check)
    assert checks.read_checks(tmp_path) == ([check], 0)


def test_existing_ignore_is_never_overwritten(tmp_path):
    path = tmp_path / "observed" / "checks.jsonl"
    assert checks.append_jsonl(path, {"record": 1})
    ignore = path.parent / ".gitignore"
    before = ignore.stat().st_mtime_ns
    assert checks.append_jsonl(path, {"record": 2})
    assert ignore.stat().st_mtime_ns == before
    ignore.write_text("foreign\n")
    assert not checks.append_jsonl(path, {"record": 3})
    assert ignore.read_text() == "foreign\n"


def test_rotation_refuses_foreign_json_generation(tmp_path, monkeypatch):
    check = record(tmp_path)
    assert checks.append_check(tmp_path, check)
    path = tmp_path / checks.OBSERVED_DIR_REL / (checks.CHECKS_FILE + ".1")
    path.write_text('{"foreign": true}\n')
    monkeypatch.setattr(checks, "ROTATE_BYTES", 1)
    assert not checks.append_check(tmp_path, check)
    assert path.read_text() == '{"foreign": true}\n'


def test_key_creation_does_not_require_readable_write_handle(tmp_path, monkeypatch):
    original = checks._open_regular

    def write_only_creation(parent, name, flags):
        if name.endswith(".key") and flags & os.O_CREAT:
            flags = (flags & ~os.O_RDWR) | os.O_WRONLY
        return original(parent, name, flags)

    monkeypatch.setattr(checks, "_open_regular", write_only_creation)
    first = record(tmp_path)
    assert first.command_digest != checks.DIGEST_UNAVAILABLE
    assert first.command_digest == record(tmp_path).command_digest


def test_rotation_does_not_leave_an_open_generation_delete_pending(tmp_path, monkeypatch):
    path = tmp_path / "checks.jsonl"
    monkeypatch.setattr(checks, "ROTATE_BYTES", 1)
    assert checks.append_jsonl(path, {"record": 1})
    assert checks.append_jsonl(path, {"record": 2})
    unlink = checks.dirfd.unlink_child
    replace_children = checks.dirfd.replace_children
    pending = False

    def mark_delete_pending(parent, name, **kwargs):
        nonlocal pending
        if name == path.name + ".1":
            pending = True
            return None
        return unlink(parent, name, **kwargs)

    def rename(parent, source, destination, **kwargs):
        if pending and destination == path.name + ".1":
            raise PermissionError("generation still has an open delete-pending handle")
        return replace_children(parent, source, destination, **kwargs)

    monkeypatch.setattr(checks.dirfd, "unlink_child", mark_delete_pending)
    monkeypatch.setattr(checks.dirfd, "replace_children", rename)
    assert checks.append_jsonl(path, {"record": 3})
    assert checks.read_jsonl(path) == ([{"record": 2}, {"record": 3}], 0)
