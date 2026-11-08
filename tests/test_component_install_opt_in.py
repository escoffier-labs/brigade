"""Optional components: `brigade setup --component brigade-hub` (S10)."""

from __future__ import annotations

from pathlib import Path

import pytest

import brigade
from brigade import cli, component_install, component_manifest, component_paths, component_state
from tests.component_install_helpers import FakeOpener, all_fixture_payloads, linux_env, write_test_manifest
from tests.test_component_install import _install_fixture_manifest


def _state(env: dict[str, str]) -> component_state.InstalledState:
    roots = component_install.resolve_roots(env=env, system="linux")
    state = component_state.load_installed_state(Path(component_paths.installed_state_path(roots.data_root)))
    assert state is not None
    return state


def test_plain_setup_never_downloads_brigade_hub(tmp_path, monkeypatch):
    env, _ = _install_fixture_manifest(tmp_path, monkeypatch)
    opener = FakeOpener(all_fixture_payloads())
    assert component_install.setup_native_components(env=env, opener=opener) == 0
    assert set(_state(env).components) == set(component_manifest.DEFAULT_COMPONENT_IDS)
    assert not any("brigade-hub" in url for url in opener.calls)


def test_component_flag_installs_brigade_hub_and_a_later_plain_setup_keeps_it(tmp_path, monkeypatch):
    env, _ = _install_fixture_manifest(tmp_path, monkeypatch)
    smoked: list[list[str]] = []

    def runner(argv, **kwargs):
        smoked.append(list(argv))
        return component_install._default_smoke_runner(argv, **kwargs)

    assert (
        component_install.setup_native_components(
            env=env, opener=FakeOpener(all_fixture_payloads()), runner=runner, components=("brigade-hub",)
        )
        == 0
    )
    state = _state(env)
    assert set(state.components) == {*component_manifest.DEFAULT_COMPONENT_IDS, "brigade-hub"}
    hub = state.components["brigade-hub"]
    assert Path(hub.executable).name == "brigade-hub"
    assert [Path(argv[0]).name for argv in smoked].count("brigade-hub") == 1
    assert any(argv[1:] == ["--version"] for argv in smoked if Path(argv[0]).name == "brigade-hub")

    # A plain setup or update must not drop the hub host's binary.
    assert component_install.setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads())) == 0
    assert "brigade-hub" in _state(env).components
    assert Path(hub.executable).is_file()


def test_component_flag_refuses_a_platform_without_a_hub_asset(tmp_path, monkeypatch, capsys):
    env, _ = _install_fixture_manifest(tmp_path, monkeypatch)
    monkeypatch.setattr(component_manifest, "platform_key", lambda **_kwargs: "darwin-arm64")
    opener = FakeOpener(all_fixture_payloads(platform="darwin-arm64"))
    assert component_install.setup_native_components(env=env, opener=opener, components=("brigade-hub",)) == 1
    assert "unsupported-component-platform" in capsys.readouterr().err
    assert opener.calls == []


def test_component_flag_refuses_a_manifest_that_does_not_publish_the_hub(tmp_path, monkeypatch, capsys):
    env = linux_env(tmp_path)
    monkeypatch.setattr(component_manifest, "platform_key", lambda **_kwargs: "linux-amd64")
    # The bundled compatibility manifest carries brigade-hub with no assets.
    bundled = component_manifest.load()
    roots = component_install.resolve_roots(env=env, system="linux")
    with pytest.raises(ValueError, match="'brigade-hub' is not published"):
        component_install.build_setup_plan(bundled, platform="linux-amd64", roots=roots, opt_in={"brigade-hub"})
    with pytest.raises(ValueError, match="unknown optional component 'sessionfind'"):
        component_install.build_setup_plan(bundled, platform="linux-amd64", roots=roots, opt_in={"sessionfind"})


def test_component_and_rollback_cannot_combine(tmp_path, capsys):
    env = linux_env(tmp_path)
    assert component_install.setup_native_components(env=env, rollback=True, components=("brigade-hub",)) == 1
    assert "--component and --rollback cannot be used together" in capsys.readouterr().err


def test_dry_run_lists_the_hub_only_when_requested(tmp_path, monkeypatch, capsys):
    env, manifest_path = _install_fixture_manifest(tmp_path, monkeypatch)
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    assert component_install.setup_native_components(env=env, dry_run=True) == 0
    assert "component: brigade-hub" not in capsys.readouterr().out
    assert component_install.setup_native_components(env=env, dry_run=True, components=("brigade-hub",)) == 0
    assert "component: brigade-hub" in capsys.readouterr().out


def test_cli_forwards_component_flag(monkeypatch):
    captured: dict[str, object] = {}

    def fake_setup(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("brigade.component_install.setup_native_components", fake_setup)
    assert cli.main(["setup", "--component", "brigade-hub"]) == 0
    assert captured["components"] == ("brigade-hub",)
    captured.clear()
    assert cli.main(["setup"]) == 0
    assert "components" not in captured


def test_cli_rejects_a_component_that_is_not_optional(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["setup", "--component", "sessionfind"])
    assert raised.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
