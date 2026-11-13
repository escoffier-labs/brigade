"""Third-round connector regressions."""

import asyncio
import json
import secrets
from dataclasses import replace
from pathlib import Path

import pytest

from brigade.dot import connector_registry as registry, lifecycle, token_auth
from brigade.dot.contracts import DotError
from brigade.dot.profile import CLOUD_AGENT, DOT, DUET
from brigade.dot.runtime_config import CLOUD_AGENT_RUNTIME_SCHEMA, parse_runtime
from tests.dot_support import requires_local_state
from tests.test_connector_review_fixes import runtime_file, fleet_home
from tests.test_cloud_agent_profile import cloud_document
from tests.test_dot_lifecycle import config, request

pytestmark = requires_local_state
# Importing the fixture makes its isolated fleet environment available here.
assert fleet_home


def test_preview_then_apply(dot_private_dir):
    tmp_path = dot_private_dir.parent
    path = runtime_file(tmp_path)
    token = tmp_path / "listener-token"
    token.unlink()
    preview = registry.preview_setup(tmp_path, "cloud-agent", runtime_path=path)
    assert str(token) in preview["writes"] and not token.exists()
    registry.apply_setup(tmp_path, "cloud-agent", runtime_path=path)
    assert token.is_file()


def test_failed_setup_leaves_no_token(dot_private_dir):
    tmp_path = dot_private_dir.parent
    path = runtime_file(tmp_path)
    token = tmp_path / "listener-token"
    token.unlink()
    with pytest.raises(registry.PackError, match="duplicate-port"):
        registry.apply_setup(tmp_path, "cloud-agent", runtime_path=path, bind="127.0.0.1:8779")
    assert not token.exists()


@pytest.mark.parametrize("document", [[], {"auth": []}])
def test_malformed_setup(tmp_path, document):
    path = runtime_file(tmp_path)
    path.write_text(json.dumps(document))
    with pytest.raises(registry.PackError):
        registry.apply_setup(tmp_path, "cloud-agent", runtime_path=path)


@pytest.mark.parametrize("credential", ["node", "admin", "hub"])
@pytest.mark.parametrize("source", ["environment", "file"])
def test_privileged_rotation(tmp_path, monkeypatch, credential, source):
    data = cloud_document(tmp_path)
    runtime = parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    verifier = token_auth.TokenVerifier(runtime.auth, hub_token_file=runtime.hub.message_token_file)
    value = Path(data["auth"]["token_file"]).read_bytes()
    scope = {"headers": [(b"authorization", b"Bearer " + value)]}
    asyncio.run(verifier.verify(scope))
    if credential == "hub":
        runtime.hub.message_token_file.write_bytes(value)
    elif source == "file":
        home = tmp_path / "fleet-home"
        home.mkdir()
        path = tmp_path / "privileged"
        path.write_bytes(value)
        path.chmod(0o600)
        key = "node_token_file" if credential == "node" else "token_file"
        (home / "fleet.toml").write_text(f'[fleet]\n{key} = "{path.as_posix()}"\n')
    else:
        monkeypatch.setenv(
            "BRIGADE_FLEET_NODE_TOKEN" if credential == "node" else "BRIGADE_FLEET_TOKEN", value.decode()
        )
    with pytest.raises(DotError, match="authentication_required"):
        asyncio.run(verifier.verify(scope))


def test_single_listener_snapshot(tmp_path, monkeypatch):
    data = cloud_document(tmp_path)
    runtime = parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    verifier = token_auth.TokenVerifier(runtime.auth, hub_token_file=runtime.hub.message_token_file)
    original = token_auth.read_token
    calls = []

    def read(path):
        calls.append(path)
        value = original(path)
        path.write_text(secrets.token_urlsafe(48))
        return value

    monkeypatch.setattr(token_auth, "read_token", read)
    scope = {"headers": [(b"authorization", b"Bearer " + Path(data["auth"]["token_file"]).read_bytes())]}
    asyncio.run(verifier.verify(scope))
    assert len(calls) == 1


@pytest.mark.parametrize("unit", ["ab", "word", "abcdefgh"])
def test_periodic_import_refused(tmp_path, unit):
    path = tmp_path / "token"
    path.write_text(unit * 48)
    path.chmod(0o600)
    with pytest.raises(DotError):
        token_auth.read_token(path)


@pytest.mark.parametrize("mode", ["token", "access"])
def test_valid_auth_during_sustained_failures(tmp_path, monkeypatch, mode):
    from brigade.dot.access_auth import AccessVerifier, AuthError
    from tests.cloud_agent_support import _memory_document

    data = cloud_document(tmp_path)
    if mode == "access":
        data["auth"] = {**_memory_document()["auth"], "mode": "access"}
    runtime = parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    verifier = token_auth.TokenVerifier(runtime.auth) if mode == "token" else AccessVerifier(runtime.auth)

    async def verify(scope):
        if (b"authorization", b"Bearer valid") in scope["headers"]:
            return runtime.auth.approved_email
        raise AuthError()

    credential = b"Bearer valid"
    if mode == "access":
        monkeypatch.setattr(verifier, "verify", verify)
    else:
        credential = b"Bearer " + Path(data["auth"]["token_file"]).read_bytes()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    gate = lifecycle._DotGate(app, config(), verifier, profile=CLOUD_AGENT)
    for index in range(40):
        blocked_until = gate._auth_blocked_until
        assert asyncio.run(request(gate))[0] == (401 if index < 10 else 429)
        if index >= 10:
            assert gate._auth_blocked_until == blocked_until
        assert (
            asyncio.run(request(gate, headers=[(b"host", b"dot.example.test"), (b"authorization", credential)]))[0]
            == 200
        )


def test_legacy_manual_token_retains_audience_check(tmp_path, monkeypatch):
    from tests.test_dot_runtime import _document
    from tests.test_duet_profile import _duet_document
    from brigade.dot.runtime_config import DUET_RUNTIME_SCHEMA

    left = parse_runtime(_document())
    right = parse_runtime({**_duet_document(), "schema": DUET_RUNTIME_SCHEMA}, schema=DUET_RUNTIME_SCHEMA)
    left = replace(left, auth=replace(left.auth, mode="token", audience=right.auth.audience))
    monkeypatch.setattr(lifecycle, "PROFILES", {"dot": DOT, "duet": DUET})
    monkeypatch.setattr(lifecycle, "_instance_path", lambda *a: tmp_path)
    monkeypatch.setattr(lifecycle, "_load_config", lambda *a, **k: (None, right))
    assert lifecycle._isolated_from_dot(tmp_path, left) is False


def test_privileged_snapshot_cannot_pass_after_replacement(tmp_path, monkeypatch):
    data = cloud_document(tmp_path)
    runtime = parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    verifier = token_auth.TokenVerifier(runtime.auth, hub_token_file=runtime.hub.message_token_file)
    listener = Path(data["auth"]["token_file"])
    original = token_auth.read_token
    privileged = runtime.hub.message_token_file.read_bytes()
    safe = secrets.token_urlsafe(48)
    listener.write_bytes(privileged)

    def read(path):
        value = original(path)
        path.write_text(safe)
        return value

    monkeypatch.setattr(token_auth, "read_token", read)
    with pytest.raises(DotError, match="authentication_required"):
        asyncio.run(verifier.verify({"headers": [(b"authorization", b"Bearer " + privileged)]}))


def test_short_import_refused(tmp_path):
    path = tmp_path / "token"
    path.write_text(secrets.token_urlsafe(24))
    path.chmod(0o600)
    with pytest.raises(DotError):
        token_auth.read_token(path)


def test_access_dependency_row_is_unique(dot_private_dir, monkeypatch):
    from tests.cloud_agent_support import _memory_document

    data = cloud_document(dot_private_dir.parent)
    data["auth"] = {**_memory_document()["auth"], "mode": "access"}
    runtime = parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    monkeypatch.setattr(lifecycle, "_load_config", lambda *a, **k: (config(), runtime))
    monkeypatch.setattr(lifecycle, "_probe", lambda *a, **k: (401, None))
    checks = lifecycle.doctor(dot_private_dir.parent, pack_id="cloud-agent")
    names = [row["check"] for row in checks]
    assert names.count("dependency") == names.count("access-dependency") == 1


@pytest.mark.parametrize("identity", [" CLOUD-AGENT ", None, [], "unknown"])
def test_library_profile_identity_cannot_bypass_loopback(tmp_path, monkeypatch, identity):
    from types import SimpleNamespace
    from brigade.dot.runtime_config import RUNTIME_SCHEMA

    runtime = parse_runtime(cloud_document(tmp_path), schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    runtime = replace(runtime, schema=RUNTIME_SCHEMA)
    tools = SimpleNamespace(profile=replace(CLOUD_AGENT, pack_id=identity))
    listener = replace(config(), bind_host="0.0.0.0")
    with pytest.raises(DotError, match="invalid_config"):
        lifecycle.run_listener(listener, tools, runtime)


def test_setup_rolls_back_token_after_unexpected_exception(dot_private_dir, monkeypatch):
    root = dot_private_dir.parent
    path = runtime_file(root)
    token = root / "listener-token"
    token.unlink()

    def fail(*args, **kwargs):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(registry, "_write", fail)
    with pytest.raises(RuntimeError, match="unexpected failure"):
        registry.apply_setup(root, "cloud-agent", runtime_path=path)
    assert not token.exists()
    assert not registry.instance_config_path(root, "cloud-agent").exists()


def test_relative_target_setup(dot_private_dir, monkeypatch):
    root = dot_private_dir.parent
    path = runtime_file(root)
    monkeypatch.chdir(root)
    target = Path("relative-target")
    target.mkdir(mode=0o700)
    registry.apply_setup(target, "cloud-agent", runtime_path=path)
    assert registry.load_instance_config(target, "cloud-agent")["pack_id"] == "cloud-agent"
