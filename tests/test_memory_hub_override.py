"""A workspace's own memory hub: `[hub] hub_url` and `node_token_file`, or their environment pair."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade import fleet_client
from brigade import memory_hub_client as hubc
from brigade import memory_hub_search as hub_search
from brigade import memory_hub_store as store
from brigade import memory_hub_sync as sync
from tests.test_memory_hub_client import (
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
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", FLEET_HUB)
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", NODE_CREDENTIAL)
    for name in ("BRIGADE_FLEET_TOKEN", hubc.ENV_HUB_URL, hubc.ENV_HUB_TOKEN_FILE):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(hubc, "urllib_transport", router)
    router.fleet, router.memory, router.other = fleet, memory, other
    return router


def token_file(directory: Path, token: str = OTHER_CREDENTIAL, name: str = "memory-token", mode: int = 0o600) -> Path:
    path = directory / name
    path.write_text(token, encoding="utf-8")
    path.chmod(mode)
    return path


def configure(target: Path, **keys: str) -> Path:
    """Append override keys to the workspace's `[hub]` table."""
    config = target / ".brigade" / "memory-care.toml"
    extra = "".join(f'{key} = "{value}"\n' for key, value in keys.items())
    config.write_text(config.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return target


def overridden(tmp_path: Path, url: str = MEMORY_HUB, **cards: str) -> Path:
    target = workspace(tmp_path / "ws", mode="read", **cards)
    return configure(target, hub_url=url, node_token_file=str(token_file(tmp_path)))


def put(fake: FakeHub, scope: str, card_id: str, text: str, token: str = OTHER_CREDENTIAL) -> None:
    hubc.MemoryHubClient(HUB_URL, token, transport=fake).call(
        "put", scope=scope, card_id=card_id, base_revision=None, text=text, client_event_id=hubc.event_id()
    )


# --- the override replaces the fleet values for memory only ---------------------------------------


def test_memory_uses_the_override_while_fleet_settings_stay_on_fleet(hubs, tmp_path, capsys):
    put(hubs.memory, "repo:test/app", "otter", "otter notes")
    put(hubs.fleet, "repo:test/app", "decoy", "otter decoy", token=NODE_CREDENTIAL)
    target = overridden(tmp_path)
    seeded = len(hubs.fleet.calls)

    current = hubc.load_settings(target)
    assert (current.hub_url, current.token, current.source) == (MEMORY_HUB, OTHER_CREDENTIAL, "[hub]")
    # Fleet commands read `[fleet]`, untouched by the workspace.
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


def test_the_fleet_settings_are_not_read_when_memory_has_its_own_hub(hubs, tmp_path, monkeypatch):
    def refuse() -> dict:
        raise AssertionError("fleet settings were read")

    monkeypatch.setattr(fleet_client, "load_fleet_settings", refuse)
    assert hubc.load_settings(overridden(tmp_path)).hub_url == MEMORY_HUB


@POSIX_DOT
def test_dot_and_duet_memory_tools_use_the_override(hubs, tmp_path):
    put(hubs.memory, "repo:test/app", "otter", "otter notes")
    tools = _dot_cli(overridden(tmp_path, hub_url=MEMORY_HUB))
    found = tools.memory_search("otter")
    assert [(m["card_id"], m["source"]) for m in found["matches"]] == [("otter", "hub")]
    assert tools.memory_card("otter", scope="repo:test/app")["content"] == "otter notes\n"
    assert hubs.hubs_used() == {MEMORY_HUB}


def test_the_token_never_shows_in_the_settings_repr(hubs, tmp_path):
    assert OTHER_CREDENTIAL not in repr(hubc.load_settings(overridden(tmp_path)))


# --- validation --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keys", "missing"),
    [({"hub_url": MEMORY_HUB}, "node_token_file"), ({"node_token_file": "token"}, "hub_url")],
)
def test_one_key_without_the_other_is_a_config_error_naming_the_missing_one(hubs, tmp_path, capsys, keys, missing):
    target = configure(workspace(tmp_path / "ws", mode="read"), **keys)
    with pytest.raises(hubc.HubConfigError, match=rf"\[hub\] {missing} is missing"):
        hubc.load_settings(target)
    code, out, err = run(["memory", "hub", "status", "--target", str(target)], capsys)
    assert code == 2 and f"[hub] {missing} is missing" in err
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
    target = workspace(tmp_path / "ws", mode="read")
    config = target / ".brigade" / "memory-care.toml"
    config.write_text(config.read_text(encoding="utf-8") + f"{key} = 7\n", encoding="utf-8")
    with pytest.raises(hubc.HubConfigError, match=rf"\[hub\] {key} must be a non-empty string"):
        hubc.load_settings(target)


def test_the_override_url_gets_the_fleet_hub_scheme_rule(hubs, tmp_path):
    target = workspace(tmp_path / "ws", mode="read")
    configure(target, hub_url="http://192.0.2.20:3774", node_token_file=str(token_file(tmp_path)))
    with pytest.raises(hubc.HubConfigError, match=r"\[hub\] hub_url: fleet hub URL must use https"):
        hubc.load_settings(target)
    configure(
        workspace(tmp_path / "loop", mode="read"),
        hub_url="http://127.0.0.1:3774",
        node_token_file=str(tmp_path / "memory-token"),
    )
    assert hubc.load_settings(tmp_path / "loop").hub_url == "http://127.0.0.1:3774"


@POSIX
@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604])
def test_the_token_file_must_be_private(hubs, tmp_path, mode):
    target = workspace(tmp_path / "ws", mode="read")
    path = token_file(tmp_path, mode=mode)
    configure(target, hub_url=MEMORY_HUB, node_token_file=str(path))
    with pytest.raises(hubc.HubConfigError) as caught:
        hubc.load_settings(target)
    message = str(caught.value)
    assert "[hub] node_token_file" in message and "private, owned regular file" in message
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
        configure(target, hub_url=MEMORY_HUB, node_token_file=str(path))
        with pytest.raises(hubc.HubConfigError, match="private, owned regular file"):
            hubc.load_settings(target)
        (target / ".brigade" / "memory-care.toml").write_text(
            '[hub]\nmode = "read"\nscope = "fleet"\n', encoding="utf-8"
        )


def test_the_token_file_path_expands_home_and_resolves_against_the_workspace(hubs, tmp_path, monkeypatch):
    home = tmp_path / "userhome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    token_file(home, token=OTHER_CREDENTIAL, name="from-home")
    target = workspace(tmp_path / "ws", mode="read")
    configure(target, hub_url=MEMORY_HUB, node_token_file="~/from-home")
    assert hubc.load_settings(target).token == OTHER_CREDENTIAL
    token_file(target, token=ENV_CREDENTIAL, name="relative-token")
    (target / ".brigade" / "memory-care.toml").write_text('[hub]\nmode = "read"\nscope = "fleet"\n', encoding="utf-8")
    configure(target, hub_url=MEMORY_HUB, node_token_file="relative-token")
    assert hubc.load_settings(target).token == ENV_CREDENTIAL


# --- precedence and source ---------------------------------------------------------------------


def test_precedence_is_environment_then_hub_table_then_fleet(hubs, tmp_path, monkeypatch):
    plain = workspace(tmp_path / "plain", mode="read")
    table = overridden(tmp_path / "table")
    fleet = hubc.load_settings(plain)
    assert (fleet.hub_url, fleet.token, fleet.source) == (FLEET_HUB, NODE_CREDENTIAL, "fleet")
    assert hubc.load_settings(table).source == "[hub]"

    env_token = token_file(tmp_path, token=ENV_CREDENTIAL, name="env-token")
    monkeypatch.setenv(hubc.ENV_HUB_URL, ENV_HUB)
    monkeypatch.setenv(hubc.ENV_HUB_TOKEN_FILE, str(env_token))
    for target in (plain, table):
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


@pytest.mark.parametrize("source", ["fleet", "[hub]", "env"])
def test_status_reports_where_the_hub_came_from_and_never_the_token(hubs, tmp_path, monkeypatch, capsys, source):
    urls = {"fleet": FLEET_HUB, "[hub]": MEMORY_HUB, "env": ENV_HUB}
    tokens = {"fleet": NODE_CREDENTIAL, "[hub]": OTHER_CREDENTIAL, "env": ENV_CREDENTIAL}
    target = overridden(tmp_path) if source == "[hub]" else workspace(tmp_path / "ws", mode="read")
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


# --- the replica belongs to a hub ---------------------------------------------------------------


def test_switching_hub_url_discards_the_replica_and_rebuilds_instead_of_merging(hubs, tmp_path):
    # Same log_epoch and sequence numbers on both hubs, so only the hub identity tells them apart.
    hubs.other.epoch = hubs.memory.epoch
    put(hubs.memory, "repo:test/app", "from-first", "otter first")
    put(hubs.other, "repo:test/app", "from-second", "otter second")
    target = overridden(tmp_path)

    first = hubc.load_settings(target)
    sync.pull(target, hubc.client_for(first), first)
    assert _card_ids(store.replica(target)) == {"from-first"}
    assert store.replica(target).meta["hub"] == hubc.hub_identity(MEMORY_HUB)

    config = target / ".brigade" / "memory-care.toml"
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


def test_off_mode_output_is_byte_identical_with_the_override_keys_and_variables_set(
    hubs, tmp_path, monkeypatch, capsys
):
    cards = {"alpha": "---\ntitle: Alpha\n---\nhub memory words\n", "beta": "# Beta\nwords\n"}
    plain = workspace(tmp_path / "plain", mode=None, **cards)
    off = workspace(tmp_path / "off", mode="off", **cards)
    keyed = configure(
        workspace(tmp_path / "keyed", mode="off", **cards), hub_url=MEMORY_HUB, node_token_file="never-read"
    )
    # Only one of the pair, which would be an error while memory is on.
    monkeypatch.setenv(hubc.ENV_HUB_URL, ENV_HUB)
    outputs: dict[Path, list[str]] = {}
    for target in (plain, off, keyed):
        outputs[target] = []
        resolved = str(target.resolve())
        for argv in (
            ["memory", "search", "--target", str(target), "words"],
            ["memory", "search", "--target", str(target), "--json", "words"],
            ["memory", "hub", "status", "--target", str(target), "--json"],
            ["memory", "hub", "status", "--target", str(target)],
        ):
            code, out, err = run(argv, capsys)
            assert code == 0 and err == "", argv
            outputs[target].append(out.replace(json.dumps(resolved)[1:-1], "<target>").replace(resolved, "<target>"))
    # Search is the plain local path with or without a `[hub]` table. Status reads the table's scopes,
    # so it is compared between the two workspaces that have one.
    assert outputs[plain][:2] == outputs[off][:2] == outputs[keyed][:2]
    assert outputs[off][2:] == outputs[keyed][2:]
    assert "hub_source" not in outputs[keyed][2] and "hub url" not in outputs[keyed][3]
    assert hubs.seen == []
