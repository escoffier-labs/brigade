"""#2678: no removal path may erase a Claude Code ``headersHelper`` Brigade never wrote.

An owned live entry that carries a helper reads pristine (fingerprints ignore preserved native
keys), so ``mcp sync --prune``, profile catalog removal and profile uninstall would delete it
without ever reaching the rewrite gates. Each path must block that one server, remove nothing
for it, let its siblings proceed, and ignore ``--force``/``--adopt``/``--prune``.

Real code paths in a scratch HOME; the only patches wrap a planner to move the live file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import harness_profile_cmd, mcp_adapters, mcp_cmd, mcp_native_auth
from tests._home import set_home

MARKER = "MARKER_REMOVAL_HELPER_6b21"
HELPER = f"/opt/fake/helper --profile {MARKER}"
URL = "https://mcp.example.com/v1"
PLAIN_URL = "https://plain.example.com/mcp"
REASON = "native_auth_blocks_removal"
# Taken from the source mapping so this file carries no copy of a retired engine
# name; the rename paths below are what the test targets, not the literal.
LEGACY, CANONICAL = next(iter(mcp_cmd.LEGACY_SERVER_KEYS.items()))


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    set_home(monkeypatch, home)
    repo = tmp_path / "repo"
    repo.mkdir()
    assert mcp_cmd.init(target=repo, json_output=True) == 0
    return repo, home


def _server(name, url):
    return mcp_adapters.CanonicalServer(name=name, transport="http", url=url)


def _catalog(repo, **servers):
    mcp_cmd._write_canonical(repo, {name: _server(name, url) for name, url in servers.items()})


def _live_path(home):
    return home / ".claude.json"


def _live(home):
    return json.loads(_live_path(home).read_text())["mcpServers"]


def _add_helper(home, name="docs"):
    doc = json.loads(_live_path(home).read_text())
    doc["mcpServers"][name]["headersHelper"] = HELPER
    _live_path(home).write_text(json.dumps(doc, indent=2) + "\n")


def _drop_helper(home, name="docs"):
    doc = json.loads(_live_path(home).read_text())
    doc["mcpServers"][name].pop("headersHelper")
    _live_path(home).write_text(json.dumps(doc, indent=2) + "\n")


def _no_marker(*texts):
    for text in texts:
        assert MARKER not in text and "/opt/fake/helper" not in text


def _run(capsys, fn, **kw):
    capsys.readouterr()
    rc = fn(**kw)
    captured = capsys.readouterr()
    out = captured.out + captured.err
    _no_marker(out)
    return rc, out


# --------------------------------------------------------------------------- #
# mcp sync --prune
# --------------------------------------------------------------------------- #


def _sync(capsys, repo, **kw):
    kw = {"harness": "claude-user", "user_scope": True, "write": True, "json_output": True, **kw}
    rc, out = _run(capsys, mcp_cmd.sync, target=repo, **kw)
    return rc, json.loads(out)


def _owned_docs_and_plain(box, capsys):
    """Both servers owned by Brigade; docs then gains a user-owned helper; both leave the catalog."""
    repo, home = box
    _catalog(repo, docs=URL, plain=PLAIN_URL)
    assert _sync(capsys, repo)[0] == 0
    _add_helper(home)
    _catalog(repo)
    return repo, home


def _row(payload, server):
    return next(i for i in payload["items"] if i["server"] == server)


@pytest.mark.parametrize(
    "flags", [{"prune": True}, {"prune": True, "force": True, "adopt": True}], ids=["prune", "all"]
)
def test_prune_never_removes_an_owned_entry_that_carries_a_helper(box, capsys, flags):
    repo, home = _owned_docs_and_plain(box, capsys)
    before = _live(home)["docs"]
    rc, payload = _sync(capsys, repo, **flags)
    assert rc == 1
    row = _row(payload, "docs")
    assert (row["status"], row["action"], row["reason"]) == ("conflicted", "conflict", REASON)
    assert "docs" in row["detail"] and "claude-user" in row["detail"] and "by hand" in row["detail"]
    assert any(f["reason"] == REASON and f["blocking"] for r in payload["fidelity"]["servers"] for f in r["fields"])
    assert _live(home)["docs"] == before and before["headersHelper"] == HELPER
    assert "plain" not in _live(home)  # the sibling without a helper is still pruned
    ownership = mcp_cmd._load_state(repo)["ownership"]["claude-user"]
    assert set(next(iter(ownership.values()))) == {"docs"}
    _no_marker(mcp_cmd.state_path(repo).read_text())


def test_prune_plan_without_write_reports_the_block(box, capsys):
    repo, home = _owned_docs_and_plain(box, capsys)
    before = _live_path(home).read_bytes()
    rc, payload = _sync(capsys, repo, prune=True, write=False)
    assert rc == 1 and _row(payload, "docs")["reason"] == REASON
    assert _row(payload, "plain")["action"] == "remove"
    assert _live_path(home).read_bytes() == before


def test_prune_blocks_again_until_the_operator_clears_the_helper(box, capsys):
    repo, home = _owned_docs_and_plain(box, capsys)
    assert _sync(capsys, repo, prune=True)[0] == 1
    assert _sync(capsys, repo, prune=True, force=True, adopt=True)[0] == 1
    _drop_helper(home)
    rc, payload = _sync(capsys, repo, prune=True)
    assert rc == 0 and _row(payload, "docs")["action"] == "remove"
    assert _live(home) == {}


def test_prune_without_a_helper_is_unchanged(box, capsys):
    repo, home = box
    _catalog(repo, docs=URL, plain=PLAIN_URL)
    assert _sync(capsys, repo)[0] == 0
    _catalog(repo)
    rc, payload = _sync(capsys, repo, prune=True)
    assert rc == 0 and {i["server"]: i["action"] for i in payload["items"]} == {"docs": "remove", "plain": "remove"}
    assert _live(home) == {}
    assert not any(mcp_cmd._load_state(repo)["ownership"].get("claude-user", {}).values())


def test_helper_appearing_between_plan_and_apply_blocks_the_removal(box, capsys, monkeypatch):
    repo, home = box
    _catalog(repo, docs=URL, plain=PLAIN_URL)
    assert _sync(capsys, repo)[0] == 0
    _catalog(repo)
    real, calls = mcp_cmd._plan_for_harness, []

    def plan_then_add_the_helper(*args, **kwargs):
        result = real(*args, **kwargs)
        if not calls:
            calls.append(1)
            assert {i["action"] for i in result} == {"remove"}  # planned as a plain removal
            _add_helper(home)
        return result

    monkeypatch.setattr(mcp_cmd, "_plan_for_harness", plan_then_add_the_helper)
    rc, payload = _sync(capsys, repo, prune=True)
    assert calls and rc == 1
    assert (_row(payload, "docs")["status"], _row(payload, "docs")["reason"]) == ("conflicted", REASON)
    assert _live(home)["docs"]["headersHelper"] == HELPER
    assert "plain" not in _live(home)


# --------------------------------------------------------------------------- #
# --rename-legacy-servers
# --------------------------------------------------------------------------- #


def _legacy_owned_with_helper(box, capsys):
    repo, home = box
    _catalog(repo, **{LEGACY: URL})
    assert _sync(capsys, repo)[0] == 0
    _add_helper(home, LEGACY)
    return repo, home


def test_a_rename_carries_the_helper_to_the_replacement_before_the_legacy_entry_goes(box, capsys):
    repo, home = _legacy_owned_with_helper(box, capsys)
    rc, payload = _sync(capsys, repo, rename_legacy_servers=True)
    assert rc == 0, payload
    live = _live(home)
    assert LEGACY not in live and live[CANONICAL]["headersHelper"] == HELPER


def test_a_rename_that_cannot_carry_the_helper_keeps_the_legacy_entry(box, capsys):
    repo, home = _legacy_owned_with_helper(box, capsys)
    _catalog(repo, **{LEGACY: "https://elsewhere.example.net/v1"})
    before = _live_path(home).read_bytes()
    rc, payload = _sync(capsys, repo, rename_legacy_servers=True, force=True, adopt=True)
    assert rc == 1 and _live_path(home).read_bytes() == before
    assert _live(home)[LEGACY]["headersHelper"] == HELPER


def test_a_legacy_leftover_with_a_helper_is_not_cleaned_up(box, capsys):
    repo, home = _legacy_owned_with_helper(box, capsys)
    _catalog(repo, **{CANONICAL: URL})
    assert _sync(capsys, repo)[0] == 0  # installs the canonical entry; the legacy one is now a leftover
    rc, payload = _sync(capsys, repo, rename_legacy_servers=True)
    assert rc == 1
    assert _row(payload, LEGACY)["reason"] == REASON
    assert _live(home)[LEGACY]["headersHelper"] == HELPER and CANONICAL in _live(home)


# --------------------------------------------------------------------------- #
# harness profile: catalog removal and uninstall
# --------------------------------------------------------------------------- #


@pytest.fixture
def pbox(box):
    return box


def _profile_files(home):
    root = home / ".claude" / "brigade"
    return "\n".join(p.read_text() for p in sorted(root.glob("*.json")))


def _state_mcp(home):
    return json.loads((home / ".claude" / "brigade" / "install-state.json").read_text())["mcp"]


def _profile(capsys, fn, repo, home, **kw):
    capsys.readouterr()
    rc = fn(harness="claude", workspace=repo, home=home, json_output=True, **kw)
    captured = capsys.readouterr()
    out = captured.out + captured.err
    _no_marker(out)
    return rc, json.loads(out)


def _mcp_rows(payload):
    return {i["name"]: i for r in payload["results"] for i in r["items"] if i["surface"] == "mcp"}


def _profile_owned(pbox, capsys):
    repo, home = pbox
    _catalog(repo, docs=URL, plain=PLAIN_URL)
    rc, _ = _profile(capsys, harness_profile_cmd.sync, repo, home, write=True, adopt=False)
    assert rc == 0 and set(_live(home)) == {"docs", "plain"}
    _add_helper(home)
    return repo, home


def test_profile_catalog_removal_keeps_the_helper_entry_and_its_ownership(pbox, capsys):
    repo, home = _profile_owned(pbox, capsys)
    _catalog(repo)
    for kw in ({}, {"adopt": True, "force": True}):
        rc, payload = _profile(capsys, harness_profile_cmd.sync, repo, home, write=True, **{"adopt": False, **kw})
        assert rc == 1 and payload["ready"] is False
        result = payload["results"][0]
        assert result["status"] == "incomplete" and result["incomplete"] is True
        rows = _mcp_rows(payload)
        assert (rows["docs"]["status"], rows["docs"]["reason"]) == ("blocked", REASON)
        assert "docs" in rows["docs"]["detail"] and "claude" in rows["docs"]["detail"]
        assert _live(home)["docs"]["headersHelper"] == HELPER
        assert "plain" not in _live(home)
        assert set(_state_mcp(home)) == {"docs"}  # ownership kept for the next run
        _no_marker(_profile_files(home))
    _drop_helper(home)
    rc, payload = _profile(capsys, harness_profile_cmd.sync, repo, home, write=True, adopt=False)
    assert rc == 0 and _live(home) == {} and _state_mcp(home) == {}


def test_profile_removal_without_a_helper_is_unchanged(pbox, capsys):
    repo, home = pbox
    _catalog(repo, docs=URL, plain=PLAIN_URL)
    assert _profile(capsys, harness_profile_cmd.sync, repo, home, write=True, adopt=False)[0] == 0
    _catalog(repo)
    rc, payload = _profile(capsys, harness_profile_cmd.sync, repo, home, write=True, adopt=False)
    assert rc == 0 and payload["results"][0]["incomplete"] is False
    assert _live(home) == {} and _state_mcp(home) == {}
    rc, payload = _profile(capsys, harness_profile_cmd.uninstall, repo, home, write=True)
    assert rc == 0 and not (home / ".claude" / "brigade" / "install-state.json").exists()


def test_profile_uninstall_finishes_the_rest_and_reports_incomplete(pbox, capsys):
    repo, home = _profile_owned(pbox, capsys)
    for kw in ({}, {"force": True}):
        rc, payload = _profile(capsys, harness_profile_cmd.uninstall, repo, home, write=True, **kw)
        assert rc == 1 and payload["ready"] is False
        result = payload["results"][0]
        assert result["status"] == "incomplete" and result["incomplete"] is True and not result["conflicts"]
        assert [b["name"] for b in result["blocked"]] == ["docs"]
        rows = _mcp_rows(payload)
        assert (rows["docs"]["status"], rows["docs"]["reason"]) == ("blocked", REASON)
        assert rows.get("plain", {"action": "remove"})["action"] == "remove"  # gone after the first pass
        assert _live(home) == {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}}
        assert set(_state_mcp(home)) == {"docs"}
        assert Path(result["receipt_path"]).exists()
        _no_marker(_profile_files(home))
    _drop_helper(home)
    rc, payload = _profile(capsys, harness_profile_cmd.uninstall, repo, home, write=True)
    assert rc == 0 and _live(home) == {}
    assert not (home / ".claude" / "brigade" / "install-state.json").exists()


def test_profile_uninstall_completes_when_the_operator_removes_the_entry_by_hand(pbox, capsys):
    repo, home = _profile_owned(pbox, capsys)
    assert _profile(capsys, harness_profile_cmd.uninstall, repo, home, write=True)[0] == 1
    live_path = _live_path(home)
    doc = json.loads(live_path.read_text())
    doc["mcpServers"].pop("docs")
    live_path.write_text(json.dumps(doc))
    rc, _ = _profile(capsys, harness_profile_cmd.uninstall, repo, home, write=True)
    assert rc == 0 and not (home / ".claude" / "brigade" / "install-state.json").exists()


def test_profile_text_output_names_the_server_without_the_helper(pbox, capsys):
    repo, home = _profile_owned(pbox, capsys)
    capsys.readouterr()
    rc = harness_profile_cmd.uninstall(harness="claude", workspace=repo, home=home, write=True)
    out = capsys.readouterr()
    text = out.out + out.err
    assert rc == 1 and "incomplete" in text and "docs" in text and "by hand" in text
    _no_marker(text)


@pytest.mark.parametrize("operation", ["uninstall", "sync"])
def test_profile_helper_appearing_between_plan_and_write_blocks_the_removal(pbox, capsys, monkeypatch, operation):
    repo, home = pbox
    _catalog(repo, docs=URL, plain=PLAIN_URL)
    assert _profile(capsys, harness_profile_cmd.sync, repo, home, write=True, adopt=False)[0] == 0
    if operation == "sync":
        _catalog(repo)
    name = "_mcp_uninstall_plan" if operation == "uninstall" else "_mcp_plan"
    real, calls = getattr(harness_profile_cmd, name), []

    def plan_then_add_the_helper(*args, **kwargs):
        result = real(*args, **kwargs)
        if not calls:
            calls.append(1)
            assert result["remove"] == {"docs", "plain"}
            _add_helper(home)
        return result

    monkeypatch.setattr(harness_profile_cmd, name, plan_then_add_the_helper)
    kw = {"adopt": False} if operation == "sync" else {}
    rc, payload = _profile(capsys, getattr(harness_profile_cmd, operation), repo, home, write=True, **kw)
    assert calls and rc == 1
    result = payload["results"][0]
    assert result["status"] == "conflict" and [c["reason"] for c in result["conflicts"]] == [REASON]
    assert _live(home)["docs"]["headersHelper"] == HELPER and "plain" in _live(home)
    assert set(_state_mcp(home)) == {"docs", "plain"}


def test_the_reason_is_reported_as_itself_and_the_message_is_value_free():
    from brigade import mcp_fidelity

    assert REASON in mcp_fidelity._ERROR_REASONS and mcp_native_auth.REMOVAL_BLOCKED == REASON
    assert mcp_native_auth.removal_blockers("claude", {"a": {"headersHelper": HELPER}, "b": {}}, ["a", "b", "c"]) == [
        "a"
    ]
    assert not mcp_native_auth.has_preserved_native_keys("codex", {"headersHelper": HELPER})
    assert not mcp_native_auth.has_preserved_native_keys("claude", {"headersHelper": ""})
    message = mcp_native_auth.removal_block_message("docs", "claude")
    _no_marker(message)
    assert message.startswith("docs:") and "claude" in message
