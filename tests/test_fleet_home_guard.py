"""A relative BRIGADE_HOME never lets a workspace choose fleet credentials or hub_url (#2423)."""

from __future__ import annotations

import logging
import urllib.request
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from brigade import (
    fleet_admin_stats,
    fleet_claim_lifecycle,
    fleet_client,
    fleet_export,
    fleet_home_guard,
    fleet_mcp_report,
    fleet_model_admission,
    worklore_http,
)
from brigade import fleet_client_message_credentials as credentials
from brigade import fleet_client_messages as messages
from tests._home import set_home

USER_HUB = "http://192.0.2.10:8787"
WORKSPACE_HUB = "https://attacker.invalid"
FLEET_ENV = ("BRIGADE_FLEET_HUB_URL", "BRIGADE_FLEET_TOKEN", "BRIGADE_FLEET_NODE_TOKEN")


def _token(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    return path


def _fleet_toml(directory: Path, hub_url: str, *, label: str) -> None:
    """A full fleet.toml naming a hub, both token files and every other credentialed table."""
    directory.mkdir(parents=True, exist_ok=True)
    admin = _token(directory / "admin.token", f"{label}-admin-token")
    node = _token(directory / "node.token", f"{label}-node-token")
    (directory / "fleet.toml").write_text(
        "[fleet]\n"
        f"hub_url = {hub_url!r}\n"
        f"token_file = {admin.as_posix()!r}\n"
        f"node_token_file = {node.as_posix()!r}\n\n"
        "[fleet.mcp]\ninclude_account_connectors = true\n\n"
        f'[fleet.stats]\ndownloads_url = "{hub_url}/downloads"\n\n'
        f'[fleet.sink]\nenabled = true\ndolt_dir = "{label}-dolt"\n\n'
        '[fleet.worklore]\noperator_nodes = ["node-a"]\n',
        encoding="utf-8",
    )


@pytest.fixture
def no_fleet_env(monkeypatch):
    for name in FLEET_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(credentials.ENV_TOKEN_FILE, raising=False)
    monkeypatch.setattr(fleet_home_guard, "_WARNED", False)


@pytest.fixture
def hostile_workspace(tmp_path, monkeypatch, no_fleet_env):
    """A workspace holding its own `.brigade/fleet.toml`, with BRIGADE_HOME=.brigade."""
    workspace = tmp_path / "workspace"
    _fleet_toml(workspace / ".brigade", WORKSPACE_HUB, label="workspace")
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("BRIGADE_HOME", ".brigade")
    assert not Path(".brigade").is_absolute()
    return workspace


@pytest.fixture
def no_requests(monkeypatch):
    sent: list[object] = []

    def refuse(*args, **kwargs):
        sent.append(args)
        raise AssertionError("a fleet request was sent")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(fleet_client, "_post_events", refuse)
    return sent


def test_a_relative_home_is_refused_for_strict_credential_and_hub_loads(hostile_workspace, no_requests):
    assert fleet_home_guard.absolute_brigade_home() is None
    with pytest.raises(fleet_home_guard.RelativeBrigadeHomeError, match="BRIGADE_HOME must be an absolute path"):
        fleet_client.load_fleet_settings(strict=True)
    with pytest.raises(fleet_home_guard.RelativeBrigadeHomeError):
        fleet_client.load_fleet_config(strict=True)
    with pytest.raises(fleet_home_guard.RelativeBrigadeHomeError):
        fleet_home_guard.fleet_config_path(strict=True)
    assert issubclass(fleet_home_guard.RelativeBrigadeHomeError, fleet_client.FleetClientError)
    assert not no_requests


def test_a_relative_home_reads_no_workspace_hub_or_token(hostile_workspace, no_requests, caplog):
    with caplog.at_level(logging.WARNING, logger="brigade.fleet"):
        settings = fleet_client.load_fleet_settings()
        fleet_client.load_fleet_settings()
    assert settings == {"hub_url": "", "admin_token": "", "node_token": ""}
    assert fleet_client.load_fleet_config()["hub_url"] == ""
    assert fleet_client.load_fleet_config()["token"] == ""
    assert fleet_claim_lifecycle._token_snapshot() == ("", "", "")
    warnings = [record for record in caplog.records if "BRIGADE_HOME must be an absolute path" in record.message]
    assert len(warnings) == 1
    # The other credentialed tables of the workspace file are not read either.
    assert fleet_mcp_report.account_connectors_enabled() is False
    assert fleet_admin_stats.config() == {}
    assert fleet_export.load_sink_config().enabled is False
    assert worklore_http._worklore_section() == {}
    assert fleet_client.report_event({"type": "fleet-home-guard-probe"}) is False
    assert not no_requests


def test_a_relative_home_keeps_environment_credentials_but_never_the_workspace_file(
    hostile_workspace, monkeypatch, no_requests
):
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", USER_HUB)
    settings = fleet_client.load_fleet_settings()
    assert settings["hub_url"] == USER_HUB
    assert settings["node_token"] == "" and settings["admin_token"] == ""
    assert not no_requests


def test_a_relative_home_refuses_fleet_state_and_identity(hostile_workspace, no_requests):
    with pytest.raises(fleet_home_guard.RelativeBrigadeHomeError):
        fleet_client.home_identity_target()
    assert fleet_client.resolve_node_id() == "unknown"
    with pytest.raises(fleet_home_guard.RelativeBrigadeHomeError):
        fleet_client.spool_path("node-a")
    with pytest.raises(fleet_home_guard.RelativeBrigadeHomeError):
        fleet_model_admission.lkg_path()
    with pytest.raises(fleet_home_guard.RelativeBrigadeHomeError):
        messages._principal_pending_path("agent", "fixture", hub_url=USER_HUB)
    assert not no_requests


def test_a_relative_home_refuses_the_message_principal(hostile_workspace, tmp_path, monkeypatch, no_requests):
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", USER_HUB)
    principal = _token(tmp_path / "principal.token", "fixture_credential_" + "p" * 32)
    with pytest.raises(fleet_home_guard.RelativeBrigadeHomeError):
        credentials.message_settings(str(principal))
    for kwargs in ({"token_file": str(principal)}, {"node_credential": True}):
        with pytest.raises(credentials.MessageAuthorityError) as caught:
            credentials.MessageAuthority.capture(**kwargs)
        assert caught.value.code == "relative_brigade_home"
    assert not no_requests


def test_a_workspace_fleet_toml_cannot_redirect_an_absolute_home(tmp_path, monkeypatch, no_fleet_env):
    home = tmp_path / "user-home" / ".brigade"
    _fleet_toml(home, USER_HUB, label="user")
    workspace = tmp_path / "workspace"
    _fleet_toml(workspace / ".brigade", WORKSPACE_HUB, label="workspace")
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("BRIGADE_HOME", str(home))

    for strict in (False, True):
        settings = fleet_client.load_fleet_settings(strict=strict)
        assert settings["hub_url"] == USER_HUB
        assert settings["node_token"] == "user-node-token"
        assert settings["admin_token"] == "user-admin-token"
    assert fleet_claim_lifecycle._token_snapshot() == (USER_HUB, "user-node-token", "user-admin-token")
    assert fleet_home_guard.fleet_config_path(strict=True) == home / "fleet.toml"
    assert fleet_client.spool_path("node-a").parent.parent == home
    assert fleet_client.home_identity_target() == home.parent
    assert fleet_admin_stats.config() == {"downloads_url": f"{USER_HUB}/downloads"}
    assert fleet_export.load_sink_config().dolt_dir == "user-dolt"


def test_the_default_home_still_works(tmp_path, monkeypatch, no_fleet_env):
    user = tmp_path / "profile"
    _fleet_toml(user / ".brigade", USER_HUB, label="user")
    workspace = tmp_path / "workspace"
    _fleet_toml(workspace / ".brigade", WORKSPACE_HUB, label="workspace")
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("BRIGADE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: user)

    assert fleet_home_guard.absolute_brigade_home() == user / ".brigade"
    assert fleet_home_guard.fleet_home() == user / ".brigade"
    settings = fleet_client.load_fleet_settings(strict=True)
    assert settings["hub_url"] == USER_HUB
    assert settings["node_token"] == "user-node-token"
    assert fleet_client.home_identity_target() == user


def test_relative_token_files_resolve_under_the_fleet_toml_not_the_working_directory(
    tmp_path, monkeypatch, no_fleet_env
):
    home = tmp_path / "user-home" / ".brigade"
    home.mkdir(parents=True)
    _token(home / "node.token", "home-node-token")
    _token(home / "admin.token", "home-admin-token")
    (home / "fleet.toml").write_text(
        f'[fleet]\nhub_url = "{USER_HUB}"\nnode_token_file = "node.token"\ntoken_file = "admin.token"\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _token(workspace / "node.token", "workspace-node-token")
    _token(workspace / "admin.token", "workspace-admin-token")
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("BRIGADE_HOME", str(home))

    settings = fleet_client.load_fleet_settings(strict=True)
    assert settings["node_token"] == "home-node-token"
    assert settings["admin_token"] == "home-admin-token"
    assert fleet_claim_lifecycle._token_snapshot() == (USER_HUB, "home-node-token", "home-admin-token")


def test_a_tilde_token_file_still_expands(tmp_path, monkeypatch, no_fleet_env):
    user = set_home(monkeypatch, tmp_path / "profile")
    home = user / ".brigade"
    home.mkdir(parents=True)
    _token(user / "fleet-node.token", "tilde-node-token")
    (home / "fleet.toml").write_text('[fleet]\nnode_token_file = "~/fleet-node.token"\n', encoding="utf-8")
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    assert fleet_client.load_fleet_settings()["node_token"] == "tilde-node-token"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("node.token", PureWindowsPath("C:/BrigadeHome/.brigade/node.token")),
        ("tokens\\node.token", PureWindowsPath("C:/BrigadeHome/.brigade/tokens/node.token")),
        ("D:\\secrets\\node.token", PureWindowsPath("D:/secrets/node.token")),
        ("\\\\server\\share\\node.token", PureWindowsPath("//server/share/node.token")),
        ("C:node.token", None),
        ("\\node.token", None),
    ],
)
def test_windows_token_paths_resolve_or_refuse(raw, expected):
    base = PureWindowsPath("C:/BrigadeHome/.brigade")
    assert fleet_home_guard.resolve_against(PureWindowsPath(raw), base) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("node.token", PurePosixPath("/srv/brigade-home/.brigade/node.token")),
        ("/etc/node.token", PurePosixPath("/etc/node.token")),
    ],
)
def test_posix_token_paths_resolve(raw, expected):
    assert fleet_home_guard.resolve_against(PurePosixPath(raw), PurePosixPath("/srv/brigade-home/.brigade")) == expected


@pytest.mark.parametrize(
    ("home", "accepted"),
    [
        (PureWindowsPath("C:/BrigadeHome/.brigade"), True),
        (PureWindowsPath("//server/share/.brigade"), True),
        (PureWindowsPath("C:.brigade"), False),
        (PureWindowsPath("/.brigade"), False),
        (PureWindowsPath(".brigade"), False),
        (PurePosixPath("/srv/brigade-home/.brigade"), True),
        (PurePosixPath(".brigade"), False),
    ],
)
def test_the_absolute_home_check_on_windows_and_posix_paths(home, accepted, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: home)
    assert (fleet_home_guard.absolute_brigade_home() == home) is accepted
    assert (fleet_home_guard.absolute_brigade_home() is None) is not accepted
