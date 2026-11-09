"""#2416: the Grok Bot memory credential comes only from the user-level fleet.toml.

No test here prints the environment or a token. Assertions compare against the
fixture token instead of echoing it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest

from brigade import cli, dispatch_memory, fleet_client, grokbot_memory_credential as cred, grokbot_ops
from tests import test_dispatch_memory as dm
from tests.test_dispatch_memory import _isolated, _workspace, token_hub  # noqa: F401 - fixtures
from tests.test_grokbot_jobs import _spec

HUB_URL = "https://hub.invalid"
BOT = "fixture-grokbot-memory-principal-token-0001"
NODE = "fixture-host-node-credential-token-000000002"
POSIX = sys.platform != "win32"
posix_only = pytest.mark.skipif(not POSIX, reason="POSIX owner and mode bits")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    names = (
        "BRIGADE_FLEET_HUB_URL",
        "BRIGADE_FLEET_TOKEN",
        "BRIGADE_FLEET_NODE_TOKEN",
        "BRIGADE_FLEET_MESSAGE_TOKEN_FILE",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)


def _home() -> Path:
    home = Path(os.environ["BRIGADE_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    return home


def _private(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def _fleet_toml(token_file: str | None = None, *, hub: bool = True, node_file: Path | None = None, extra: str = ""):
    lines = ["[fleet]"]
    if hub:
        lines.append(f"hub_url = {json.dumps(HUB_URL)}")
    if node_file is not None:
        lines.append(f"node_token_file = {json.dumps(str(node_file))}")
    if token_file is not None:
        lines += ["", "[grokbot_memory]", f"token_file = {json.dumps(token_file)}"]
    (_home() / "fleet.toml").write_text("\n".join(lines) + "\n" + extra, encoding="utf-8")


def _token_file(tmp_path: Path, token: str = BOT) -> Path:
    return _private(tmp_path / "grokbot-memory.token", token)


def _assert_off(reason: str, *, refused: bool) -> None:
    status = cred.resolve()
    assert not status.on and status.authority is None
    assert status.reason == reason and status.refused is refused
    assert dispatch_memory.grokbot_memory_authority() is None


def _ok(condition: bool, what: str) -> None:
    """Fail with a fixed message, so a failing run never echoes a token or settings."""
    if not condition:
        pytest.fail(what, pytrace=False)


def _assert_quiet(text: str) -> None:
    _ok(BOT not in text and NODE not in text, "a token reached the text")


# On


def test_a_private_user_level_token_file_turns_refs_on(tmp_path, token_hub, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dm, "BOT_TOKEN", BOT)
    path = _token_file(tmp_path)
    _fleet_toml(str(path))
    target = _workspace(tmp_path)

    status = cred.resolve()
    assert status.on and status.reason == cred.ON and not status.refused
    _ok(dispatch_memory.grokbot_memory_authority() == (HUB_URL, BOT), "authority is not the bot credential")
    _assert_quiet(repr(status) + str(status))

    refs = dispatch_memory.job_refs(target, "hub deploy")
    assert refs == [{"scope": "fleet", "card_id": "deploy-notes", "hash": "a" * 12}]
    _ok(set(token_hub.tokens) == {BOT}, "a credential other than the bot's searched")
    spec = dispatch_memory.with_job_refs(target, {**_spec(), "instructions": "hub deploy"})
    assert spec["memory_refs"] == refs


# Off


def test_refs_stay_off_while_the_key_is_absent(tmp_path, token_hub):  # noqa: F811
    target = _workspace(tmp_path)
    _assert_off(cred.OFF_NOT_CONFIGURED, refused=False)
    _fleet_toml(None)
    _assert_off(cred.OFF_NOT_CONFIGURED, refused=False)
    _fleet_toml(None, extra="[grokbot_memory]\n")
    _assert_off(cred.OFF_NOT_CONFIGURED, refused=False)

    assert dispatch_memory.job_refs(target, "hub deploy") == []
    _ok(not token_hub.tokens, "a hub request was sent")


@posix_only
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644, 0o700 | 0o004])
def test_a_token_file_with_group_or_world_access_is_refused(tmp_path, token_hub, mode):  # noqa: F811
    path = _token_file(tmp_path)
    path.chmod(mode)
    _fleet_toml(str(path))

    _assert_off(cred.OFF_TOKEN_REFUSED, refused=True)
    assert dispatch_memory.job_refs(_workspace(tmp_path), "hub deploy") == []
    _ok(not token_hub.tokens, "a hub request was sent")


def test_a_symlinked_token_file_is_refused(tmp_path, token_hub):  # noqa: F811
    real = _token_file(tmp_path)
    link = tmp_path / "link.token"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks")
    _fleet_toml(str(link))

    _assert_off(cred.OFF_TOKEN_REFUSED, refused=True)
    assert dispatch_memory.job_refs(_workspace(tmp_path), "hub deploy") == []
    _ok(not token_hub.tokens, "a hub request was sent")


def test_a_missing_token_file_is_refused(tmp_path, token_hub):  # noqa: F811
    _fleet_toml(str(tmp_path / "absent.token"))

    _assert_off(cred.OFF_TOKEN_REFUSED, refused=True)
    assert dispatch_memory.job_refs(_workspace(tmp_path), "hub deploy") == []
    _ok(not token_hub.tokens, "a hub request was sent")


@pytest.mark.parametrize("kind", ["directory", "oversize", "not-a-token", "trailing-garbage", "empty"])
def test_a_token_file_that_is_not_one_bounded_token_is_refused(tmp_path, kind):
    path = tmp_path / "grokbot-memory.token"
    if kind == "directory":
        path.mkdir()
    else:
        text = {
            "oversize": "a" * 5000,
            "not-a-token": "short",
            "trailing-garbage": BOT + " extra",
            "empty": "",
        }[kind]
        _private(path, text)
    _fleet_toml(str(path))

    _assert_off(cred.OFF_TOKEN_REFUSED, refused=True)


def test_a_relative_path_is_refused_so_the_working_directory_cannot_choose(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _token_file(tmp_path)
    _fleet_toml("grokbot-memory.token")

    _assert_off(cred.OFF_NOT_ABSOLUTE, refused=True)


@pytest.mark.parametrize("value", ['""', "42", '["/a"]', '{ path = "/a" }'])
def test_a_malformed_key_is_refused(value):
    _fleet_toml(None, extra=f"[grokbot_memory]\ntoken_file = {value}\n")

    _assert_off(cred.OFF_INVALID_KEY, refused=True)


def test_an_unparsable_fleet_toml_is_refused():
    (_home() / "fleet.toml").write_text("[grokbot_memory\ntoken_file = ", encoding="utf-8")

    _assert_off(cred.OFF_CONFIG_UNREADABLE, refused=True)


def test_no_fleet_hub_means_no_refs(tmp_path):
    _fleet_toml(str(_token_file(tmp_path)), hub=False)

    _assert_off(cred.OFF_NO_HUB, refused=True)


def test_the_node_credential_cannot_double_as_the_bot_credential(tmp_path):
    node = _private(tmp_path / "node.token", NODE)
    _fleet_toml(str(_token_file(tmp_path, NODE)), node_file=node)

    _assert_off(cred.OFF_SHARED_CREDENTIAL, refused=True)


# A workspace file never chooses


def test_workspace_files_never_set_or_override_the_path(tmp_path, monkeypatch):
    target = _workspace(tmp_path)
    good = _token_file(tmp_path)
    other = _private(tmp_path / "workspace-chosen.token", "fixture-workspace-chosen-token-0000000003")
    workspace_toml = f"[grokbot_memory]\ntoken_file = {json.dumps(str(other))}\n"
    (target / ".brigade").mkdir(exist_ok=True)
    (target / ".brigade" / "fleet.toml").write_text(workspace_toml, encoding="utf-8")
    packs = target / ".brigade" / "grokbot" / "packs"
    packs.mkdir(parents=True)
    pack = {"hub": {"message_token_file": str(other)}}
    (packs / "grokbot-memory.json").write_text(json.dumps(pack), encoding="utf-8")
    monkeypatch.chdir(target)

    # With no user-level key, the workspace copies are never read.
    _assert_off(cred.OFF_NOT_CONFIGURED, refused=False)

    # With one, the user-level path wins and the workspace copies change nothing.
    _fleet_toml(str(good))
    _ok(dispatch_memory.grokbot_memory_authority() == (HUB_URL, BOT), "authority is not the user-level credential")


# The token never appears


def _doctor(target: Path) -> int:
    return cli.main(["run", "cloud", "grokbot", "doctor", "--target", str(target), "--instance", "operator"])


def test_the_token_never_reaches_output_logs_or_errors(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    path = _token_file(tmp_path)
    _fleet_toml(str(path))

    assert cred.doctor_check() == {"check": "memory-refs", "status": "ok", "detail": cred.ON}
    assert _doctor(tmp_path) == 1  # no listener configured in this target
    on = capsys.readouterr()
    assert f"memory-refs: ok ({cred.ON})" in on.out

    if POSIX:
        path.chmod(0o640)
    else:
        path.unlink()
    assert cred.doctor_check() == {"check": "memory-refs", "status": "fail", "detail": cred.OFF_TOKEN_REFUSED}
    assert _doctor(tmp_path) == 1
    off = capsys.readouterr()
    assert f"memory-refs: fail ({cred.OFF_TOKEN_REFUSED})" in off.out

    combined = on.out + on.err + off.out + off.err + caplog.text
    _assert_quiet(combined)
    assert str(path) not in combined


def test_doctor_reports_the_reason_and_fails_only_on_a_refused_key(tmp_path, capsys, monkeypatch):
    # Every other doctor check passes here, so the exit code is the memory-refs line alone.
    monkeypatch.setattr(grokbot_ops, "doctor", lambda *_a, **_k: [{"check": "config", "status": "ok"}])

    assert _doctor(tmp_path) == 0
    assert f"memory-refs: skipped ({cred.OFF_NOT_CONFIGURED})" in capsys.readouterr().out

    _fleet_toml(str(tmp_path / "absent.token"))
    assert _doctor(tmp_path) == 1
    assert f"memory-refs: fail ({cred.OFF_TOKEN_REFUSED})" in capsys.readouterr().out

    _fleet_toml(str(_token_file(tmp_path)))
    assert _doctor(tmp_path) == 0
    assert f"memory-refs: ok ({cred.ON})" in capsys.readouterr().out


def test_pack_doctor_shape_is_unchanged(tmp_path):
    assert all(set(check) == {"check", "status"} for check in grokbot_ops.doctor(tmp_path, "operator"))


# Dot, Duet and interactive search keep their principals


def test_fleet_settings_for_other_principals_are_unchanged(tmp_path, monkeypatch):
    from brigade import fleet_client_message_credentials as messages

    node = _private(tmp_path / "node.token", NODE)
    _fleet_toml(None, node_file=node)
    before = fleet_client.load_fleet_settings()
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "node-a")
    message_before = messages.message_settings(None)

    _fleet_toml(str(_token_file(tmp_path)), node_file=node)

    _ok(fleet_client.load_fleet_settings() == before, "fleet settings changed")
    _ok(messages.message_settings(None) == message_before, "message settings changed")
    _ok(messages.message_settings(None)[1] == NODE, "message operations left the node credential")
    assert cred.resolve().on
