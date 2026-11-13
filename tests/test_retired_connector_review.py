"""Review regressions using renderer output captured from origin/overhaul."""

import json
import stat
from pathlib import Path

import pytest
from brigade import cli, grokbot_packs
from brigade.dot import connector_registry as registry, lifecycle
from brigade.dot.contracts import DotError
from brigade.dot.runtime_config import DUET_RUNTIME_SCHEMA, parse_runtime
from tests.dot_support import requires_local_state
from tests.test_cloud_agent_profile import cloud_document

BASE = json.loads((Path(__file__).parent / "fixtures/retired_connector_base.json").read_text())


@pytest.mark.parametrize("name", BASE)
@pytest.mark.parametrize("route", [["run", "cloud", "grokbot", "pack"], ["hub", "connector"]])
@requires_local_state
def test_retired_remove_units_only(dot_private_dir, capsys, name, route):
    root = dot_private_dir.parent
    units = root / "units"
    units.mkdir()
    for filename, text in BASE[name]["units"].items():
        (units / filename).write_text(text)
    retained = root / ".brigade/grokbot/packs" / (name + ".json")
    retained.parent.mkdir(parents=True)
    retained.write_text(
        json.dumps(
            dict(
                schema=registry.INSTANCE_SCHEMA,
                pack_id=name,
                pack_version="1.0.0",
                bind="127.0.0.1:8781",
                allowed_hosts=[],
                allowed_origins=[],
                public_route="",
                runtime_path=(root / "runtime.json").as_posix(),
            )
        )
    )
    (root / "runtime.json").write_text(json.dumps(BASE[name]["runtime"]))
    state = root / "state"
    state.mkdir()
    (state / "db").write_bytes(b"keep state")
    (root / "token").write_bytes(b"keep token")
    (units / "unrelated.service").write_text("keep unit")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    args = route + ["remove", "--id", name, "--target", str(root), "--unit-dir", str(units)]
    assert cli.main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert {Path(p).name for p in output["paths"]} == set(BASE[name]["units"])
    assert all((units / f).exists() for f in BASE[name]["units"])
    assert cli.main(args + ["--apply"]) == 0
    assert all(not (units / f).exists() for f in BASE[name]["units"])
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == {p: v for p, v in before.items() if p not in {Path("units") / f for f in BASE[name]["units"]}}


@pytest.mark.parametrize("name", BASE)
def test_retirement_message_names_base_units(name, capsys):
    assert cli.main(["hub", "connector", "show", "--id", name]) == 2
    message = capsys.readouterr().err
    assert "systemctl --user disable --now" in message
    assert all(filename in message for filename in BASE[name]["units"])


@pytest.mark.parametrize("name", BASE)
@pytest.mark.parametrize(
    "shared", ["principal", "token-path", "token-content", "state", "audience", "resource", "unreadable", "none"]
)
@requires_local_state
def test_retained_isolation(dot_private_dir, monkeypatch, name, shared):
    root = dot_private_dir.parent
    data = cloud_document(root)
    data["schema"] = DUET_RUNTIME_SCHEMA
    data["messaging_enabled"] = False
    data["digest_pr_repo"] = "example/project"
    data["hub"]["principal"] = "agent:duet"
    from tests.cloud_agent_support import _memory_document

    data["auth"] = _memory_document()["auth"]
    runtime = parse_runtime(data, schema=DUET_RUNTIME_SCHEMA)
    other = json.loads(json.dumps(BASE[name]["runtime"]))
    other["auth"]["audience"] = "d" * 64
    other["auth"]["resource"] = "https://retained.example.test/mcp"
    # Keep the captured runtime shape, with independent local paths and credentials.
    other["paths"]["state_dir"] = (root / "other-state").as_posix()
    token = root / "other-token"
    token.write_text("independent-example-message-credential-000000000")
    token.chmod(stat.S_IRUSR | stat.S_IWUSR)
    other["hub"]["message_token_file"] = token.as_posix()
    if shared == "principal":
        other["hub"]["principal"] = data["hub"]["principal"]
    if shared == "token-path":
        other["hub"]["message_token_file"] = data["hub"]["message_token_file"]
    if shared == "token-content":
        token.write_bytes(Path(data["hub"]["message_token_file"]).read_bytes())
    if shared == "state":
        other["paths"]["state_dir"] = data["paths"]["state_dir"]
    if shared in {"audience", "resource"}:
        other["auth"][shared] = data["auth"][shared]
    path = root / "retired-runtime.json"
    path.write_text(json.dumps(other))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    instance = root / ".brigade/grokbot/packs" / (name + ".json")
    instance.parent.mkdir(parents=True)
    instance.write_text(
        json.dumps(
            dict(
                schema=registry.INSTANCE_SCHEMA,
                pack_id=name,
                pack_version="1.0.0",
                bind="127.0.0.1:8781",
                allowed_hosts=[],
                allowed_origins=[],
                public_route="",
                runtime_path=path.as_posix(),
            )
        )
    )
    instance.chmod(stat.S_IRUSR | stat.S_IWUSR)
    if shared == "unreadable":
        path.write_text("{")
    monkeypatch.setattr(
        lifecycle,
        "_load_config",
        lambda *a, **k: (type("Config", (), {"bind_host": "127.0.0.1", "bind_port": 8780})(), runtime),
    )
    monkeypatch.setattr(lifecycle, "_probe", lambda *a, **k: (401, None))
    monkeypatch.setattr(lifecycle, "authenticated_authority", lambda *a: None)
    monkeypatch.setattr(lifecycle, "build_tools", lambda *a: ("tools", None, None))
    if shared == "none":
        assert lifecycle._isolated_from_dot(root, runtime)
    else:
        with pytest.raises(DotError, match="not_isolated_from_dot"):
            lifecycle.build_listener_from_target(root, pack_id="duet")
        assert any(c["check"] == "isolation" and c["status"] == "fail" for c in lifecycle.doctor(root, pack_id="duet"))


@requires_local_state
def test_unexpected_setup_error_keeps_identity(dot_private_dir, monkeypatch):
    root = dot_private_dir.parent
    document = cloud_document(root)
    token = Path(document["auth"]["token_file"])
    token.unlink()
    runtime = root / "runtime.json"
    runtime.write_text(json.dumps(document))
    runtime.chmod(stat.S_IRUSR | stat.S_IWUSR)
    error = TypeError("example programmer error")

    def fail(*a, **k):
        raise error

    monkeypatch.setattr(registry, "_write", fail)
    with pytest.raises(TypeError) as caught:
        registry.apply_setup(root, "cloud-agent", runtime_path=runtime)
    assert caught.value is error
    assert not token.exists()


@pytest.mark.parametrize("name", ["dot", "duet"])
@requires_local_state
def test_stray_pack_json_does_not_break_setup(dot_private_dir, name):
    from tests.test_dot_runtime import _document
    from tests.test_duet_profile import _duet_document

    root = dot_private_dir.parent
    packs = root / ".brigade/grokbot/packs"
    packs.mkdir(parents=True)
    (packs / "backup.json").write_text("{")
    path = root / "runtime.json"
    path.write_text(json.dumps(_document() if name == "dot" else _duet_document()))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert grokbot_packs.apply_setup(root, name, runtime_path=path)["apply"]


def test_current_setup_references():
    root = Path(__file__).resolve().parents[1]
    assert "docs/grokbot-memory-connector.md" not in (root / "README.md").read_text()
    text = (root / "registry/skills/verify-brigade/features/grokbot-pack-lifecycle.md").read_text()
    assert "`grokbot-memory`" not in text and "`grokbot-inbox`" not in text


@pytest.mark.parametrize("name", BASE)
@requires_local_state
def test_cloud_setup_refuses_copied_retired_runtime(dot_private_dir, name):
    root = dot_private_dir.parent
    data = cloud_document(root)
    retained = json.loads(json.dumps(BASE[name]["runtime"]))
    for key in ("principal", "message_token_file"):
        retained["hub"][key] = data["hub"][key]
    retained["paths"]["state_dir"] = data["paths"]["state_dir"]
    path = root / "retired.json"
    path.write_text(json.dumps(retained))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    instance = root / ".brigade/grokbot/packs" / (name + ".json")
    instance.parent.mkdir(parents=True)
    instance.write_text(
        json.dumps(
            dict(
                schema=registry.INSTANCE_SCHEMA,
                pack_id=name,
                pack_version="1.0.0",
                allowed_hosts=[],
                allowed_origins=[],
                public_route="",
                runtime_path=path.as_posix(),
                bind="127.0.0.1:8781",
            )
        )
    )
    runtime = root / "runtime.json"
    runtime.write_text(json.dumps(data))
    runtime.chmod(stat.S_IRUSR | stat.S_IWUSR)
    with pytest.raises(registry.PackError, match="not-isolated"):
        registry.apply_setup(root, "cloud-agent", runtime_path=runtime)
    assert not registry.instance_config_path(root, "cloud-agent").exists()


@requires_local_state
def test_no_retained_instance_is_isolated(dot_private_dir):
    from brigade.dot.runtime_config import CLOUD_AGENT_RUNTIME_SCHEMA

    root = dot_private_dir.parent
    runtime = parse_runtime(cloud_document(root), schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    assert lifecycle._isolated_from_dot(root, runtime)


@pytest.mark.parametrize("name", BASE)
@requires_local_state
def test_retired_cleanup_refuses_unowned_directory(dot_private_dir, name):
    from brigade.dot.retired_instances import remove_units

    root = dot_private_dir.parent
    units = root / "units"
    units.mkdir()
    (units / f"brigade-{name}-watch.timer").write_text("unowned timer")
    with pytest.raises(registry.PackError, match="outside-owned-path"):
        remove_units(name, unit_dir=units, apply=True)
    assert list(units.iterdir())


@pytest.mark.parametrize("name", BASE)
@pytest.mark.parametrize("hostile", ["fifo", "oversized", "nested"])
@requires_local_state
def test_hostile_retained_instance_refuses_without_hanging(dot_private_dir, name, hostile):
    """A retained file that blocks, is huge or nests deeply is a clear refusal."""
    import os

    from brigade.dot.runtime_config import CLOUD_AGENT_RUNTIME_SCHEMA

    root = dot_private_dir.parent
    runtime = parse_runtime(cloud_document(root), schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    instance = root / ".brigade/grokbot/packs" / (name + ".json")
    instance.parent.mkdir(parents=True)
    if hostile == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("no FIFO on this platform")
        os.mkfifo(instance)
    elif hostile == "oversized":
        instance.write_text(json.dumps({"pad": "x" * 70_000}))
    else:
        instance.write_text("[" * 50_000)
    with pytest.raises(DotError, match="not_isolated_from_dot"):
        lifecycle._isolated_from_dot(root, runtime)
    if hostile != "fifo":
        with pytest.raises(registry.PackError, match="unsafe-path"):
            grokbot_packs._occupied_setup_ports(root, exclude_pack_id="dot")


@pytest.mark.parametrize("name", BASE)
@pytest.mark.parametrize("argv", [["--id", "duet", "--unit-dir", "units", "--apply"], []])
def test_retired_remove_without_its_own_units_prints_the_stop_instruction(tmp_path, capsys, name, argv):
    from brigade.dot.retired_instances import remove_command

    units = tmp_path / "units"
    units.mkdir()
    unit = units / f"brigade-{name}.service"
    unit.write_text("unit")
    argv = [a if a != "units" else str(units) for a in argv] or ["--id", name]
    assert remove_command(argv, name) == 2
    assert "systemctl --user disable --now" in capsys.readouterr().err
    assert unit.exists()
