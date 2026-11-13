"""Regression coverage for connector review findings."""

from tests.dot_support import requires_local_state

import asyncio
import json
from pathlib import Path

import pytest

from brigade.dot import connector_registry as registry, lifecycle
from brigade.dot.contracts import DotError
from brigade.dot.profile import CLOUD_AGENT
from brigade.dot.runtime_config import CLOUD_AGENT_RUNTIME_SCHEMA, parse_runtime
from brigade.dot.token_auth import TokenVerifier
from tests.test_cloud_agent_profile import cloud_document
from tests.test_dot_lifecycle import config, request
from tests.cloud_agent_support import _memory_document


@pytest.fixture(autouse=True)
def fleet_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "fleet-home"))
    for key in ("BRIGADE_FLEET_TOKEN", "BRIGADE_FLEET_NODE_TOKEN"):
        monkeypatch.delenv(key, raising=False)


def runtime_file(root, mode="token"):
    data = cloud_document(root)
    if mode == "access":
        data["auth"] = {**_memory_document()["auth"], "mode": "access"}
    path = root / "runtime.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("mode", ["token", "access"])
@requires_local_state
def test_nonloopback_refused_at_setup_and_load(dot_private_dir, mode):
    root = dot_private_dir.parent
    path = runtime_file(root, mode)
    with pytest.raises(registry.PackError, match="unsafe-bind"):
        registry.apply_setup(
            root, "cloud-agent", runtime_path=path, bind="0.0.0.0:8783", allowed_hosts=["example.test:8783"]
        )
    registry.apply_setup(root, "cloud-agent", runtime_path=path)
    with pytest.raises(DotError, match="invalid_config"):
        lifecycle._load_config(root, profile=CLOUD_AGENT, bind="0.0.0.0:8783", allowed_hosts=["example.test:8783"])


@pytest.mark.parametrize("credential", ["node", "admin", "hub"])
@pytest.mark.parametrize("alias", ["path", "hardlink", "content"])
def test_credential_isolation(tmp_path, credential, alias, monkeypatch):
    data = cloud_document(tmp_path)
    listener = Path(data["auth"]["token_file"])
    other = tmp_path / "privileged"
    other.write_bytes(listener.read_bytes())
    other.chmod(0o600)
    if alias == "path":
        data["auth"]["token_file"] = str(other)
    elif alias == "hardlink":
        listener.unlink()
        listener.hardlink_to(other)
    if credential == "hub":
        data["hub"]["message_token_file"] = str(other)
    else:
        home = tmp_path / "fleet-home"
        home.mkdir()
        key = "node_token_file" if credential == "node" else "token_file"
        (home / "fleet.toml").write_text(f'[fleet]\n{key} = "{other.as_posix()}"\n')
    with pytest.raises(DotError, match="invalid_config"):
        parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)


def test_rotation_rechecks_isolation(tmp_path):
    data = cloud_document(tmp_path)
    runtime = parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    verifier = TokenVerifier(runtime.auth, hub_token_file=runtime.hub.message_token_file)
    value = Path(data["auth"]["token_file"])
    scope = {"headers": [(b"authorization", b"Bearer " + value.read_bytes())]}
    asyncio.run(verifier.verify(scope))
    value.write_bytes(runtime.hub.message_token_file.read_bytes())
    with pytest.raises(DotError, match="authentication_required"):
        asyncio.run(verifier.verify({"headers": [(b"authorization", b"Bearer " + value.read_bytes())]}))


@requires_local_state
def test_setup_generates_private_token(dot_private_dir, capsys):
    root = dot_private_dir.parent
    path = runtime_file(root)
    token = root / "listener-token"
    token.unlink()
    result = registry.apply_setup(root, "cloud-agent", runtime_path=path)
    assert token.is_file() and token.stat().st_mode & 0o777 == 0o600
    assert len(token.read_bytes()) >= 32
    assert str(token) in result["writes"]
    assert token.read_text() not in capsys.readouterr().out


def test_repeated_token_refused(tmp_path):
    data = cloud_document(tmp_path)
    Path(data["auth"]["token_file"]).write_text("a" * 40)
    with pytest.raises(DotError, match="invalid_config"):
        parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)


@pytest.mark.parametrize("mode", ["token", "access"])
def test_listener_failure_limit(tmp_path, monkeypatch, mode):
    from brigade.dot.access_auth import AccessVerifier

    data = cloud_document(tmp_path)
    if mode == "access":
        data["auth"] = {**_memory_document()["auth"], "mode": "access"}
    runtime = parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    now = [100.0]
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: now[0])
    records = []

    async def app(scope, receive, send):
        pytest.fail("unauthenticated request reached app")

    gate = lifecycle._DotGate(
        app,
        config(),
        TokenVerifier(runtime.auth) if mode == "token" else AccessVerifier(runtime.auth),
        lambda *a, **k: records.append((a, k)),
        profile=CLOUD_AGENT,
    )
    for _ in range(10):
        assert asyncio.run(request(gate))[0] == 401
    assert asyncio.run(request(gate, client=("127.0.0.2", 2)))[0] == 429
    # A cooldown gets one audit row however many requests fail in it, and failures do not extend it.
    assert len(records) == 11
    for _ in range(5):
        assert asyncio.run(request(gate))[0] == 429
    assert len(records) == 11
    assert all(row[1]["error_code"] == "authentication_required" for row in records)
    now[0] += 61
    assert asyncio.run(request(gate))[0] == 401


@pytest.mark.parametrize("name", ["dot", "duet", "cloud-agent"])
@requires_local_state
def test_canonical_install_remove(dot_private_dir, name):
    from tests.test_dot_runtime import _document
    from brigade.dot.profile import profile_for

    root = dot_private_dir.parent
    path = runtime_file(root)
    if name != "cloud-agent":
        from tests.test_duet_profile import _duet_document

        data = _document() if name == "dot" else _duet_document()
        if name == "duet":
            from brigade.dot.runtime_config import DUET_RUNTIME_SCHEMA

            data["schema"] = DUET_RUNTIME_SCHEMA
        path.write_text(json.dumps(data))
    registry.apply_setup(root, name, runtime_path=path)
    units = root / "units"
    registry.apply_install_service(root, name, out_dir=units)
    unit = units / profile_for(name).unit_name
    assert unit.exists()
    registry.apply_remove(root, name, unit_dir=units)
    assert not unit.exists()


@pytest.mark.parametrize(
    "bad_json", ["{", "{}", "[]", json.dumps({"schema": registry.INSTANCE_SCHEMA, "pack_id": "dot"})]
)
@requires_local_state
def test_bad_neighbor_list_and_selected_show(dot_private_dir, bad_json):
    root = dot_private_dir.parent
    path = runtime_file(root)
    registry.apply_setup(root, "cloud-agent", runtime_path=path)
    bad = registry.instance_config_path(root, "dot")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(bad_json)
    with pytest.raises(DotError, match="not_isolated_from_dot"):
        lifecycle._isolated_from_dot(root, lifecycle._load_config(root, profile=CLOUD_AGENT)[1])
    assert registry.show_pack("cloud-agent", root)["installed_bind"] == "127.0.0.1:8783"
    assert next(p for p in registry.list_packs(root) if p["id"] == "dot")["status"] == "invalid"


@requires_local_state
def test_doctor_config_failure_is_not_dependency(dot_private_dir, monkeypatch):
    root = dot_private_dir.parent
    monkeypatch.setattr(lifecycle.grokbot_mcp, "_load_mcp", lambda: None)
    checks = lifecycle.doctor(root, pack_id="cloud-agent")
    assert {"check": "dependency", "status": "ok"} in checks
    assert {"check": "config", "status": "fail"} in checks


@requires_local_state
def test_registry_converts_isolation_error(dot_private_dir, monkeypatch):
    root = dot_private_dir.parent
    path = runtime_file(root)

    def fail(*args):
        raise DotError("not_isolated_from_dot")

    monkeypatch.setattr(lifecycle, "_isolated_from_dot", fail)
    with pytest.raises(registry.PackError, match="not-isolated"):
        registry.apply_setup(root, "cloud-agent", runtime_path=path)


@requires_local_state
def test_legacy_flow_matches_base(dot_private_dir, monkeypatch, capsys):
    from tests.connector_legacy_capture import capture

    expected = json.loads((Path(__file__).parent / "fixtures/connector_legacy_base.json").read_text())
    assert capture(dot_private_dir.parent / "capture") == expected

    from brigade import cli, dot

    seen = []

    def build(target, **kwargs):
        seen.append((target, kwargs))
        return None, None, None

    monkeypatch.setattr(dot, "build_listener_from_target", build)
    monkeypatch.setattr(dot, "run_listener", lambda *args: None)
    for name in ("dot", "duet"):
        target = dot_private_dir.parent / "capture" / name
        assert cli.main(["run", "cloud", "grokbot", "serve", "--pack", name, "--target", str(target)]) == 0
        assert capsys.readouterr().out == ""
        assert seen[-1] == (
            target,
            {
                **({} if name == "dot" else {"pack_id": name}),
                "bind": None,
                "allowed_hosts": None,
                "allowed_origins": None,
            },
        )


@pytest.mark.parametrize("name", ["dot", "duet"])
@requires_local_state
def test_legacy_setup_skips_isolation(dot_private_dir, monkeypatch, name):
    from brigade.dot import legacy_registry as grokbot_packs
    from tests.test_dot_runtime import _document
    from tests.test_duet_profile import _duet_document

    root = dot_private_dir.parent
    path = root / "runtime.json"
    path.write_text(json.dumps(_document() if name == "dot" else _duet_document()))
    path.chmod(0o600)
    monkeypatch.setattr(lifecycle, "_isolated_from_dot", lambda *a: pytest.fail("legacy isolation"))
    grokbot_packs.apply_setup(root, name, runtime_path=path)
    with pytest.raises(grokbot_packs.PackError, match="unsafe-bind"):
        lifecycle._load_config(root, profile=lifecycle.profile_for(name), bind="0.0.0.0:8999")


@requires_local_state
def test_legacy_remove_oserror(dot_private_dir, monkeypatch):
    from brigade.dot import legacy_registry as grokbot_packs
    from brigade.dot import service_render as grokbot_ops

    root = dot_private_dir.parent
    instance = grokbot_packs.instance_config_path(root, "dot")
    instance.parent.mkdir(parents=True)
    instance.write_text("{}")
    instance.chmod(0o600)

    def fail(*args):
        raise OSError("test refusal")

    monkeypatch.setattr(grokbot_ops, "remove_regular_file", fail)
    with pytest.raises(grokbot_packs.PackError, match="unsafe-path"):
        grokbot_packs.apply_remove(root, "dot")


@pytest.mark.parametrize("mode", ["token", "access"])
@requires_local_state
def test_load_refuses_remote_override(dot_private_dir, mode):
    root = dot_private_dir.parent
    path = runtime_file(root, mode)
    registry.apply_setup(root, "cloud-agent", runtime_path=path)
    with pytest.raises(DotError, match="invalid_config"):
        lifecycle._load_config(root, profile=CLOUD_AGENT, bind="0.0.0.0:8783", allowed_hosts=["example.test:8783"])


@requires_local_state
def test_start_rechecks_rotated_token(tmp_path):
    data = cloud_document(tmp_path)
    runtime = parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    Path(data["auth"]["token_file"]).write_bytes(runtime.hub.message_token_file.read_bytes())
    with pytest.raises(DotError, match="invalid_config"):
        lifecycle.run_listener(config(), None, runtime)


@pytest.mark.parametrize("mode", ["token", "access"])
@requires_local_state
def test_loopback_probes(dot_private_dir, monkeypatch, mode):
    from types import SimpleNamespace
    from brigade.dot import watch
    from brigade.dot.access_auth import AccessVerifier

    root = dot_private_dir.parent
    path = runtime_file(root, mode)
    registry.apply_setup(root, "cloud-agent", runtime_path=path, allowed_hosts=["cloud-agent.example.test"])
    listener, runtime = lifecycle._load_config(root, profile=CLOUD_AGENT)
    verifier = (
        TokenVerifier(runtime.auth, hub_token_file=runtime.hub.message_token_file)
        if mode == "token"
        else AccessVerifier(runtime.auth)
    )

    async def app(*args):
        pytest.fail("anonymous probe reached app")

    gate = lifecycle._DotGate(app, listener, verifier, profile=CLOUD_AGENT)

    def probe(url, *, method="GET", host=None, **kwargs):
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        status, _, payload = asyncio.run(
            request(
                gate,
                path=parsed.path,
                method=method,
                headers=[
                    (
                        b"host",
                        (
                            host
                            or (
                                runtime.auth.resource.split("/")[2]
                                if mode == "access" and parsed.scheme == "https"
                                else parsed.netloc
                            )
                        ).encode(),
                    )
                ],
            )
        )
        return status, payload

    monkeypatch.setattr(lifecycle, "_probe", probe)
    monkeypatch.setattr(watch, "_probe", probe)
    monkeypatch.setattr(lifecycle, "authenticated_authority", lambda *a: None)
    monkeypatch.setattr(
        lifecycle,
        "build_tools",
        lambda *a: (SimpleNamespace(profile=CLOUD_AGENT), SimpleNamespace(close=lambda: None), None),
    )
    monkeypatch.setattr(lifecycle, "_registered_names", lambda *a: CLOUD_AGENT.tools)
    assert watch.probe_route(listener, runtime) == []
    assert {"check": "endpoint", "status": "ok"} in lifecycle.doctor(root, pack_id="cloud-agent")
    assert lifecycle.canary(root, pack_id="cloud-agent")["ok"]
    assert len(gate._auth_failures) < 10
    assert gate._auth_blocked_until == 0


@pytest.mark.parametrize("name", ["dot", "duet"])
@requires_local_state
def test_legacy_setup_with_unreadable_neighbor_runtime(dot_private_dir, name):
    from brigade.dot import legacy_registry as grokbot_packs
    from tests.test_dot_runtime import _document
    from tests.test_duet_profile import _duet_document

    root = dot_private_dir.parent
    path = root / "runtime.json"
    path.write_text(json.dumps(_document() if name == "dot" else _duet_document()))
    path.chmod(0o600)
    neighbor = root / ".brigade/grokbot/packs/grokbot-memory.json"
    neighbor.parent.mkdir(parents=True)
    neighbor.write_text(
        json.dumps(
            dict(
                schema=registry.INSTANCE_SCHEMA,
                pack_id="grokbot-memory",
                pack_version="1.0.0",
                bind="127.0.0.1:8781",
                allowed_hosts=[],
                allowed_origins=[],
                public_route="",
                runtime_path=str(root / "missing-runtime.json"),
            )
        )
    )
    neighbor.chmod(0o600)
    grokbot_packs.apply_setup(root, name, runtime_path=path)


@requires_local_state
@pytest.mark.parametrize("mode", ["token", "access"])
def test_exported_listener_refuses_remote_bind(tmp_path, monkeypatch, mode):
    import dataclasses

    data = cloud_document(tmp_path)
    if mode == "access":
        data["auth"] = {**_memory_document()["auth"], "mode": "access"}
    runtime = parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    remote = dataclasses.replace(config(), bind_host="0.0.0.0")
    started = []
    monkeypatch.setattr(lifecycle, "require_credential_isolation", lambda *a, **k: started.append("isolation"))
    with pytest.raises(DotError, match="^invalid_config$"):
        lifecycle.run_listener(remote, None, runtime)
    # The refusal comes before any credential read.
    assert started == []


def test_app_refuses_remote_bind_for_cloud_agent(tmp_path):
    import dataclasses

    pytest.importorskip("mcp", reason="requires the connector extra")
    runtime = parse_runtime(cloud_document(tmp_path), schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    remote = dataclasses.replace(config(), bind_host="0.0.0.0")
    with pytest.raises(DotError, match="^invalid_config$"):
        lifecycle.build_app(remote, None, None, TokenVerifier(runtime.auth), profile=CLOUD_AGENT)


def test_failed_token_write_leaves_no_file(tmp_path, monkeypatch):
    from brigade.dot import token_auth

    path = tmp_path / "listener-token"

    def fail(fd):
        raise OSError("no space")

    monkeypatch.setattr(token_auth.os, "fsync", fail)
    with pytest.raises(DotError, match="^invalid_config$"):
        token_auth.ensure_listener_token(path)
    assert not path.exists()


def test_failed_token_cleanup_is_reported(tmp_path, monkeypatch):
    from brigade.dot import token_auth

    path = tmp_path / "listener-token"

    def fail(*args, **kwargs):
        raise OSError("failed")

    monkeypatch.setattr(token_auth.os, "fsync", fail)
    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(token_auth.TokenCleanupError):
        token_auth.ensure_listener_token(path)


def test_token_cleanup_covers_non_os_errors(tmp_path, monkeypatch):
    from brigade.dot import token_auth

    path = tmp_path / "listener-token"

    def fail(nbytes):
        raise RuntimeError("no entropy")

    monkeypatch.setattr(token_auth.secrets, "token_urlsafe", fail)
    with pytest.raises(DotError, match="^invalid_config$"):
        token_auth.ensure_listener_token(path)
    assert not path.exists()


@requires_local_state
def test_listener_refuses_mismatched_runtime_and_tools(tmp_path, monkeypatch):
    import dataclasses
    import types

    cloud_tools = types.SimpleNamespace(profile=CLOUD_AGENT)
    other_runtime = types.SimpleNamespace(schema="brigade.other.runtime.v1", auth=None, hub=None)
    remote = dataclasses.replace(config(), bind_host="0.0.0.0")
    touched = []
    monkeypatch.setattr(lifecycle, "require_credential_isolation", lambda *a, **k: touched.append(1))
    with pytest.raises(DotError, match="^invalid_config$"):
        lifecycle.run_listener(remote, cloud_tools, other_runtime)
    with pytest.raises(DotError, match="^invalid_config$"):
        lifecycle.run_listener(config(), cloud_tools, other_runtime)
    assert touched == []
