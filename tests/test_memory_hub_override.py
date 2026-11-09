"""Memory's own hub: `[memory_hub]` in `~/.brigade/fleet.toml`, or its environment pair (#2318)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import fleet_client
from brigade import memory_hub_client as hubc
from brigade import memory_hub_search as hub_search
from brigade import memory_hub_store as store
from brigade import memory_hub_sync as sync
from brigade.dot import process as dot_process
from tests._home import set_home
from tests.test_memory_hub_client import (
    CONNECTOR_CREDENTIAL,
    HUB_URL,
    NODE_CREDENTIAL,
    OTHER_CREDENTIAL,
    POSIX_DOT,
    FakeHub,
    _card_ids,
    _dot_cli,
    run,
    workspace,
)

FLEET_HUB = "https://192.0.2.10:3774"
MEMORY_HUB = "https://192.0.2.20:3774"
ENV_HUB = "https://192.0.2.30:3774"
# Built at runtime: fixtures, not credentials.
ENV_CREDENTIAL = "-".join(("fixture", "env", "credential", "0123456789"))
ADMIN_CREDENTIAL = "-".join(("fixture", "admin", "credential", "0123456789"))
POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")


class Router:
    """Sends each call to the fake hub that owns the URL, and records which hub and token it used."""

    def __init__(self, **hubs: FakeHub) -> None:
        self.hubs = {url: fake for url, fake in hubs.items()}
        self.seen: list[tuple[str, str]] = []

    def __call__(self, url: str, token: str, body: dict, timeout: float):
        base = url.removesuffix("/memory")
        self.seen.append((base, token))
        return self.hubs[base](HUB_URL + "/memory", token, body, timeout)

    def hubs_used(self) -> set[str]:
        return {base for base, _ in self.seen}


@pytest.fixture
def hubs(monkeypatch, tmp_path):
    fleet, memory, other = FakeHub(), FakeHub(), FakeHub()
    router = Router(**{FLEET_HUB: fleet, MEMORY_HUB: memory, ENV_HUB: other})
    # A fake user home, so `~` and the default ~/.brigade never reach the real one.
    user = set_home(monkeypatch, tmp_path / "userhome")
    user.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", FLEET_HUB)
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", NODE_CREDENTIAL)
    for name in ("BRIGADE_FLEET_TOKEN", hubc.ENV_HUB_URL, hubc.ENV_HUB_TOKEN_FILE):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(hubc, "urllib_transport", router)
    router.fleet, router.memory, router.other, router.home, router.user = fleet, memory, other, home, user
    return router


def token_file(directory: Path, token: str = OTHER_CREDENTIAL, name: str = "memory-token", mode: int = 0o600) -> Path:
    path = directory / name
    path.write_text(token, encoding="utf-8")
    path.chmod(mode)
    return path


def literal(value: object) -> str:
    """A TOML literal string: a Windows path's backslashes stay as they are."""
    return f"'{value}'"


def user_config(home: Path, memory_hub: dict | None = None, fleet: dict | None = None, raw: str = "") -> Path:
    """Write `fleet.toml` under BRIGADE_HOME with the given `[memory_hub]` and `[fleet]` keys."""
    lines: list[str] = []
    for name, table in (("fleet", fleet), ("memory_hub", memory_hub)):
        if table is not None:
            lines.append(f"[{name}]")
            lines.extend(f"{key} = {literal(value)}" for key, value in table.items())
    path = home / "fleet.toml"
    path.write_text("\n".join(lines) + "\n" + raw, encoding="utf-8")
    return path


def configure(target: Path, **keys: object) -> Path:
    """Append keys to the workspace's `[hub]` table."""
    config = target / ".brigade" / "memory-care.toml"
    extra = "".join(f"{key} = {literal(value)}\n" for key, value in keys.items())
    config.write_text(config.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return target


def overridden(hubs: Router, tmp_path: Path, url: str = MEMORY_HUB, **cards: str) -> Path:
    """A read workspace whose user-level `[memory_hub]` points at `url`."""
    user_config(hubs.home, {"hub_url": url, "node_token_file": token_file(tmp_path)})
    return workspace(tmp_path / "ws", mode="read", **cards)


def put(fake: FakeHub, scope: str, card_id: str, text: str, token: str = OTHER_CREDENTIAL) -> None:
    hubc.MemoryHubClient(HUB_URL, token, transport=fake).call(
        "put", scope=scope, card_id=card_id, base_revision=None, text=text, client_event_id=hubc.event_id()
    )


# --- the override replaces the fleet values for memory only ---------------------------------------


def test_memory_uses_the_override_while_fleet_settings_stay_on_fleet(hubs, tmp_path, capsys):
    put(hubs.memory, "repo:test/app", "otter", "otter notes")
    put(hubs.fleet, "repo:test/app", "decoy", "otter decoy", token=NODE_CREDENTIAL)
    target = overridden(hubs, tmp_path)
    seeded = len(hubs.fleet.calls)

    current = hubc.load_settings(target)
    assert (current.hub_url, current.token, current.source) == (MEMORY_HUB, OTHER_CREDENTIAL, "[memory_hub]")
    # Fleet commands read `[fleet]`, untouched by `[memory_hub]`.
    assert fleet_client.load_fleet_settings()["hub_url"] == FLEET_HUB
    assert fleet_client.load_fleet_settings()["node_token"] == NODE_CREDENTIAL

    code, out, err = run(["memory", "hub", "pull", "--target", str(target), "--json"], capsys)
    assert code == 0, err
    assert _card_ids(store.replica(target)) == {"otter"}
    found = hub_search.search_payload(target, "otter")
    assert [(m["card_id"], m["source"]) for m in found["matches"] if m["source"] == "hub"] == [("otter", "hub")]
    code, out, err = run(["memory", "hub", "card", "--target", str(target), "--json", "otter"], capsys)
    assert code == 0 and json.loads(out)["card"]["text"] == "otter notes\n"
    assert hubs.hubs_used() == {MEMORY_HUB}
    assert {token for _, token in hubs.seen} == {OTHER_CREDENTIAL}
    assert len(hubs.fleet.calls) == seeded


@POSIX_DOT
def test_dot_and_duet_memory_tools_use_the_user_level_override(hubs, tmp_path):
    put(hubs.memory, "repo:test/app", "otter", "otter notes")
    tools = _dot_cli(overridden(hubs, tmp_path))
    found = tools.memory_search("otter")
    assert [(m["card_id"], m["source"]) for m in found["matches"]] == [("otter", "hub")]
    assert hubs.hubs_used() == {MEMORY_HUB}
    # Card reads are push-safe and go out as the connector principal, so they use the hub that
    # enrolled it, never the override: its credential never reaches another hub (S23).
    put(hubs.fleet, "repo:test/app", "otter", "otter notes")
    hubs.fleet.cards[("repo:test/app", "otter")]["trust"] = "reviewed"
    tools._authority = lambda: SimpleNamespace(hub=FLEET_HUB, token=CONNECTOR_CREDENTIAL)
    hubs.seen.clear()
    assert tools.memory_card("otter", scope="repo:test/app")["content"] == "otter notes\n"
    assert set(hubs.seen) == {(FLEET_HUB, CONNECTOR_CREDENTIAL)}


def test_the_dot_child_environment_carries_brigade_home_and_no_hub_variables(hubs, monkeypatch, tmp_path):
    monkeypatch.setenv(hubc.ENV_HUB_URL, ENV_HUB)
    monkeypatch.setenv(hubc.ENV_HUB_TOKEN_FILE, str(tmp_path / "token"))
    env = dot_process.child_environment("/usr/bin")
    # The child finds the same fleet.toml, so it reads the same `[memory_hub]`.
    assert env["BRIGADE_HOME"] == str(hubs.home)
    assert not [key for key in env if key.startswith(("BRIGADE_FLEET_", "BRIGADE_MEMORY_"))]


def test_the_default_brigade_home_is_read_without_brigade_home(hubs, tmp_path, monkeypatch):
    # Dot and Duet children get HOME even when BRIGADE_HOME is unset, so the default path must work.
    monkeypatch.delenv("BRIGADE_HOME")
    default = hubs.user / ".brigade"
    default.mkdir()
    user_config(default, {"hub_url": MEMORY_HUB, "node_token_file": token_file(tmp_path)})
    current = hubc.load_settings(workspace(tmp_path / "ws", mode="read"))
    assert (current.hub_url, current.source) == (MEMORY_HUB, "[memory_hub]")


def test_the_token_never_shows_in_the_settings_repr(hubs, tmp_path):
    assert OTHER_CREDENTIAL not in repr(hubc.load_settings(overridden(hubs, tmp_path)))


# --- a workspace never chooses the hub -------------------------------------------------------------


@pytest.mark.parametrize("mode", ["read", "off"])
@pytest.mark.parametrize(
    "keys",
    [
        {"hub_url": MEMORY_HUB},
        {"node_token_file": "token"},
        {"hub_url": MEMORY_HUB, "node_token_file": "token"},
    ],
)
def test_a_workspace_hub_table_that_sets_the_hub_is_refused(hubs, tmp_path, capsys, keys, mode):
    target = configure(workspace(tmp_path / "ws", mode=mode), **keys)
    with pytest.raises(
        hubc.HubConfigError, match=r"not read from the workspace: .*\[memory_hub\] in ~/\.brigade/fleet\.toml"
    ):
        hubc.load_settings(target)
    code, out, err = run(["memory", "hub", "status", "--target", str(target)], capsys)
    assert code == 2 and "[memory_hub]" in err and hubc.ENV_HUB_URL in err
    assert hubs.seen == []


def test_a_workspace_hub_url_is_refused_even_beside_a_user_level_override(hubs, tmp_path):
    put(hubs.memory, "repo:test/app", "otter", "otter notes")
    target = configure(overridden(hubs, tmp_path), hub_url=ENV_HUB)
    with pytest.raises(hubc.HubConfigError, match="not read from the workspace"):
        hubc.load_settings(target)


# --- validation --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keys", "missing"),
    [({"hub_url": MEMORY_HUB}, "node_token_file"), ({"node_token_file": "/token"}, "hub_url")],
)
def test_one_key_without_the_other_is_a_config_error_naming_the_missing_one(hubs, tmp_path, capsys, keys, missing):
    user_config(hubs.home, keys)
    target = workspace(tmp_path / "ws", mode="read")
    with pytest.raises(hubc.HubConfigError, match=rf"\[memory_hub\] {missing} is missing"):
        hubc.load_settings(target)
    code, out, err = run(["memory", "hub", "status", "--target", str(target)], capsys)
    assert code == 2 and f"[memory_hub] {missing} is missing" in err
    assert hubs.seen == []


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({hubc.ENV_HUB_URL: MEMORY_HUB}, hubc.ENV_HUB_TOKEN_FILE),
        ({hubc.ENV_HUB_TOKEN_FILE: "token"}, hubc.ENV_HUB_URL),
    ],
)
def test_one_environment_variable_without_the_other_is_a_config_error(hubs, tmp_path, monkeypatch, env, missing):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(hubc.HubConfigError, match=rf"{missing} is missing"):
        hubc.load_settings(workspace(tmp_path / "ws", mode="read"))


def test_a_blank_environment_value_counts_as_unset(hubs, tmp_path, monkeypatch):
    monkeypatch.setenv(hubc.ENV_HUB_URL, "  ")
    monkeypatch.setenv(hubc.ENV_HUB_TOKEN_FILE, "")
    current = hubc.load_settings(workspace(tmp_path / "ws", mode="read"))
    assert (current.hub_url, current.source) == (FLEET_HUB, "fleet")


@pytest.mark.parametrize("key", ["hub_url", "node_token_file"])
def test_an_override_key_must_be_a_non_empty_string(hubs, tmp_path, key):
    user_config(hubs.home, raw=f"[memory_hub]\n{key} = 7\n")
    with pytest.raises(hubc.HubConfigError, match=rf"\[memory_hub\] {key} must be a non-empty string"):
        hubc.load_settings(workspace(tmp_path / "ws", mode="read"))


def test_an_unknown_memory_hub_key_is_a_config_error(hubs, tmp_path):
    user_config(hubs.home, {"hub_url": MEMORY_HUB, "node_token_file": token_file(tmp_path), "token": "x"})
    with pytest.raises(hubc.HubConfigError, match=r"\[memory_hub\] has unknown keys: token"):
        hubc.load_settings(workspace(tmp_path / "ws", mode="read"))


def test_an_unreadable_user_config_is_a_config_error(hubs, tmp_path):
    user_config(hubs.home, raw="[memory_hub\n")
    with pytest.raises(hubc.HubConfigError, match="could not be read"):
        hubc.load_settings(workspace(tmp_path / "ws", mode="read"))


def test_the_override_url_gets_the_fleet_hub_scheme_rule(hubs, tmp_path):
    target = workspace(tmp_path / "ws", mode="read")
    path = token_file(tmp_path)
    user_config(hubs.home, {"hub_url": "http://192.0.2.20:3774", "node_token_file": path})
    with pytest.raises(hubc.HubConfigError, match=r"\[memory_hub\] hub_url: fleet hub URL must use https"):
        hubc.load_settings(target)
    user_config(hubs.home, {"hub_url": "http://127.0.0.1:3774", "node_token_file": path})
    assert hubc.load_settings(target).hub_url == "http://127.0.0.1:3774"


@pytest.mark.parametrize("userinfo", ["user:hunter2-secret@", "user@", ":hunter2-secret@"])
@pytest.mark.parametrize("source", ["[memory_hub]", "env"])
def test_the_override_url_may_not_carry_a_user_name_or_password(hubs, tmp_path, monkeypatch, userinfo, source):
    url = f"https://{userinfo}192.0.2.20:3774"
    path = token_file(tmp_path)
    if source == "env":
        monkeypatch.setenv(hubc.ENV_HUB_URL, url)
        monkeypatch.setenv(hubc.ENV_HUB_TOKEN_FILE, str(path))
        name = hubc.ENV_HUB_URL
    else:
        user_config(hubs.home, {"hub_url": url, "node_token_file": path})
        name = "[memory_hub] hub_url"
    with pytest.raises(hubc.HubConfigError) as caught:
        hubc.load_settings(workspace(tmp_path / "ws", mode="read"))
    assert str(caught.value) == f"{name} must not carry a user name or password"
    assert hubs.seen == []


def test_status_never_prints_url_credentials(hubs, tmp_path, monkeypatch, capsys):
    # The fleet URL is not checked for userinfo, so status strips it before printing.
    leaky = "https://user:hunter2-secret@192.0.2.10:3774"
    hubs.hubs[leaky] = hubs.fleet
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", leaky)
    target = workspace(tmp_path / "ws", mode="read")
    code, out, err = run(["memory", "hub", "status", "--target", str(target), "--json"], capsys)
    assert code == 0 and json.loads(out)["hub_url"] == FLEET_HUB
    code, text, err = run(["memory", "hub", "status", "--target", str(target)], capsys)
    assert f"hub url: {FLEET_HUB} (source: fleet)" in text
    assert "hunter2" not in out + text + err and "user:" not in out + text


def test_display_url_drops_only_the_userinfo():
    assert hubc.display_url("https://u:p@hub.example.test:3774/x?q=1") == "https://hub.example.test:3774/x?q=1"
    assert hubc.display_url(MEMORY_HUB) == MEMORY_HUB
    assert hubc.display_url("") == ""


@POSIX
@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604])
def test_the_token_file_must_be_private(hubs, tmp_path, mode):
    target = workspace(tmp_path / "ws", mode="read")
    path = token_file(tmp_path, mode=mode)
    user_config(hubs.home, {"hub_url": MEMORY_HUB, "node_token_file": path})
    with pytest.raises(hubc.HubConfigError) as caught:
        hubc.load_settings(target)
    message = str(caught.value)
    assert "[memory_hub] node_token_file" in message and "private, owned regular file" in message
    assert OTHER_CREDENTIAL not in message
    path.chmod(0o600)
    assert hubc.load_settings(target).token == OTHER_CREDENTIAL


@POSIX
def test_a_symlinked_or_missing_token_file_is_refused(hubs, tmp_path):
    target = workspace(tmp_path / "ws", mode="read")
    real = token_file(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real)
    for path in (link, tmp_path / "absent"):
        user_config(hubs.home, {"hub_url": MEMORY_HUB, "node_token_file": path})
        with pytest.raises(hubc.HubConfigError, match="private, owned regular file"):
            hubc.load_settings(target)


def test_the_table_token_path_expands_home_and_must_then_be_absolute(hubs, tmp_path, monkeypatch):
    home = hubs.user
    token_file(home, token=OTHER_CREDENTIAL, name="from-home")
    target = workspace(tmp_path / "ws", mode="read")
    user_config(hubs.home, {"hub_url": MEMORY_HUB, "node_token_file": "~/from-home"})
    assert hubc.load_settings(target).token == OTHER_CREDENTIAL
    # A relative path is refused, even when the workspace holds a file by that name.
    token_file(target, token=ENV_CREDENTIAL, name="relative-token")
    user_config(hubs.home, {"hub_url": MEMORY_HUB, "node_token_file": "relative-token"})
    with pytest.raises(hubc.HubConfigError, match=r"\[memory_hub\] node_token_file must be an absolute path"):
        hubc.load_settings(target)


@pytest.mark.parametrize("raw", ["relative-token", "~/env-token"])
def test_the_environment_token_path_must_be_absolute(hubs, tmp_path, monkeypatch, raw):
    home = hubs.user
    target = workspace(tmp_path / "ws", mode="read")
    token_file(target, token=ENV_CREDENTIAL, name="relative-token")
    token_file(home, token=ENV_CREDENTIAL, name="env-token")
    monkeypatch.setenv(hubc.ENV_HUB_URL, ENV_HUB)
    monkeypatch.setenv(hubc.ENV_HUB_TOKEN_FILE, raw)
    with pytest.raises(hubc.HubConfigError, match=rf"{hubc.ENV_HUB_TOKEN_FILE} must be an absolute path"):
        hubc.load_settings(target)


# --- a fleet credential never goes to the memory hub ----------------------------------------------


@pytest.mark.parametrize("source", ["[memory_hub]", "env"])
@pytest.mark.parametrize("fleet_credential", ["node", "admin"])
def test_an_override_token_equal_to_a_fleet_token_is_refused(
    hubs, tmp_path, monkeypatch, capsys, source, fleet_credential
):
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", ADMIN_CREDENTIAL)
    reused = NODE_CREDENTIAL if fleet_credential == "node" else ADMIN_CREDENTIAL
    path = token_file(tmp_path, token=reused, name="copied-fleet-token")
    if source == "env":
        monkeypatch.setenv(hubc.ENV_HUB_URL, MEMORY_HUB)
        monkeypatch.setenv(hubc.ENV_HUB_TOKEN_FILE, str(path))
    else:
        user_config(hubs.home, {"hub_url": MEMORY_HUB, "node_token_file": path})
    target = workspace(tmp_path / "ws", mode="read")
    with pytest.raises(hubc.HubConfigError, match="must not use a fleet credential") as caught:
        hubc.load_settings(target)
    assert reused not in str(caught.value)
    for argv in (
        ["memory", "hub", "status", "--target", str(target)],
        ["memory", "search", "--target", str(target), "--json", "otter"],
    ):
        code, out, err = run(argv, capsys)
        assert reused not in out + err and ADMIN_CREDENTIAL not in out + err
    assert hubs.seen == []


def test_an_override_token_equal_to_the_fleet_file_token_is_refused(hubs, tmp_path, monkeypatch):
    # The fleet node token comes from `[fleet] node_token_file`, and the override holds a copy of it.
    monkeypatch.delenv("BRIGADE_FLEET_NODE_TOKEN")
    fleet_file = token_file(tmp_path, token=NODE_CREDENTIAL, name="fleet-node-token")
    copy = token_file(tmp_path, token=NODE_CREDENTIAL, name="copy")
    user_config(hubs.home, {"hub_url": MEMORY_HUB, "node_token_file": copy}, fleet={"node_token_file": fleet_file})
    with pytest.raises(hubc.HubConfigError, match="must not use a fleet credential"):
        hubc.load_settings(workspace(tmp_path / "ws", mode="read"))


@pytest.mark.parametrize("fleet_key", ["node_token_file", "token_file"])
@pytest.mark.parametrize("spelling", ["same", "hardlink"])
def test_the_fleet_token_file_is_refused_as_the_override(hubs, tmp_path, monkeypatch, fleet_key, spelling):
    # Both fleet tokens in use come from the environment, so only the file check can catch this.
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", ADMIN_CREDENTIAL)
    fleet_file = token_file(tmp_path, token=OTHER_CREDENTIAL, name="fleet-token")
    override = fleet_file
    if spelling == "hardlink":
        override = tmp_path / "linked-token"
        try:
            os.link(fleet_file, override)
        except OSError:
            pytest.skip("no hard links on this filesystem")
    user_config(hubs.home, {"hub_url": MEMORY_HUB, "node_token_file": override}, fleet={fleet_key: fleet_file})
    with pytest.raises(hubc.HubConfigError, match="must not use a fleet credential") as caught:
        hubc.load_settings(workspace(tmp_path / "ws", mode="read"))
    assert OTHER_CREDENTIAL not in str(caught.value)
    assert hubs.seen == []


def test_the_fleet_token_file_is_refused_through_the_environment_pair(hubs, tmp_path, monkeypatch):
    fleet_file = token_file(tmp_path, token=OTHER_CREDENTIAL, name="fleet-token")
    user_config(hubs.home, fleet={"node_token_file": fleet_file})
    monkeypatch.setenv(hubc.ENV_HUB_URL, ENV_HUB)
    monkeypatch.setenv(hubc.ENV_HUB_TOKEN_FILE, str(fleet_file))
    with pytest.raises(hubc.HubConfigError, match=rf"{hubc.ENV_HUB_TOKEN_FILE} .*must not use a fleet credential"):
        hubc.load_settings(workspace(tmp_path / "ws", mode="read"))


def test_a_missing_fleet_token_file_does_not_block_a_distinct_override(hubs, tmp_path):
    # samefile fails when the fleet file is absent; the resolved-path fallback must not match another file.
    user_config(
        hubs.home,
        {"hub_url": MEMORY_HUB, "node_token_file": token_file(tmp_path)},
        fleet={"node_token_file": tmp_path / "absent-fleet-token"},
    )
    assert hubc.load_settings(workspace(tmp_path / "ws", mode="read")).token == OTHER_CREDENTIAL


# --- precedence and source ---------------------------------------------------------------------


def test_precedence_is_environment_then_memory_hub_table_then_fleet(hubs, tmp_path, monkeypatch):
    target = workspace(tmp_path / "ws", mode="read")
    fleet = hubc.load_settings(target)
    assert (fleet.hub_url, fleet.token, fleet.source) == (FLEET_HUB, NODE_CREDENTIAL, "fleet")
    user_config(hubs.home, {"hub_url": MEMORY_HUB, "node_token_file": token_file(tmp_path)})
    assert hubc.load_settings(target).source == "[memory_hub]"

    env_token = token_file(tmp_path, token=ENV_CREDENTIAL, name="env-token")
    monkeypatch.setenv(hubc.ENV_HUB_URL, ENV_HUB)
    monkeypatch.setenv(hubc.ENV_HUB_TOKEN_FILE, str(env_token))
    current = hubc.load_settings(target)
    assert (current.hub_url, current.token, current.source) == (ENV_HUB, ENV_CREDENTIAL, "env")


def test_the_environment_pair_is_checked_like_the_table_pair(hubs, tmp_path, monkeypatch):
    monkeypatch.setenv(hubc.ENV_HUB_URL, "http://192.0.2.30:3774")
    monkeypatch.setenv(hubc.ENV_HUB_TOKEN_FILE, str(token_file(tmp_path)))
    with pytest.raises(hubc.HubConfigError, match=rf"{hubc.ENV_HUB_URL}: fleet hub URL must use https"):
        hubc.load_settings(workspace(tmp_path / "ws", mode="read"))


@POSIX
def test_the_environment_token_file_must_be_private(hubs, tmp_path, monkeypatch):
    monkeypatch.setenv(hubc.ENV_HUB_URL, ENV_HUB)
    monkeypatch.setenv(hubc.ENV_HUB_TOKEN_FILE, str(token_file(tmp_path, mode=0o644)))
    with pytest.raises(hubc.HubConfigError, match=rf"{hubc.ENV_HUB_TOKEN_FILE} .*private, owned regular file"):
        hubc.load_settings(workspace(tmp_path / "ws", mode="read"))


@pytest.mark.parametrize("source", ["fleet", "[memory_hub]", "env"])
def test_status_reports_where_the_hub_came_from_and_never_the_token(hubs, tmp_path, monkeypatch, capsys, source):
    urls = {"fleet": FLEET_HUB, "[memory_hub]": MEMORY_HUB, "env": ENV_HUB}
    tokens = {"fleet": NODE_CREDENTIAL, "[memory_hub]": OTHER_CREDENTIAL, "env": ENV_CREDENTIAL}
    target = overridden(hubs, tmp_path) if source == "[memory_hub]" else workspace(tmp_path / "ws", mode="read")
    if source == "env":
        monkeypatch.setenv(hubc.ENV_HUB_URL, ENV_HUB)
        monkeypatch.setenv(hubc.ENV_HUB_TOKEN_FILE, str(token_file(tmp_path, token=ENV_CREDENTIAL, name="env-token")))
    for fake in (hubs.fleet, hubs.memory, hubs.other):
        fake.principals[ENV_CREDENTIAL] = "node-3"
        fake.grants["node-3"] = None

    code, out, err = run(["memory", "hub", "status", "--target", str(target), "--json"], capsys)
    report = json.loads(out)
    assert code == 0 and report["hub_source"] == source and report["hub_url"] == urls[source]
    assert report["hub"]["available"] is True
    code, text, err = run(["memory", "hub", "status", "--target", str(target)], capsys)
    assert f"hub url: {urls[source]} (source: {source})" in text
    for secret in tokens.values():
        assert secret not in out + text + err
    assert hubs.hubs_used() == {urls[source]}


def test_status_reports_the_source_when_no_hub_is_configured(hubs, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BRIGADE_FLEET_HUB_URL")
    target = workspace(tmp_path / "ws", mode="read")
    code, out, err = run(["memory", "hub", "status", "--target", str(target), "--json"], capsys)
    report = json.loads(out)
    assert code == 0 and report["hub_source"] == "fleet" and "hub_url" not in report
    assert report["hub"] == {"available": False, "reason": "no-hub-configured"}


def test_the_no_node_credential_warning_names_the_memory_hub_override(hubs, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BRIGADE_FLEET_NODE_TOKEN")
    target = workspace(tmp_path / "ws", mode="read")
    code, out, err = run(["memory", "hub", "status", "--target", str(target), "--json"], capsys)
    report = json.loads(out)
    assert report["hub"] == {"available": False, "reason": "no-node-credential"}
    [warning] = [w for w in report["warnings"] if "no node token" in w]
    assert "[memory_hub]" in warning and hubc.ENV_HUB_TOKEN_FILE in warning


# --- the replica belongs to a hub ---------------------------------------------------------------


def test_switching_hub_url_discards_the_replica_and_rebuilds_instead_of_merging(hubs, tmp_path):
    # Same log_epoch and sequence numbers on both hubs, so only the hub identity tells them apart.
    hubs.other.epoch = hubs.memory.epoch
    put(hubs.memory, "repo:test/app", "from-first", "otter first")
    put(hubs.other, "repo:test/app", "from-second", "otter second")
    target = overridden(hubs, tmp_path)

    first = hubc.load_settings(target)
    sync.pull(target, hubc.client_for(first), first)
    assert _card_ids(store.replica(target)) == {"from-first"}
    assert store.replica(target).meta["hub"] == hubc.hub_identity(MEMORY_HUB)

    config = hubs.home / "fleet.toml"
    config.write_text(config.read_text(encoding="utf-8").replace(MEMORY_HUB, ENV_HUB), encoding="utf-8")
    second = hubc.load_settings(target)
    assert second.hub_url == ENV_HUB
    # Until the next pull the old replica is not served, even when the new hub is down.
    hubs.other.mode = "down"
    found = hub_search.search_payload(target, "otter", settings=second)
    assert not [m for m in found["matches"] if m.get("source") == "replica"]
    hubs.other.mode = "up"

    report = sync.pull(target, hubc.client_for(second), second)
    assert report["rebuilt"] and any("another hub" in warning for warning in report["warnings"])
    live = store.replica(target)
    assert _card_ids(live) == {"from-second"}
    assert live.meta["hub"] == hubc.hub_identity(ENV_HUB)
    assert not store.building(target).exists


# --- off mode ------------------------------------------------------------------------------------


def test_off_mode_never_reads_the_override(hubs, tmp_path, monkeypatch, capsys):
    cards = {"alpha": "---\ntitle: Alpha\n---\nhub memory words\n", "beta": "# Beta\nwords\n"}
    off = workspace(tmp_path / "off", mode="off", **cards)
    resolved = str(off.resolve())
    argvs = (
        ["memory", "search", "--target", str(off), "words"],
        ["memory", "search", "--target", str(off), "--json", "words"],
        ["memory", "hub", "status", "--target", str(off), "--json"],
        ["memory", "hub", "status", "--target", str(off)],
    )

    def outputs() -> list[str]:
        seen = []
        for argv in argvs:
            code, out, err = run(argv, capsys)
            assert code == 0 and err == "", argv
            seen.append(out.replace(json.dumps(resolved)[1:-1], "<target>").replace(resolved, "<target>"))
        return seen

    plain = outputs()
    # A half-set table and a half-set environment pair, both errors while memory is on.
    user_config(hubs.home, {"hub_url": MEMORY_HUB})
    monkeypatch.setenv(hubc.ENV_HUB_URL, ENV_HUB)
    assert outputs() == plain
    assert "hub_source" not in plain[2] and "hub url" not in plain[3]
    assert hubs.seen == []


# --- the suite scrubs the override variables ------------------------------------------------------


@pytest.fixture(scope="module")
def _exported_override():
    """An operator's exported override, set before the function-scoped autouse fixtures run."""
    saved = {name: os.environ.get(name) for name in (hubc.ENV_HUB_URL, hubc.ENV_HUB_TOKEN_FILE)}
    os.environ[hubc.ENV_HUB_URL] = "https://192.0.2.99:3774"
    os.environ[hubc.ENV_HUB_TOKEN_FILE] = "/nonexistent/memory-token"
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_the_suite_scrubs_an_exported_memory_hub_override(_exported_override):
    assert hubc.ENV_HUB_URL not in os.environ
    assert hubc.ENV_HUB_TOKEN_FILE not in os.environ
