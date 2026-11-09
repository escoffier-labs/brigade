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


@pytest.fixture
def relative_home_workspace(tmp_path, monkeypatch, no_fleet_env):
    """A repository with its own `.brigade/node.toml` and fleet.toml, run with BRIGADE_HOME=.brigade."""
    from brigade import node as node_mod

    workspace = tmp_path / "ws"
    _fleet_toml(workspace / ".brigade", WORKSPACE_HUB, label="workspace")
    node_mod.ensure_identity(workspace)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("BRIGADE_HOME", ".brigade")
    return workspace


@pytest.fixture
def no_workspace_reads(monkeypatch):
    """Fail if the node identity or a fleet.toml is read."""
    from brigade import node as node_mod

    def refuse(*args, **kwargs):
        raise AssertionError("a workspace-selected fleet file was read")

    monkeypatch.setattr(node_mod, "load_identity", refuse)
    monkeypatch.setattr(fleet_client, "_read_fleet_section", refuse)


def test_a_relative_home_has_no_claim_target(relative_home_workspace, no_workspace_reads, no_requests):
    # Even where discovery would find the home identity, a relative home gives no key at all, so no
    # caller can submit a directory name as a shared claim (#2423).
    with pytest.raises(fleet_home_guard.RelativeBrigadeHomeError):
        fleet_client.resolve_claim_target(relative_home_workspace)
    assert not no_requests


def test_a_relative_home_gives_no_claim_target_for_a_plain_directory(tmp_path, monkeypatch, no_fleet_env):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.setenv("BRIGADE_HOME", ".brigade")
    with pytest.raises(fleet_home_guard.RelativeBrigadeHomeError):
        fleet_client.resolve_claim_target(plain)


def _run_argv(workspace: Path) -> list[str]:
    roster = workspace / "roster.toml"
    roster.write_text('orchestrator = "chef"\n\n[agents.chef]\ncli = "codex"\nrole = "plan"\n', encoding="utf-8")
    return ["run", "do something", "--roster", str(roster), "--cwd", str(workspace)]


def test_a_run_under_a_relative_home_takes_no_claim_on_an_env_hub(
    relative_home_workspace, monkeypatch, no_requests, caplog
):
    from brigade import aboyeur, cli

    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", USER_HUB)
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "fixture-node-credential")

    def no_claim(*args, **kwargs):
        raise AssertionError("a fleet claim was attempted")

    monkeypatch.setattr(fleet_client, "repo_claim", no_claim)
    monkeypatch.setattr(fleet_client, "acquire_claim", no_claim)
    dispatched: list[int] = []
    monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: dispatched.append(1) or 0)

    with caplog.at_level(logging.WARNING, logger="brigade.fleet"):
        assert cli.main(_run_argv(relative_home_workspace)) == 0
    assert dispatched == [1]
    skipped = [r.message for r in caplog.records if "fleet claim skipped for run directory" in r.message]
    assert skipped == [
        "fleet claim skipped for run directory ws (BRIGADE_HOME is relative); relying on the local run lock alone"
    ]
    assert not no_requests


def test_require_fleet_claim_under_a_relative_home_fails_cleanly(relative_home_workspace, monkeypatch, capsys):
    from brigade import aboyeur, cli

    dispatched: list[int] = []
    monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: dispatched.append(1) or 0)
    assert cli.main([*_run_argv(relative_home_workspace), "--require-fleet-claim"]) == 2
    err = capsys.readouterr().err
    assert "BRIGADE_HOME must be an absolute path" in err and "Traceback" not in err
    assert dispatched == []


def test_grokbot_takes_no_claim_under_a_relative_home(relative_home_workspace, monkeypatch, no_requests):
    from types import SimpleNamespace

    from brigade import grokbot_jobs, grokbot_mcp

    # `_fleet_acquire` swallows every exception, so record attempts instead of raising.
    attempts: list[object] = []
    monkeypatch.setattr(fleet_client, "acquire_claim", lambda *args, **kwargs: attempts.append(args))
    monkeypatch.setattr(grokbot_jobs, "lease_claim_target", lambda *args: None)
    owner = SimpleNamespace(
        config=SimpleNamespace(target=relative_home_workspace, bot_id="bot", lease_seconds=60, instance="builder")
    )
    decision, target, from_row = grokbot_mcp.GrokbotAdapter._fleet_acquire(owner, "job", "lease", "holder", "session")
    assert decision.granted is False and target is None and from_row is False
    assert attempts == []
    assert not no_requests


def test_one_home_resolution_serves_the_section_and_its_token_files(tmp_path, monkeypatch, no_fleet_env):
    first, second = tmp_path / "first" / ".brigade", tmp_path / "second" / ".brigade"
    for home, label in ((first, "first"), (second, "second")):
        home.mkdir(parents=True)
        _token(home / "node.token", f"{label}-node-token")
        (home / "fleet.toml").write_text(f'[fleet]\nhub_url = "{USER_HUB}"\nnode_token_file = "node.token"\n')
    homes = iter([first, second, second, second])
    monkeypatch.setattr(fleet_home_guard, "absolute_brigade_home", lambda: next(homes))
    monkeypatch.chdir(tmp_path)

    assert fleet_client.load_fleet_settings()["node_token"] == "first-node-token"
    homes = iter([first, second, second, second])
    assert fleet_claim_lifecycle._token_snapshot()[1] == "first-node-token"


def test_a_local_only_run_with_no_fleet_claim_works_under_a_relative_home(
    relative_home_workspace, monkeypatch, no_requests, capsys
):
    from brigade import aboyeur, cli

    roster = relative_home_workspace / "roster.toml"
    roster.write_text('orchestrator = "chef"\n\n[agents.chef]\ncli = "codex"\nrole = "plan"\n', encoding="utf-8")
    dispatched: list[int] = []
    monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: dispatched.append(1) or 0)
    argv = ["run", "do something", "--roster", str(roster), "--cwd", str(relative_home_workspace)]

    assert cli.main([*argv, "--no-fleet-claim"]) == 0
    assert dispatched == [1]
    assert "Traceback" not in capsys.readouterr().err
    assert not no_requests


def test_model_admission_under_a_relative_home_fails_closed_without_the_cache(
    relative_home_workspace, monkeypatch, no_requests
):
    import urllib.error

    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", USER_HUB)
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "fixture-node-credential")

    def unauthorized(*args, **kwargs):
        raise urllib.error.HTTPError(USER_HUB + "/models", 401, "unauthorized", None, None)

    monkeypatch.setattr(fleet_client, "_get_models_blocking", unauthorized)
    assert fleet_model_admission.nofollow_supported() is False
    decision = fleet_model_admission.fetch_versioned_roster()
    assert decision.ok is False
    assert decision.reason == "auth-failed"
    assert not (relative_home_workspace / ".brigade" / fleet_client.SPOOL_DIRNAME).exists()


@pytest.mark.parametrize(
    ("argv", "code"),
    [(["node", "--machine"], 2), (["fleet", "claims", "--release", ".", "--path"], 1)],
    ids=["node-machine", "claims-release-path"],
)
def test_fleet_commands_that_need_the_home_print_a_clear_error(relative_home_workspace, argv, code, capsys):
    from brigade import cli

    assert cli.main(argv) == code
    err = capsys.readouterr().err
    assert "BRIGADE_HOME must be an absolute path" in err and "Traceback" not in err


def test_worklore_best_effort_receipts_skip_a_relative_home(relative_home_workspace):
    from brigade import worklore_brigade_sync, worklore_github_sync

    worklore_github_sync._write_receipt_best_effort(
        "github:example", idempotency_key="", observation_count=0, last_error_code=None
    )
    worklore_brigade_sync._write_receipt_best_effort(
        "brigade:example", adapter_id="brigade", idempotency_key="", observation_count=0, last_error_code=None
    )
    assert not (relative_home_workspace / ".brigade" / "worklore").exists()


def test_no_fleet_claim_never_resolves_a_claim_target(relative_home_workspace, monkeypatch, no_requests):
    from brigade import aboyeur, cli

    def refused(base_path=None):
        raise fleet_home_guard.RelativeBrigadeHomeError(fleet_home_guard.RELATIVE_HOME_REFUSAL)

    monkeypatch.setattr(fleet_client, "resolve_claim_target", refused)
    roster = relative_home_workspace / "roster.toml"
    roster.write_text('orchestrator = "chef"\n\n[agents.chef]\ncli = "codex"\nrole = "plan"\n', encoding="utf-8")
    dispatched: list[int] = []
    monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: dispatched.append(1) or 0)
    argv = ["run", "do something", "--roster", str(roster), "--cwd", str(relative_home_workspace)]

    assert cli.main([*argv, "--no-fleet-claim"]) == 0
    assert dispatched == [1]


@pytest.fixture
def stored_lease(relative_home_workspace, monkeypatch, no_requests):
    """A Grok Bot lease whose claim key was stored before the home turned relative, with an env hub."""
    from types import SimpleNamespace

    from brigade import grokbot_jobs

    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", USER_HUB)
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "fixture-node-credential")
    monkeypatch.setattr(grokbot_jobs, "lease_claim_target", lambda *args: "stored-key")
    calls: list[str] = []
    for name in ("acquire_claim", "renew_claim", "release_claim", "report_external_event"):
        monkeypatch.setattr(fleet_client, name, lambda *args, _name=name, **kwargs: calls.append(_name))
    owner = SimpleNamespace(
        config=SimpleNamespace(target=relative_home_workspace, bot_id="bot", lease_seconds=60, instance="builder")
    )
    owner._lease_target = lambda job_id, lease_id: grokbot_mcp_adapter()._lease_target(owner, job_id, lease_id)
    return owner, calls


def grokbot_mcp_adapter():
    from brigade import grokbot_mcp

    return grokbot_mcp.GrokbotAdapter


def test_grokbot_never_acquires_a_stored_key_under_a_relative_home(stored_lease, no_requests):
    owner, calls = stored_lease
    decision, target, _from_row = grokbot_mcp_adapter()._fleet_acquire(owner, "job", "lease", "holder", "session")
    assert decision.granted is False and target is None
    assert calls == [] and not no_requests


def test_grokbot_never_renews_a_stored_key_under_a_relative_home(stored_lease, no_requests):
    owner, calls = stored_lease
    decision = grokbot_mcp_adapter()._fleet_renew(owner, "job", "lease", "holder")
    assert decision.granted is False
    assert calls == [] and not no_requests


@pytest.mark.parametrize("target", [None, "stored-key"], ids=["from-row", "explicit"])
def test_grokbot_never_releases_a_stored_key_under_a_relative_home(stored_lease, no_requests, target):
    owner, calls = stored_lease
    grokbot_mcp_adapter()._fleet_release(owner, "job", "lease", "holder", target)
    assert calls == [] and not no_requests


def test_grokbot_never_reports_an_event_for_a_stored_key_under_a_relative_home(stored_lease, no_requests):
    owner, calls = stored_lease
    grokbot_mcp_adapter()._fleet_event(owner, "job", "lease", "session", "running")
    assert calls == [] and not no_requests


@pytest.mark.parametrize("operation", ["acquire", "renew", "release", "inspect"])
def test_no_claim_request_leaves_under_a_relative_home_even_with_an_explicit_node(
    relative_home_workspace, monkeypatch, no_requests, operation
):
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", USER_HUB)
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "fixture-node-credential")
    sent: list[object] = []
    monkeypatch.setattr(fleet_client, "_post_claim_blocking", lambda *args, **kwargs: sent.append(args))
    call = {
        "acquire": lambda: fleet_client.acquire_claim("stored-key", node_id="node-a"),
        "renew": lambda: fleet_client.renew_claim("stored-key", holder="holder", node_id="node-a"),
        "release": lambda: fleet_client.release_claim("stored-key", holder="holder", node_id="node-a"),
        "inspect": lambda: fleet_client.inspect_claim("stored-key", node_id="node-a"),
    }[operation]
    decision = call()
    assert decision.granted is False and decision.reason == "relative-brigade-home"
    assert sent == [] and not no_requests


def test_an_operator_release_by_node_sends_nothing_under_a_relative_home(
    relative_home_workspace, monkeypatch, no_requests, capsys
):
    from brigade import cli

    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", USER_HUB)
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "fixture-node-credential")
    sent: list[object] = []
    monkeypatch.setattr(fleet_client, "_post_claim_blocking", lambda *args, **kwargs: sent.append(args))
    assert cli.main(["fleet", "claims", "--release", "stored-key", "--node", "node-a"]) != 0
    assert "Traceback" not in capsys.readouterr().err
    assert sent == [] and not no_requests
