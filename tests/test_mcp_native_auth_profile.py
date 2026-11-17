"""#2678 through the harness profile layer (`brigade harness sync|doctor|uninstall`).

The profile layer has its own plan, verify and uninstall paths that fingerprint live entries,
so the Claude Code ``headersHelper`` has to survive and stay invisible there too: kept on a
same-origin update, never written on an origin or transport change, and absent from the
ownership state and receipt files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import harness_profile_cmd, mcp_cmd
from tests._home import set_home

HELPER = "/opt/fake/helper --profile MARKER_PROFILE_HELPER_4d1e"
MARKER = "MARKER_PROFILE_HELPER_4d1e"
URL = "https://mcp.example.com/v1"
OTHER_URL = "https://other.example.net/v1"


@pytest.fixture
def pbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    set_home(monkeypatch, home)
    ws = tmp_path / "ws"
    ws.mkdir()
    assert mcp_cmd.init(target=ws, json_output=True) == 0
    return ws, home


def _catalog(ws, entry):
    mcp_cmd.canonical_path(ws).write_text(json.dumps({"servers": {"docs": entry}}))


def _live_path(home):
    return home / ".claude.json"


def _live(home):
    return json.loads(_live_path(home).read_text())["mcpServers"]


def _write_live(home, entry):
    _live_path(home).write_text(json.dumps({"mcpServers": {"docs": entry}}, indent=2) + "\n")


def _profile_files(home):
    root = home / ".claude" / "brigade"
    return "\n".join(p.read_text() for p in sorted(root.glob("*.json")))


def _run(capsys, fn, ws, home, **kw):
    capsys.readouterr()
    rc = fn(harness="claude", workspace=ws, home=home, json_output=True, **kw)
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert MARKER not in out and "/opt/fake/helper" not in out
    return rc, json.loads(out)


def _mcp_items(payload):
    return [i for r in payload["results"] for i in r.get("mcp", {}).get("items", [])]


def _sync(capsys, ws, home, **kw):
    return _run(capsys, harness_profile_cmd.sync, ws, home, write=True, **{"adopt": False, **kw})


def _doctor(capsys, ws, home):
    return _run(capsys, harness_profile_cmd.doctor, ws, home, verify_mcp=True)


def _managed_entry_with_helper(capsys, ws, home):
    """The profile creates the entry (managed ownership); the user then adds a helper by hand."""
    _catalog(ws, {"transport": "http", "url": URL})
    rc, payload = _sync(capsys, ws, home)
    assert rc == 0 and [i["action"] for i in _mcp_items(payload)] == ["create"]
    live = _live(home)
    live["docs"]["headersHelper"] = HELPER
    _write_live(home, live["docs"])


def test_helper_is_kept_through_profile_update_verify_and_uninstall(pbox, capsys):
    ws, home = pbox
    _managed_entry_with_helper(capsys, ws, home)
    # Verify and the next sync fingerprint the entry without the helper: current, not edited.
    assert _doctor(capsys, ws, home)[0] == 0
    rc, payload = _sync(capsys, ws, home)
    assert rc == 0 and [(i["status"], i["action"]) for i in _mcp_items(payload)] == [("current", "none")]

    _catalog(ws, {"transport": "http", "url": "https://mcp.example.com:443/v2"})
    rc, payload = _sync(capsys, ws, home)
    assert rc == 0 and [i["action"] for i in _mcp_items(payload)] == ["update"]
    assert _live(home)["docs"] == {"type": "http", "url": "https://mcp.example.com:443/v2", "headersHelper": HELPER}
    assert _doctor(capsys, ws, home)[0] == 0
    assert MARKER not in _profile_files(home)

    # Uninstall still recognises the entry as the one the profile wrote, but never erases the
    # helper with it: the entry stays, ownership stays, and the run reports itself incomplete.
    rc, payload = _run(capsys, harness_profile_cmd.uninstall, ws, home, write=True)
    assert rc == 1 and [(i["status"], i["reason"]) for i in _mcp_items(payload)] == [
        ("blocked", "native_auth_blocks_removal")
    ]
    assert _live(home)["docs"]["headersHelper"] == HELPER


@pytest.mark.parametrize("adopt", [False, True])
def test_profile_origin_change_blocks_without_a_write(pbox, capsys, adopt):
    ws, home = pbox
    _managed_entry_with_helper(capsys, ws, home)
    assert _sync(capsys, ws, home)[0] == 0
    before_live, before_state = _live_path(home).read_bytes(), _profile_files(home)
    _catalog(ws, {"transport": "http", "url": OTHER_URL})

    rc, payload = _sync(capsys, ws, home, adopt=adopt)
    assert rc == 1
    assert [(i["status"], i["reason"]) for i in _mcp_items(payload)] == [("conflict", "native_auth_origin_changed")]
    assert _live_path(home).read_bytes() == before_live
    assert _profile_files(home) == before_state
    rc, payload = _doctor(capsys, ws, home)
    assert rc == 1
    assert [i["reason"] for i in _mcp_items(payload)] == ["native_auth_origin_changed"]


@pytest.mark.parametrize("adopt", [False, True])
def test_profile_transport_change_to_stdio_blocks_without_a_write(pbox, capsys, adopt):
    ws, home = pbox
    _managed_entry_with_helper(capsys, ws, home)
    before_live = _live_path(home).read_bytes()
    _catalog(ws, {"transport": "stdio", "command": "npx", "args": ["-y", "pkg"]})
    rc, payload = _sync(capsys, ws, home, adopt=adopt, allow_global_stdio=True)
    assert rc == 1
    assert [i["reason"] for i in _mcp_items(payload)] == ["native_auth_transport_changed"]
    assert _live_path(home).read_bytes() == before_live


@pytest.mark.parametrize("adopt", [False, True])
def test_profile_blocks_an_entry_it_does_not_own_on_origin_change(pbox, capsys, adopt):
    ws, home = pbox
    _write_live(home, {"type": "http", "url": URL, "headersHelper": HELPER})
    _catalog(ws, {"transport": "http", "url": OTHER_URL})
    before = _live_path(home).read_bytes()
    rc, payload = _sync(capsys, ws, home, adopt=adopt)
    assert rc == 1
    assert [i["reason"] for i in _mcp_items(payload)] == ["native_auth_origin_changed"]
    assert _live_path(home).read_bytes() == before
    assert MARKER not in _profile_files(home)


def test_profile_adopts_a_helper_entry_on_the_same_origin_and_keeps_receipts_clean(pbox, capsys):
    ws, home = pbox
    _write_live(home, {"type": "http", "url": URL, "headersHelper": HELPER})
    _catalog(ws, {"transport": "http", "url": URL})
    rc, payload = _sync(capsys, ws, home, adopt=True)
    assert rc == 0 and [i["status"] for i in _mcp_items(payload)] == ["adopted"]
    assert _live(home)["docs"]["headersHelper"] == HELPER
    assert MARKER not in _profile_files(home)
    assert _doctor(capsys, ws, home)[0] == 0


def test_profile_blocks_catalog_headers_next_to_a_live_helper(pbox, capsys):
    ws, home = pbox
    _write_live(home, {"type": "http", "url": URL, "headersHelper": HELPER})
    _catalog(ws, {"transport": "http", "url": URL, "headers": {"X-Team": {"literal": "blue"}}})
    before = _live_path(home).read_bytes()
    for adopt in (False, True):
        rc, payload = _sync(capsys, ws, home, adopt=adopt)
        assert rc == 1
        assert [i["reason"] for i in _mcp_items(payload)] == ["native_auth_source_collision"]
        assert _live_path(home).read_bytes() == before
