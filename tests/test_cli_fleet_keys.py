"""Offline custody through the public CLI, with disposable physical homes."""

from dataclasses import replace
import errno
import io
import json
import os
import stat
import subprocess
import sys

import pytest

from brigade import cli, fleet_mesh_crypto as crypto, fleet_mesh_keys as custody

HUB = "h_" + crypto.encode_binary(b"h" * 16)
OTHER = "h_" + crypto.encode_binary(b"j" * 16)
PUBLIC = {"v", "principal", "hub", "kid", "sig_pub", "enc_pub", "suites", "created_at", "self_sig"}


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    home = tmp_path / "private-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.setenv("BRIGADE_EXTRAS", "0")
    monkeypatch.setenv("BRIGADE_NO_UPDATE_CHECK", "1")
    return home


def run(verb, *flags, principal="node-a", hub=HUB):
    return cli.main(["fleet", "keys", verb, "--principal", principal, "--hub-id", hub, *flags])


def load(home, principal="node-a", hub=HUB):
    return custody.load_local_keys(brigade_home=home, principal=principal, hub_id=hub)


def snapshot(home):
    return {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()}


def test_public_workflow(private_home, capsys):
    assert run("init", "--json") == 0
    summary = json.loads(capsys.readouterr().out)
    first = load(private_home)
    assert summary == dict(
        principal="node-a",
        hub=HUB,
        kid=first.active.record["kid"],
        fingerprint=crypto.fingerprint(crypto.decode_binary(first.active.record["sig_pub"], 32)),
        scope="local-custody",
    )
    before = snapshot(private_home)
    for verb in ("init", "show", "show"):
        assert run(verb) == 0
        out = capsys.readouterr().out
        assert all(
            value in out
            for value in (
                "node-a",
                HUB,
                summary["kid"],
                summary["fingerprint"],
                "offline local custody; no publication or trust acceptance",
            )
        )
        assert snapshot(private_home) == before
    assert run("show", "--record") == 0
    raw = capsys.readouterr().out
    assert raw == crypto.canonical(first.active.record).decode() + "\n"
    assert set(json.loads(raw)) == PUBLIC
    assert run("rotate", "--json") == 0
    second_summary = json.loads(capsys.readouterr().out)
    second = load(private_home)
    assert second_summary["kid"] != summary["kid"]
    assert second.retry_key == first.retry_key
    assert second.retired[0].private_keys == first.active.private_keys
    crypto.verify_key_record(second.active.record, previous_record=first.active.record)
    assert run("show", "--record") == 0
    record = json.loads(capsys.readouterr().out)
    assert set(record) == PUBLIC | {"prev_sig", "prev_kid"}
    assert record == second.active.record


@pytest.mark.parametrize("verb", ["show", "rotate"])
def test_missing_never_initializes(private_home, capsys, verb):
    assert run(verb, "--json") == 1
    assert json.loads(capsys.readouterr().out)["code"] == "mesh_keys_missing"
    assert snapshot(private_home) == {}
    assert not (private_home / "fleet").exists()


@pytest.mark.parametrize(
    "principal,hub",
    [
        ("node\x1b[31m", HUB),
        ("../secret", HUB),
        ("", HUB),
        ("node-a", "mesh"),
        ("node-a", HUB + "="),
        ("node-a", "h_" + "A" * 21 + "B"),
    ],
)
@pytest.mark.parametrize("verb", ["init", "show", "rotate"])
def test_invalid_scope_before_api(private_home, monkeypatch, capsys, principal, hub, verb):
    def forbidden(**kwargs):
        pytest.fail("invalid scope reached custody")

    monkeypatch.setattr(custody, "load_local_keys", forbidden)
    monkeypatch.setattr(custody, "rotate_local_keys", forbidden)
    assert run(verb, "--json", principal=principal, hub=hub) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["code"] == "mesh_keys_identity_invalid"
    assert "\x1b" not in output.out + output.err
    assert snapshot(private_home) == {}


def test_namespace_and_no_network(private_home, monkeypatch, capsys):
    from brigade import fleet_client, fleet_client_messages
    import urllib.request

    def forbidden(*args, **kwargs):
        pytest.fail("network/settings/auth called")

    for name in ("load_fleet_settings", "load_fleet_config"):
        monkeypatch.setattr(fleet_client, name, forbidden)
    monkeypatch.setattr(fleet_client_messages, "identity", forbidden)
    monkeypatch.setattr(fleet_client_messages, "manage_principal", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    fleet = private_home / "fleet"
    fleet.mkdir(mode=0o700)
    for name in ("mesh-trust.json", "mesh-hub-identities.json"):
        (fleet / name).write_bytes(b"untouched evidence sentinel")
    retained = snapshot(private_home)
    kids = set()
    for principal, hub in [("node-a", HUB), ("agent:build", HUB), ("node-a", OTHER)]:
        assert run("init", principal=principal, hub=hub) == 0
        assert run("show", "--record", principal=principal, hub=hub) == 0
        assert run("rotate", principal=principal, hub=hub) == 0
        kids.add(load(private_home, principal, hub).active.record["kid"])
    assert len(kids) == 3
    assert all(snapshot(private_home)[p] == raw for p, raw in retained.items())
    capsys.readouterr()


def test_corrupt_never_overwrites(private_home, capsys):
    assert run("init") == 0
    path = next((private_home / "fleet" / "mesh-keys").glob("*.json"))
    path.write_bytes(b"private corrupt sentinel")
    before = snapshot(private_home)
    for verb in ("init", "show", "rotate"):
        assert run(verb, "--json") == 1
        output = capsys.readouterr()
        assert json.loads(output.out.splitlines()[-1])["code"] == "mesh_keys_invalid"
        assert "private corrupt sentinel" not in output.out + output.err
        assert snapshot(private_home) == before


@pytest.mark.parametrize("stage", ["before", "after", "cleanup", "cancel"])
def test_rotation_fault_one_commit_reconcile(private_home, monkeypatch, capsys, stage):
    assert run("init") == 0
    capsys.readouterr()
    first = load(private_home)
    real_replace, real_unlink = os.replace, os.unlink
    replacements = []

    def replaced(*args, **kwargs):
        if "src_dir_fd" not in kwargs:
            return real_replace(*args, **kwargs)
        replacements.append(args)
        if stage == "before":
            raise OSError(errno.ENOSPC, "EXCEPTION_SECRET", "/PRIVATE_PATH")
        real_replace(*args, **kwargs)
        if stage == "after":
            raise OSError(errno.EIO, "EXCEPTION_SECRET", "/PRIVATE_PATH")
        if stage == "cancel":
            raise KeyboardInterrupt("EXCEPTION_SECRET /PRIVATE_PATH")

    def unlinked(name, *args, **kwargs):
        if stage == "cleanup" and str(name).endswith(".tmp"):
            raise OSError(errno.EIO, "EXCEPTION_SECRET", "/PRIVATE_PATH")
        return real_unlink(name, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", replaced)
        fault.setattr(os, "unlink", unlinked)
        assert run("rotate", "--json") == 1
    output = capsys.readouterr()
    assert "EXCEPTION_SECRET" not in output.out + output.err
    assert "/PRIVATE_PATH" not in output.out + output.err
    assert len(replacements) == 1
    visible = load(private_home)
    assert len(visible.retired) == (0 if stage == "before" else 1)
    assert visible.retry_key == first.retry_key
    if stage != "before":
        crypto.verify_key_record(visible.active.record, previous_record=first.active.record)
    before = snapshot(private_home)
    assert run("show") == 0
    assert snapshot(private_home) == before
    assert run("init") == 0
    assert load(private_home) == visible


@pytest.mark.parametrize("point", ["write", "flush", "stderr"])
def test_output_failure_no_retry(private_home, monkeypatch, capsys, point):
    assert run("init") == 0
    capsys.readouterr()
    first = load(private_home)
    calls = []
    original = custody.rotate_local_keys

    def rotate(**kwargs):
        calls.append(1)
        return original(**kwargs)

    class Broken(io.StringIO):
        def write(self, text):
            if point != "flush":
                raise BrokenPipeError("EXCEPTION_SECRET /PRIVATE_PATH")
            return super().write(text)

        def flush(self):
            raise BrokenPipeError("EXCEPTION_SECRET /PRIVATE_PATH")

    with monkeypatch.context() as fault:
        fault.setattr(custody, "rotate_local_keys", rotate)
        fault.setattr(sys, "stdout", Broken())
        if point == "stderr":
            fault.setattr(sys, "stderr", Broken())
        assert run("rotate", "--json") == 1
    assert calls == [1]
    second = load(private_home)
    assert len(second.retired) == 1 and second.retry_key == first.retry_key
    assert "EXCEPTION_SECRET" not in "".join(capsys.readouterr())
    assert run("init") == 0
    assert load(private_home) == second


def test_secret_projection(private_home, monkeypatch, capsys):
    monkeypatch.setattr(crypto, "generate_keys", lambda: crypto.PrivateKeys(b"S" * 32, b"K" * 32))
    monkeypatch.setattr(custody.secrets, "token_bytes", lambda size: b"R" * size)
    assert run("init") == 0
    state = load(private_home)
    outputs = capsys.readouterr().out
    for flags in ((), ("--json",), ("--record",)):
        assert run("show", *flags) == 0
        outputs += capsys.readouterr().out
    for secret in (b"S" * 32, b"K" * 32, b"R" * 32):
        for value in (secret.decode(), secret.hex(), crypto.encode_binary(secret)):
            assert value not in outputs
    assert str(private_home) not in outputs
    extra = replace(state, active=replace(state.active, record={**state.active.record, "secret": "EXTRA_SECRET"}))
    monkeypatch.setattr(custody, "load_local_keys", lambda **kwargs: extra)
    assert run("show", "--record") == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported == state.active.record and set(exported) == PUBLIC


@pytest.mark.parametrize(
    "code",
    [
        "mesh_keys_busy",
        "mesh_keys_unsafe",
        "mesh_keys_platform_unsupported",
        "mesh_backend_unsupported",
        "mesh_extra_missing",
        "mesh_keys_io_error",
        "UNKNOWN_SECRET",
    ],
)
def test_fixed_error_boundary(private_home, monkeypatch, capsys, code):
    def fail(**kwargs):
        raise crypto.MeshError(code)

    monkeypatch.setattr(custody, "load_local_keys", fail)
    assert run("init", "--json") == 1
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert set(payload) == {"error", "status", "code"} and payload["status"] is None
    assert payload["code"] == (code if code != "UNKNOWN_SECRET" else "mesh_keys_failed")
    assert "UNKNOWN_SECRET" not in output.out + output.err


def test_help_without_extra(private_home):
    code = """
import sys
sys.modules['cryptography'] = None
from brigade import cli
try:
    cli.main(['fleet', 'keys', '--help'])
except SystemExit as error:
    assert error.code == 0
assert cli.main(['fleet', 'keys', 'init', '--principal', 'node-a', '--hub-id', sys.argv[1], '--json']) == 1
"""
    result = subprocess.run([sys.executable, "-c", code, HUB], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "mesh_extra_missing" in result.stdout
    assert "init" in result.stdout and "rotate" in result.stdout and "show" in result.stdout
    assert "accept" not in result.stdout and "prune" not in result.stdout


@pytest.mark.parametrize("flags", [[], ["--record", "--json"], ["--principal"], ["--home", "/PRIVATE_PATH"]])
def test_syntax(private_home, flags):
    with pytest.raises(SystemExit) as error:
        cli.main(["fleet", "keys", "show", *(["--principal", "node-a", "--hub-id", HUB] if flags else []), *flags])
    assert error.value.code == 2


@pytest.mark.parametrize("verb,create", [("init", True), ("show", False), ("rotate", None)])
def test_exact_api_call(private_home, monkeypatch, capsys, verb, create):
    assert run("init") == 0
    capsys.readouterr()
    calls = []
    real_load, real_rotate = custody.load_local_keys, custody.rotate_local_keys

    def loaded(**kwargs):
        calls.append(("load", kwargs))
        return real_load(**kwargs)

    def rotated(**kwargs):
        calls.append(("rotate", kwargs))
        return real_rotate(**kwargs)

    monkeypatch.setattr(custody, "load_local_keys", loaded)
    monkeypatch.setattr(custody, "rotate_local_keys", rotated)
    assert run(verb) == 0
    expected = dict(brigade_home=private_home, principal="node-a", hub_id=HUB)
    if create is not None:
        expected["create"] = create
    assert calls == [("rotate" if create is None else "load", expected)]


def test_post_replace_directory_flush_and_explicit_reconciliation(private_home, monkeypatch, capsys):
    assert run("init") == 0
    capsys.readouterr()
    first = load(private_home)
    replaced = []
    real_replace, real_fsync = os.replace, os.fsync

    def replace_file(*args, **kwargs):
        result = real_replace(*args, **kwargs)
        if "src_dir_fd" in kwargs:
            replaced.append(args)
        return result

    def flush(fd):
        if replaced and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "EXCEPTION_SECRET", "/PRIVATE_PATH")
        return real_fsync(fd)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", replace_file)
        fault.setattr(os, "fsync", flush)
        assert run("rotate", "--json") == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["code"] == "mesh_keys_io_error"
    assert len(replaced) == 1
    visible = load(private_home)
    assert visible.active.record["kid"] != first.active.record["kid"]
    before = snapshot(private_home)
    flushes = []

    def counted(fd):
        flushes.append(fd)
        return real_fsync(fd)

    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", counted)
        assert run("show") == 0
        assert flushes == []
        assert run("init") == 0
        assert flushes
    assert load(private_home) == visible and snapshot(private_home) == before


@pytest.mark.parametrize("unsafe", ["mode", "hardlink", "symlink"])
def test_unsafe_custody_refused_without_repair(private_home, monkeypatch, capsys, unsafe):
    assert run("init") == 0
    capsys.readouterr()
    path = next((private_home / "fleet" / "mesh-keys").glob("*.json"))
    if unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "hardlink":
        os.link(path, private_home / "duplicate")
    else:
        alias = private_home.parent / "alias"
        alias.symlink_to(private_home, target_is_directory=True)
        monkeypatch.setenv("BRIGADE_HOME", str(alias))
    before = snapshot(private_home)
    for verb in ("init", "show", "rotate"):
        assert run(verb, "--json") == 1
        output = capsys.readouterr()
        assert json.loads(output.out)["code"] == "mesh_keys_unsafe"
        assert str(private_home) not in output.out + output.err
        assert snapshot(private_home) == before
    assert path.stat().st_mode & 0o777 == (0o644 if unsafe == "mode" else 0o600)


def test_backend_unsupported_is_fixed_without_secret(private_home, monkeypatch, capsys):
    def unsupported():
        raise crypto.MeshError("mesh_backend_unsupported")

    monkeypatch.setattr(crypto, "probe_backend", unsupported)
    assert run("init", "--json") == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == dict(error="mesh_backend_unsupported", status=None, code="mesh_backend_unsupported")
    assert snapshot(private_home) == {}
