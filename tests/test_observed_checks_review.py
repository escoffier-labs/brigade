"""Regression coverage for review of the observed-check storage boundary."""

import multiprocessing
import os
import secrets
import shlex
import stat
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from brigade import agent_identity, observed_checks as checks
from brigade.inbox_lock import held_file_lock
from tests._home import set_home


@pytest.fixture(autouse=True)
def isolated_digest_state(tmp_path_factory, monkeypatch):
    tmp_path = tmp_path_factory.mktemp("identity-state")
    set_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))


def _record(target, command="pytest"):
    return checks.build_check(
        target=target,
        command=command,
        status="ok",
        identity=agent_identity.AgentIdentity("codex", "env", "model-x", "env"),
        session_fingerprint=agent_identity.session_fingerprint("codex", "review"),
    )


POSITIONS = [
    "OPENAI_API_KEY={s} pytest",
    "GITHUB_TOKEN={s} make test",
    "AWS_SECRET_ACCESS_KEY={s} pytest",
    "PGPASSWORD={s} pytest",
    "env FOO={s} BAR=y pytest",
    "env FOO=y BAR={s} pytest",
    "pytest --token {s}",
    "tool --password={s}",
    'curl -H "Authorization: Bearer {raw}" url',
    "curl -u me:{s} url",
    "DATABASE_URL=postgres://u:{s}@db/app pytest",
    'pytest --token "{raw}',
    "tool --api-key {s}",
    "tool --pwd={s}",
    "tool --auth {s}",
    "tool --credential {s}",
    "tool --bearer={s}",
    "mysql -p {s}",
    'curl --header "X-Custom: {raw}" url',
    "curl https://me:{s}@db.test/a",
]


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("secret", ["x", "fake secret with spaces", "fake[REDACTED]value"])
def test_command_secrets_absent_from_storage(tmp_path, position, secret):
    command = position.format(s=shlex.quote(secret), raw=secret)
    assert checks.append_check(tmp_path, _record(tmp_path, command))
    stored, skipped = checks.read_checks(tmp_path)
    assert skipped == 0
    assert secret not in stored[0].tool
    assert "command" not in stored[0].to_dict()


def test_random_secrets_in_every_position(tmp_path):
    positions = POSITIONS + [
        "pytest --opt {s}",
        "pytest --opt={s}",
        "mysql -p{raw} -e query",
        "pytest {s}",
        "echo {s} | gh auth login --with-token",
        'bash -c "pytest {raw}"',
        "pytest <<< {s}",
        "pytest\n{s}",
        "make {s}",
        "npm run {s}",
        "cargo {s}",
        "pytest --{raw}",
        "npm run test:{raw}",
        "make test/{raw}",
        "cargo test/{raw}",
    ]
    for position in positions:
        secret = secrets.token_hex(24)
        command = position.format(s=shlex.quote(secret), raw=secret)
        assert checks.append_check(tmp_path, _record(tmp_path, command))
        path = tmp_path / checks.OBSERVED_DIR_REL / checks.CHECKS_FILE
        line = path.read_text().splitlines()[-1]
        assert secret not in line
        assert all(secret[i : i + 8] not in line for i in range(len(secret) - 7))
    stored, skipped = checks.read_checks(tmp_path)
    assert len(stored) == len(positions) and skipped == 0


@pytest.mark.parametrize("tool", ["make private", "npm:private", "cargo private", "private"])
def test_direct_record_tool_is_sanitized(tmp_path, tool):
    assert checks.append_check(tmp_path, replace(_record(tmp_path), tool=tool))
    stored, skipped = checks.read_checks(tmp_path)
    assert skipped == 0 and stored[0].tool == "other"


@pytest.mark.parametrize("value", ["fake identity secret", "x" * 81, "name/secret", None, 3])
def test_unsafe_identity_is_unknown(tmp_path, value):
    record = replace(_record(tmp_path), model=value, agent_type=value)
    assert checks.append_check(tmp_path, record)
    stored, skipped = checks.read_checks(tmp_path)
    assert skipped == 0
    assert stored[0].model == stored[0].agent_type == "unknown"


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError, OSError])
def test_rotation_error_still_appends(tmp_path, monkeypatch, error):
    path = tmp_path / "checks.jsonl"

    def fail(*_args):
        raise error("simulated rotation failure")

    monkeypatch.setattr(checks, "_rotate", fail)
    assert checks.append_jsonl(path, {"record": 1})
    assert checks.read_jsonl(path) == ([{"record": 1}], 0)


def test_short_write_does_not_consume_next_record(tmp_path, monkeypatch):
    path = tmp_path / "checks.jsonl"
    write = os.write

    def partial(fd, data):
        return write(fd, data[:5])

    with monkeypatch.context() as patch:
        patch.setattr(checks.os, "write", partial)
        assert not checks.append_jsonl(path, {"record": "first"})
    assert checks.append_jsonl(path, {"record": "second"})
    assert checks.read_jsonl(path) == ([{"record": "second"}], 1)


def _writer(args):
    path, worker = args
    checks.ROTATE_BYTES = 200_000
    return all(checks.append_jsonl(Path(path), {"id": f"{worker}:{i}", "pad": "a" * 2000}) for i in range(40))


def test_spawn_writers_rotate_without_loss(tmp_path):
    path = tmp_path / "checks.jsonl"
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        assert all(pool.map(_writer, [(str(path), i) for i in range(4)]))
    records, skipped = checks.read_jsonl(path)
    assert skipped == 0
    assert {r["id"] for r in records} == {f"{w}:{i}" for w in range(4) for i in range(40)}
    assert path.with_name(path.name + ".1").exists()


def test_reader_opens_generations_under_lock(tmp_path, monkeypatch):
    path = tmp_path / "checks.jsonl"
    assert checks.append_jsonl(path, {"record": 1})
    held = False

    @contextmanager
    def lock(*args, **kwargs):
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    original = checks.dirfd.open_child_file

    def opening(parent, source, *args, **kwargs):
        if source in (path.name, path.name + ".1"):
            assert held
        return original(parent, source, *args, **kwargs)

    monkeypatch.setattr(checks, "held_file_lock", lock, raising=False)
    monkeypatch.setattr(checks.dirfd, "open_child_file", opening)
    assert checks.read_jsonl(path) == ([{"record": 1}], 0)


def _hold_lock(path, ready, release):
    with held_file_lock(Path(path), deadline_seconds=2):
        ready.set()
        release.wait(10)


def test_lock_timeout_writes_nothing_and_counts_reader_fallback(tmp_path, monkeypatch):
    path = tmp_path / "checks.jsonl"
    assert checks.append_jsonl(path, {"record": 1})
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_hold_lock, args=(str(path) + ".lock", ready, release))
    process.start()
    try:
        assert ready.wait(10)
        monkeypatch.setattr(checks, "LOCK_SECONDS", 0.1, raising=False)
        before = path.read_bytes()
        assert not checks.append_jsonl(path, {"record": 2})
        assert path.read_bytes() == before
        assert checks.read_jsonl(path) == ([{"record": 1}], 1)
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0


def test_private_directory_and_existing_file(tmp_path):
    path = tmp_path / "observed" / "checks.jsonl"
    assert checks.append_jsonl(path, {"record": 1})
    if os.name != "nt":
        public = stat.S_IRGRP | stat.S_IROTH
        assert not path.parent.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        path.chmod(path.stat().st_mode | public)
        assert checks.append_jsonl(path, {"record": 2})
        assert not path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)


def test_stale_rotation_lock_is_never_inspected(tmp_path, monkeypatch):
    path = tmp_path / "checks.jsonl"
    assert checks.append_jsonl(path, {"record": 1})
    monkeypatch.setattr(checks, "ROTATE_BYTES", 1)
    original = Path.stat

    def stat_path(source, *args, **kwargs):
        if source.name.endswith(".rotate.lock"):
            raise OSError("obsolete mtime locking must not run")
        return original(source, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_path)
    assert checks.append_jsonl(path, {"record": 2})


def test_unreadable_parent_is_counted(tmp_path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise PermissionError("simulated inaccessible parent")

    monkeypatch.setattr(checks.dirfd, "open_directory_chain", denied)
    assert checks.read_jsonl(tmp_path / "checks.jsonl") == ([], 1)


@pytest.mark.parametrize("threshold", [1, checks.ROTATE_BYTES])
def test_existing_public_file_is_tightened(tmp_path, monkeypatch, threshold):
    monkeypatch.setattr(checks, "ROTATE_BYTES", threshold)
    path = tmp_path / "checks.jsonl"
    assert checks.append_jsonl(path, {"record": 1})
    if os.name != "nt":
        path.chmod(path.stat().st_mode | stat.S_IRGRP | stat.S_IROTH)
        assert checks.append_jsonl(path, {"record": 2})
        assert not path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        older = path.with_name(path.name + ".1")
        if older.exists():
            assert not older.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)


def test_reader_retains_generation_during_concurrent_rotation(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    path = tmp_path / "checks.jsonl"
    monkeypatch.setattr(checks, "ROTATE_BYTES", 1)
    assert checks.append_jsonl(path, {"record": "old"})
    assert checks.append_jsonl(path, {"record": "current"})
    original = checks.dirfd.open_child_file
    started = Event()
    future = None
    with ThreadPoolExecutor(max_workers=1) as pool:

        def writer():
            started.set()
            return checks.append_jsonl(path, {"record": "new"})

        def opening(parent, source, *args, **kwargs):
            nonlocal future
            if source == path.name and future is None:
                future = pool.submit(writer)
                assert started.wait(5)
                try:
                    future.result(timeout=0.2)
                except TimeoutError:
                    pass
            return original(parent, source, *args, **kwargs)

        monkeypatch.setattr(checks.dirfd, "open_child_file", opening)
        records, skipped = checks.read_jsonl(path)
        assert future is not None and future.result(timeout=5)
    assert skipped == 0
    assert {"record": "current"} in records


def test_vanished_active_file_still_appends(tmp_path, monkeypatch):
    path = tmp_path / "checks.jsonl"
    assert checks.append_jsonl(path, {"record": "old"})
    monkeypatch.setattr(checks, "ROTATE_BYTES", 1)
    original = Path.stat
    raced = False

    def stat_path(source, *args, **kwargs):
        nonlocal raced
        if source == path and not raced:
            raced = True
            value = original(source, *args, **kwargs)
            os.replace(path, path.with_name(path.name + ".1"))
            return value
        return original(source, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_path)
    assert checks.append_jsonl(path, {"record": "new"})
    assert {"record": "new"} in checks.read_jsonl(path)[0]
