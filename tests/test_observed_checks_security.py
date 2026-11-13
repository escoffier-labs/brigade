"""Storage boundary regressions for observed checks."""

import hashlib
import json
import os
import secrets
import stat
from dataclasses import replace
from pathlib import Path

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
    assert not checks.append_check(tmp_path, check)


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
    assert getattr(checks.ObservedCheck.from_dict(dict(check.to_dict(), **{field: "bad/value"})), field) == "unknown"


@pytest.mark.parametrize(
    ("field", "bad"),
    [
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


@pytest.mark.parametrize("case", ["file", "line", "field", "count"])
def test_reader_bounds(tmp_path, monkeypatch, case):
    check = record(tmp_path)
    path = checks.digest_key_path(tmp_path).with_name(checks.CHECKS_FILE)
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
    assert skipped >= (0 if case == "count" else 1)
    assert len(records) <= (1 if case == "count" else 0)


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
    assert checks.read_jsonl(path) == ([{"record": 3}, {"record": 2}], 0)


def test_no_workspace_writes_and_relative_target(tmp_path, monkeypatch):
    target = tmp_path / "workspace"
    target.mkdir()
    monkeypatch.chdir(tmp_path)
    check = record(target)
    assert checks.append_check(Path("workspace"), check)
    assert checks.read_checks(target) == ([check], 0)
    assert checks.read_checks(Path("workspace")) == ([check], 0)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("kind", ["git", "public", "owner"])
def test_untrusted_state_is_unavailable(tmp_path, monkeypatch, capsys, kind):
    target = tmp_path / "workspace"
    path = checks.digest_key_path(target)
    assert path is not None
    if kind == "git":
        # State inside the workspace's own work tree.
        (target / ".git").mkdir(parents=True)
        set_home(monkeypatch, target / "home")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(target / "state"))
        monkeypatch.setenv("LOCALAPPDATA", str(target / "state"))
        assert checks.digest_key_path(target) is None
        assert checks.read_checks(target) == ([], 0)
    else:
        assert checks.command_digest("pytest", target) != checks.DIGEST_UNAVAILABLE
        if os.name == "nt":
            pytest.skip("POSIX ownership and permissions")
        if kind == "public":
            path.parent.chmod(0o750)
        else:
            original = checks.os.fstat

            def foreign(fd):
                value = original(fd)
                if stat.S_ISREG(value.st_mode) and value.st_size == 32:
                    values = list(value)
                    values[4] = value.st_uid + 1
                    return os.stat_result(values)
                return value

            monkeypatch.setattr(checks.os, "fstat", foreign)
    check = record(target)
    assert check.command_digest == check.session_fingerprint == checks.DIGEST_UNAVAILABLE
    assert not checks.append_check(target, check)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "harness,agent,model,expected",
    [
        ("codex", "main", "GPT-6.1", ("codex", "main", "gpt-6.1")),
        ("claude-code", "Explore", "claude-sonnet-4", ("claude-code", "Explore", "claude-sonnet-4")),
        ("cursor", "subagent", "composer-2.5", ("cursor", "subagent", "composer-2.5")),
        ("unknown-tool", "custom-agent", "a1b2c3d4e5f6", ("unknown", "unknown", "unknown")),
        ("codex", "Plan", "gpt-private/value", ("codex", "Plan", "unknown")),
        *[("codex", "unknown", m, ("codex", "unknown", m)) for m in ("gemini-4", "kimi-k2", "glm-5", "muse-1")],
    ],
)
def test_identity_mapping_at_every_boundary(tmp_path, harness, agent, model, expected):
    base = record(tmp_path)
    identity = agent_identity.AgentIdentity(harness, "env", model, "env", agent)
    built = checks.build_check(
        target=tmp_path, command="pytest", status="ok", identity=identity, session_fingerprint="session"
    )
    direct = replace(base, harness=harness, model=model, agent_type=agent)
    decoded = checks.ObservedCheck.from_dict(direct.to_dict())
    assert checks.append_check(tmp_path, direct)
    stored = checks.read_checks(tmp_path)[0][0]
    for item in (built, decoded, stored):
        assert (item.harness, item.agent_type, item.model) == expected


def _key_worker(args):
    target, root = args
    os.environ["XDG_CONFIG_HOME"] = root
    os.environ["LOCALAPPDATA"] = root
    return [checks.command_digest("pytest", Path(target)) for _ in range(8)]


def test_atomic_key_publication_across_processes(tmp_path, monkeypatch):
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    target = tmp_path / "workspace"
    root = str(tmp_path / "state")
    set_home(monkeypatch, tmp_path / "home")
    with ProcessPoolExecutor(max_workers=6, mp_context=get_context("spawn")) as pool:
        values = list(pool.map(_key_worker, [(str(target), root)] * 6))
    digests = {value for batch in values for value in batch}
    assert checks.DIGEST_UNAVAILABLE not in digests
    assert len(digests) == 1
    assert not list(tmp_path.rglob(".key-*"))
    assert len(list(tmp_path.rglob("digest.key"))) == 1


def test_session_is_workspace_keyed_with_separate_domain(tmp_path):
    import hmac

    first = record(tmp_path)
    assert first.session_fingerprint != record(tmp_path / "other").session_fingerprint
    key = checks._digest_key(tmp_path)
    assert (
        first.session_fingerprint == hmac.new(key, b"observed-session\0" + b"a" * 16, hashlib.sha256).hexdigest()[:16]
    )


def test_leftover_temporary_key_and_workspace_records_are_ignored(tmp_path):
    target = tmp_path / "workspace"
    old = target / ".brigade/work/observed/checks.jsonl"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"obsolete")
    assert checks.command_digest("pytest", target) != checks.DIGEST_UNAVAILABLE
    key = checks.digest_key_path(target)
    (key.parent / ".key-leftover").write_bytes(b"incomplete")
    check = record(target)
    assert checks.append_check(target, check)
    assert checks.read_checks(target) == ([check], 0)
    assert old.read_bytes() == b"obsolete"


def test_state_under_an_unrelated_repository_records_and_ignores_itself(tmp_path):
    """A home directory kept in Git still records; the state directory ignores itself."""
    target = tmp_path / "workspace"
    key = checks.digest_key_path(target)
    key.parent.parent.parent.mkdir(parents=True)
    key.parent.parent.parent.chmod(0o775)
    (key.parent.parent.parent / ".git").mkdir()
    check = record(target)
    assert check.command_digest != checks.DIGEST_UNAVAILABLE
    assert checks.append_check(target, check)
    assert checks.read_checks(target) == ([check], 0)
    assert (key.parent / ".gitignore").read_bytes() == b"*\n"


def test_writable_state_ancestor_is_unavailable(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX ownership and permissions")
    target = tmp_path / "workspace"
    key = checks.digest_key_path(target)
    assert checks.command_digest("pytest", target) != checks.DIGEST_UNAVAILABLE
    for directory, mode in ((key.parent.parent, 0o770), (key.parent.parent.parent, 0o777)):
        directory.chmod(mode)
        try:
            assert checks.command_digest("pytest", target) == checks.DIGEST_UNAVAILABLE
            assert not checks.append_check(target, record(target))
        finally:
            directory.chmod(0o700)


def test_key_with_a_leftover_second_name_recovers(tmp_path):
    target = tmp_path / "workspace"
    first = checks.command_digest("pytest", target)
    key = checks.digest_key_path(target)
    os.link(key, key.with_name(".key-crashed"))
    assert checks.command_digest("pytest", target) == first != checks.DIGEST_UNAVAILABLE
    assert not key.with_name(".key-crashed").exists()


def test_failed_rotation_still_appends(tmp_path, monkeypatch):
    target = tmp_path / "workspace"
    check = record(target)
    assert checks.append_check(target, check)
    monkeypatch.setattr(checks, "ROTATE_BYTES", 1)

    def refuse(*args, **kwargs):
        raise PermissionError("in use")

    monkeypatch.setattr(checks.dirfd, "replace_children", refuse)
    assert checks.append_check(target, check)
    assert checks.read_checks(target) == ([check, check], 0)
    assert not checks.digest_key_path(target).with_name(checks.CHECKS_FILE + ".1").exists()


def test_model_variant_suffix_is_dropped():
    mapped = agent_identity.map_identity("claude-code", "claude-fable-5-2[1m]", "main")
    assert mapped["model"] == "claude-fable-5-2"
    assert agent_identity.map_identity("codex", "gpt-6[secret value]", "main")["model"] == "unknown"
