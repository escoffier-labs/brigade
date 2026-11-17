"""#2678: remote-server auth survives `mcp import` and `mcp sync`.

Real code paths in a scratch HOME. The Claude Code ``headersHelper`` stays out of the
catalog and the sidecar, is kept on the same live entry while the endpoint is unchanged,
and blocks the projection (no override) when the endpoint would change.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib

import pytest

from brigade import localio, mcp_adapters, mcp_cmd, mcp_native_auth
from tests._home import set_home

# Markers are planted in commands and variable names, then searched for in every output.
HELPER = "/opt/fake/helper --profile MARKER_HELPER_7f3a"
TOKEN_VAR = "MARKER_TOKEN_VAR_91c2"
URL = "https://mcp.example.com/v1"
OTHER_URL = "https://other.example.net/v1"
MARKERS = ("MARKER_HELPER_7f3a", "MARKER_TOKEN_VAR_91c2", "/opt/fake/helper")


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    repo = tmp_path / "repo"
    repo.mkdir()
    assert mcp_cmd.init(target=repo, json_output=True) == 0
    return repo, home


def _write_claude(home, servers):
    path = home / ".claude.json"
    path.write_text(json.dumps({"numStartups": 3, "mcpServers": servers}, indent=2) + "\n")
    return path


def _write_codex(home, body):
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _claude_servers(path):
    return json.loads(path.read_text())["mcpServers"]


def _run(capsys, fn, **kw):
    capsys.readouterr()
    rc = fn(**kw)
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def _sync(capsys, repo, harness="claude-user", **kw):
    kw = {
        "adopt": True,
        "force": True,
        "write": True,
        "json_output": True,
        "user_scope": harness.endswith("-user"),
        **kw,
    }
    rc, out = _run(capsys, mcp_cmd.sync, target=repo, harness=harness, **kw)
    return rc, out, json.loads(out)


def _statuses(capsys, repo, harness="claude-user"):
    rc, out = _run(
        capsys, mcp_cmd.plan, target=repo, harness=harness, user_scope=harness.endswith("-user"), json_output=True
    )
    return {i["server"]: i for i in json.loads(out)["items"]}


def _import_claude(capsys, repo):
    return _run(
        capsys,
        mcp_cmd.import_servers,
        target=repo,
        harness="claude-user",
        merge=True,
        user_scope=True,
        json_output=True,
    )


def _set_catalog_url(repo, name, url):
    path = mcp_cmd.canonical_path(repo)
    doc = json.loads(path.read_text())
    doc["servers"][name]["url"] = url
    path.write_text(json.dumps(doc, indent=2) + "\n")


def _assert_no_markers(*texts):
    for text in texts:
        for marker in MARKERS:
            assert marker not in text


# --------------------------------------------------------------------------- #
# Claude headersHelper: import -> sync
# --------------------------------------------------------------------------- #


def test_helper_survives_import_and_sync_and_second_sync_is_a_noop(box, capsys):
    repo, home = box
    live = {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}}
    path = _write_claude(home, live)
    rc, out = _import_claude(capsys, repo)
    assert rc == 0
    catalog = mcp_cmd.canonical_path(repo).read_text()
    _assert_no_markers(catalog, out)
    assert "headersHelper" not in catalog

    rc, out, payload = _sync(capsys, repo)
    assert rc == 0
    assert _claude_servers(path) == live
    assert _statuses(capsys, repo)["docs"]["status"] == "current"
    before = path.read_text()
    rc, out, payload = _sync(capsys, repo)
    assert (rc, payload["files_written"], payload["terminal_state"]) == (0, [], "unchanged")
    assert path.read_text() == before
    sidecar = mcp_cmd.state_path(repo).read_text()
    _assert_no_markers(sidecar)
    for digest in (hashlib.sha256(HELPER.encode()).hexdigest(), localio.stable_hash(HELPER)):
        assert digest[:16] not in sidecar


def test_helper_is_kept_when_modeled_fields_change_on_the_same_origin(box, capsys):
    repo, home = box
    path = _write_claude(home, {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}})
    _import_claude(capsys, repo)
    assert _sync(capsys, repo)[0] == 0
    # Same origin, new path (explicit default port): a real rewrite of the entry. A catalog
    # header here would be a second credential source and is blocked instead (see below).
    _set_catalog_url(repo, "docs", "https://mcp.example.com:443/v2")
    assert _statuses(capsys, repo)["docs"]["status"] == "stale"
    rc, out, payload = _sync(capsys, repo)
    assert rc == 0 and payload["files_written"]
    entry = _claude_servers(path)["docs"]
    assert entry["headersHelper"] == HELPER
    assert entry["url"] == "https://mcp.example.com:443/v2"
    assert "headers" not in entry
    assert _statuses(capsys, repo)["docs"]["status"] == "current"


def test_origin_change_blocks_that_server_only_and_force_cannot_override(box, capsys):
    repo, home = box
    live = {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}}
    path = _write_claude(home, live)
    _import_claude(capsys, repo)
    assert _sync(capsys, repo)[0] == 0
    _set_catalog_url(repo, "docs", OTHER_URL)
    sibling = mcp_adapters.CanonicalServer(name="sibling", transport="http", url="https://sibling.example.com/mcp")
    servers, _, _ = mcp_cmd.load_canonical(repo)
    servers["sibling"] = sibling
    mcp_cmd._write_canonical(repo, servers)
    before = _claude_servers(path)["docs"]

    for force in (False, True):
        rc, out, payload = _sync(capsys, repo, force=force)
        assert rc == 1
        blocked = [i for i in payload["items"] if i["server"] == "docs"]
        assert [(i["status"], i["action"], i["reason"]) for i in blocked] == [
            ("conflicted", "conflict", "native_auth_origin_changed")
        ]
        _assert_no_markers(out)
        docs_rows = [r for r in payload["fidelity"]["servers"] if r["server"] == "docs"]
        assert any(
            f["blocking"] and f["reason"] == "native_auth_origin_changed" for r in docs_rows for f in r["fields"]
        )
        assert _claude_servers(path)["docs"] == before == live["docs"]
    assert _claude_servers(path)["sibling"]["url"] == "https://sibling.example.com/mcp"


def test_remote_turned_stdio_is_blocked_without_carrying_the_helper(box, capsys):
    repo, _ = box
    path = repo / ".mcp.json"
    live = {"mcpServers": {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}}}
    path.write_text(json.dumps(live, indent=2) + "\n")
    assert _run(capsys, mcp_cmd.import_servers, target=repo, harness="claude", merge=True, json_output=True)[0] == 0
    servers, _, _ = mcp_cmd.load_canonical(repo)
    servers["docs"] = mcp_adapters.CanonicalServer(name="docs", transport="stdio", command="npx", args=("-y", "pkg"))
    mcp_cmd._write_canonical(repo, servers)
    rc, out, payload = _sync(capsys, repo, harness="claude")
    assert rc == 1
    assert [i["reason"] for i in payload["items"] if i["server"] == "docs"] == ["native_auth_transport_changed"]
    assert json.loads(path.read_text()) == live
    _assert_no_markers(out)


def test_unknown_native_keys_are_still_replaced_by_a_rewrite(box, capsys):
    repo, home = box
    live = {"docs": {"type": "http", "url": URL, "headersHelper": HELPER, "unrelated": {"x": 1}}}
    path = _write_claude(home, live)
    _import_claude(capsys, repo)
    _set_catalog_url(repo, "docs", "https://mcp.example.com/v2")
    assert _sync(capsys, repo)[0] == 0
    entry = _claude_servers(path)["docs"]
    assert entry["headersHelper"] == HELPER and "unrelated" not in entry


AT = "@"  # spelled out so userinfo test URLs are not mistaken for email addresses
BASE = "https://mcp.example.com/v1"
BASE_ORIGIN = ("https", "mcp.example.com", 443)


def _o(scheme, host, port):
    return (scheme, host, port)


# (url, expected origin or None, True when it names exactly the BASE origin). An origin of None means
# "no provable origin": the helper is never kept for it and an import merge never matches it.
ORIGIN_CASES = [
    # the same origin, spelled the ways that are provably identical
    (BASE, BASE_ORIGIN, True),
    ("HTTPS://MCP.Example.COM/other", BASE_ORIGIN, True),
    ("https://mcp.example.com:443/v1", BASE_ORIGIN, True),
    ("https://mcp.example.com", BASE_ORIGIN, True),
    (f"https://mcp.example.com/v1#frag{AT}evil.com", BASE_ORIGIN, True),
    ("https://mcp.example.com/v1?next=@evil.com", BASE_ORIGIN, True),
    ("https://mcp.example.com/../v1", BASE_ORIGIN, True),
    # a different but well-formed origin
    ("https://mcp.example.com:8443/v1", _o("https", "mcp.example.com", 8443), False),
    ("https://mcp.example.com:65535/v1", _o("https", "mcp.example.com", 65535), False),
    ("http://mcp.example.com/v1", _o("http", "mcp.example.com", 80), False),
    ("http://mcp.example.com:443/v1", _o("http", "mcp.example.com", 443), False),
    ("https://mcp.example.com./v1", _o("https", "mcp.example.com.", 443), False),  # trailing dot kept literally
    ("https://xn--mcp-example-abc.com/v1", _o("https", "xn--mcp-example-abc.com", 443), False),
    ("https://127.1/v1", _o("https", "127.1", 443), False),
    ("https://[::1]/v1", _o("https", "[::1]", 443), False),
    ("https://[::1]:8443/v1", _o("https", "[::1]", 8443), False),
    ("https://[::1:8443]/v1", _o("https", "[::1:8443]", 443), False),  # a different, valid IPv6 literal
    ("https://[0:0:0:0:0:0:0:1]/v1", _o("https", "[0:0:0:0:0:0:0:1]", 443), False),
    ("https://[::ffff:1.2.3.4]/v1", _o("https", "[::ffff:1.2.3.4]", 443), False),
    # parser-differential and malformed forms: never an origin
    ("https://evil.com\\@mcp.example.com/v1", None, False),
    ("https://evil.com/\\@mcp.example.com/v1", None, False),
    ("https:\\\\mcp.example.com/v1", None, False),
    ("https://mcp.example.com\\/v1", None, False),
    (f"https://evil.com{AT}mcp.example.com/v1", None, False),
    (f"https://user:pw{AT}mcp.example.com/v1", None, False),
    (f"https://mcp.example.com{AT}evil.com/v1", None, False),
    ("https://@mcp.example.com/v1", None, False),
    ("https://mcp.example.com%2f.evil.com/v1", None, False),
    ("https://mcp.example.com%40evil.com/v1", None, False),
    ("https://mcp%2eexample.com/v1", None, False),
    ("https://mcp.example.com\t.evil.com/v1", None, False),
    ("https://mcp.example.com\n.evil.com/v1", None, False),
    ("https://mcp.example.com\r\n/v1", None, False),
    ("https://mcp.exam\tple.com/v1", None, False),
    ("https://mcp.example.com\x00/v1", None, False),
    ("https://mcp.example.com\x7f/v1", None, False),
    ("https://mcp.example.com /v1", None, False),
    (" https://mcp.example.com/v1", None, False),
    ("https://mcp.example.com/v1 ", None, False),
    ("https://mcp.example.com/v1\n", None, False),
    ("https://mcp.example.com#@evil.com/v1", None, False),
    ("https://mcp.example.com?@evil.com/v1", None, False),
    ("https://mcp.example.com,evil.com/v1", None, False),
    ("https://mcp.example.com!/v1", None, False),
    ("https://mcp..example.com/v1", None, False),
    ("https://mcp.example.com:0/v1", None, False),
    ("https://mcp.example.com:65536/v1", None, False),
    ("https://mcp.example.com:/v1", None, False),
    ("https://mcp.example.com:0443/v1", None, False),
    ("https://mcp.example.com:+443/v1", None, False),
    ("https://mcp.example.com:44a/v1", None, False),
    ("https://mcp.example.com:443:443/v1", None, False),
    ("https://[::1]:0/v1", None, False),
    ("https://[::1]:/v1", None, False),
    ("https://[::1%25eth0]/v1", None, False),
    ("https://[::1%eth0]/v1", None, False),
    ("https://[::1/v1", None, False),
    ("https://[::1]x/v1", None, False),
    ("https://[zz::1]/v1", None, False),
    ("https://::1/v1", None, False),
    ("https://mcpéxample.com/v1", None, False),  # non-ASCII hosts are refused, never IDNA-mapped
    ("https://ｍｃｐ.example.com/v1", None, False),
    ("https:///v1", None, False),
    ("https://:443/v1", None, False),
    ("mcp.example.com/v1", None, False),
    ("//mcp.example.com/v1", None, False),
    ("https:/mcp.example.com/v1", None, False),
    ("https:mcp.example.com/v1", None, False),
    ("ftp://mcp.example.com/v1", None, False),
    ("file:///etc/passwd", None, False),
    ("javascript:alert(1)", None, False),
    ("", None, False),
    ("not a url", None, False),
]


@pytest.mark.parametrize(("url", "origin", "same_as_base"), ORIGIN_CASES, ids=[repr(c[0]) for c in ORIGIN_CASES])
def test_origin_comparison_fails_closed(url, origin, same_as_base):
    assert mcp_native_auth.url_origin(url) == origin
    assert mcp_native_auth.same_origin(BASE, url) is same_as_base
    assert mcp_native_auth.same_origin(url, BASE) is same_as_base
    assert (mcp_native_auth.normalized_url(url) is None) is (origin is None)
    if origin is not None:  # a canonical URL always matches itself
        assert mcp_native_auth.same_origin(url, url)
    if same_as_base:  # whatever else holds, the authority text really is the base host
        assert re.match(r"https://mcp\.example\.com(:443)?([/?#]|$)", url, re.IGNORECASE)
    else:
        assert not mcp_native_auth.same_origin(url, "https://evil.example.org/v1")


@pytest.mark.parametrize("value", [None, 443, b"https://mcp.example.com/v1", ["https://mcp.example.com/v1"]])
def test_non_string_urls_have_no_origin(value):
    assert mcp_native_auth.url_origin(value) is None
    assert mcp_native_auth.normalized_url(value) is None
    assert mcp_native_auth.same_origin(value, BASE) is False


def test_normalized_url_keeps_ipv6_brackets_and_refuses_userinfo():
    norm = mcp_native_auth.normalized_url
    assert norm("https://[::1]:8443/v1") == "https://[::1]:8443/v1"
    assert norm("https://[::1]:8443/v1") != norm("https://[::1:8443]/v1")
    assert norm(f"https://alice{AT}h.example.com/v1") is None
    assert norm("https://h.example.com:443/v1#x") == "https://h.example.com/v1"
    assert norm("https://h.example.com") == norm("https://H.EXAMPLE.com:443/")
    assert norm("https://h.example.com/mcp") != norm("https://h.example.com/mcp/")
    assert norm("https://h.example.com/mcp?a=1") != norm("https://h.example.com/mcp")
    assert norm("http://h.example.com:0/") is None  # a falsy port is not "the default"
    assert norm("http://h.example.com:80/") == "http://h.example.com/"


def test_entry_fingerprint_is_unchanged_without_a_preserved_key():
    entry = {"type": "http", "url": URL}
    assert mcp_native_auth.entry_fingerprint("claude-user", entry) == localio.stable_hash(entry)
    assert mcp_native_auth.entry_fingerprint("claude-user", entry) == "36288e652913bf08"
    assert mcp_native_auth.entry_fingerprint("claude-user", {**entry, "headersHelper": HELPER}) == localio.stable_hash(
        entry
    )
    # Other harnesses never ignore anything.
    assert mcp_native_auth.entry_fingerprint("cursor-user", {**entry, "headersHelper": "x"}) != localio.stable_hash(
        entry
    )


def _set_catalog_fields(repo, name, **fields):
    path = mcp_cmd.canonical_path(repo)
    doc = json.loads(path.read_text())
    doc["servers"][name].update(fields)
    path.write_text(json.dumps(doc, indent=2) + "\n")


def _blocked(payload, reason, server="docs"):
    rows = [i for i in payload["items"] if i["server"] == server]
    assert [(i["status"], i["action"], i["reason"]) for i in rows] == [("conflicted", "conflict", reason)]
    fidelity = [r for r in payload["fidelity"]["servers"] if r["server"] == server]
    assert any(f["blocking"] and f["reason"] == reason for r in fidelity for f in r["fields"])


@pytest.mark.parametrize("adopt", [False, True])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("owned", [True, False], ids=["owned", "not-owned"])
def test_origin_change_is_blocked_whatever_the_flags_and_ownership(box, capsys, owned, force, adopt):
    repo, home = box
    live = {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}}
    path = _write_claude(home, live)
    _import_claude(capsys, repo)
    if owned:  # Brigade wrote (adopted) the entry once; otherwise it never did
        assert _sync(capsys, repo)[0] == 0
    _set_catalog_url(repo, "docs", OTHER_URL)
    sidecar = mcp_cmd.state_path(repo).read_text() if mcp_cmd.state_path(repo).is_file() else ""
    rc, out, payload = _sync(capsys, repo, force=force, adopt=adopt)
    assert rc == 1
    _blocked(payload, "native_auth_origin_changed")
    assert _claude_servers(path) == live
    assert (mcp_cmd.state_path(repo).read_text() if mcp_cmd.state_path(repo).is_file() else "") == sidecar
    _assert_no_markers(out)


COLLISIONS = [
    {"headers": {"X-Team": {"literal": "blue"}}},
    {"headers": {"Authorization": {"ref": "SOME_TOKEN_VAR"}}},
    # Codex-native carriers no longer collide: the helper outranks them (test_mcp_native_auth_precedence.py).
    {"headers": {"X-Team": {"literal": "blue"}}, "bearer_token_env_var": "SOME_TOKEN_VAR"},
]


@pytest.mark.parametrize("extra", COLLISIONS, ids=[next(iter(c)) for c in COLLISIONS])
@pytest.mark.parametrize("adopt_force", [(False, False), (True, True)])
def test_catalog_auth_next_to_a_live_helper_is_blocked_not_double_written(box, capsys, extra, adopt_force):
    repo, home = box
    live = {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}}
    path = _write_claude(home, live)
    _import_claude(capsys, repo)
    assert _sync(capsys, repo)[0] == 0
    _set_catalog_fields(repo, "docs", **extra)
    adopt, force = adopt_force
    before = path.read_text()
    rc, out, payload = _sync(capsys, repo, adopt=adopt, force=force)
    assert rc == 1
    _blocked(payload, "native_auth_source_collision")
    assert path.read_text() == before and _claude_servers(path) == live
    _assert_no_markers(out)


def test_collision_blocks_one_server_and_still_writes_its_sibling(box, capsys):
    repo, home = box
    path = _write_claude(home, {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}})
    _import_claude(capsys, repo)
    _set_catalog_fields(repo, "docs", headers={"X-Team": {"literal": "blue"}})
    servers, _, _ = mcp_cmd.load_canonical(repo)
    servers["sibling"] = mcp_adapters.CanonicalServer(
        name="sibling", transport="http", url="https://sibling.example.com/mcp"
    )
    mcp_cmd._write_canonical(repo, servers)
    rc, out, payload = _sync(capsys, repo)
    assert rc == 1
    _blocked(payload, "native_auth_source_collision")
    entries = _claude_servers(path)
    assert entries["docs"] == {"type": "http", "url": URL, "headersHelper": HELPER}
    assert entries["sibling"]["url"] == "https://sibling.example.com/mcp"


def test_a_helper_with_no_catalog_auth_is_still_kept(box, capsys):
    """The collision rule needs BOTH sources; a helper alone keeps working."""
    repo, home = box
    path = _write_claude(home, {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}})
    _import_claude(capsys, repo)
    assert _sync(capsys, repo)[0] == 0
    assert _claude_servers(path)["docs"]["headersHelper"] == HELPER


@pytest.mark.parametrize("planned_owned", [True, False], ids=["owned", "not-owned"])
def test_live_file_moving_between_plan_and_write_is_a_clean_conflict(box, capsys, monkeypatch, planned_owned):
    repo, home = box
    path = _write_claude(home, {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}})
    _import_claude(capsys, repo)
    if planned_owned:
        assert _sync(capsys, repo)[0] == 0
    # Same origin, so the plan is a clean update; the live file moves to another origin afterwards.
    _set_catalog_url(repo, "docs", "https://mcp.example.com/v2")
    moved = {"type": "http", "url": OTHER_URL, "headersHelper": HELPER}
    real, calls = mcp_cmd._plan_for_harness, []

    def plan_then_edit_the_live_file(*args, **kwargs):
        result = real(*args, **kwargs)
        if not calls:
            calls.append(1)
            _write_claude(home, {"docs": moved})
        return result

    monkeypatch.setattr(mcp_cmd, "_plan_for_harness", plan_then_edit_the_live_file)
    rc, out, payload = _sync(capsys, repo)  # no traceback: _sync parses the JSON payload
    assert calls and rc == 1
    _blocked(payload, "native_auth_origin_changed")
    assert _claude_servers(path)["docs"] == moved
    _assert_no_markers(out)


# --------------------------------------------------------------------------- #
# Codex auth merge on import
# --------------------------------------------------------------------------- #

CODEX_DOCS = f'''[mcp_servers.docs]
url = "{URL}"
bearer_token_env_var = "{TOKEN_VAR}"
'''


def _import_codex(capsys, repo):
    rc, out = _run(
        capsys, mcp_cmd.import_servers, target=repo, harness="codex-user", merge=True, user_scope=True, json_output=True
    )
    return rc, out, json.loads(out)


def test_codex_auth_merges_into_the_same_server_and_codex_sync_keeps_it(box, capsys):
    repo, home = box
    _write_claude(home, {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}})
    codex = _write_codex(home, CODEX_DOCS)
    _import_claude(capsys, repo)
    rc, out, payload = _import_codex(capsys, repo)
    assert rc == 0
    assert payload["native_auth_merged"] == ["docs"] and payload["skipped_existing"] == []
    assert payload["added"] == []
    _assert_no_markers(out)  # the whole output: nothing is excluded from the leak check
    servers, _, _ = mcp_cmd.load_canonical(repo)
    assert servers["docs"].bearer_token_env_var == TOKEN_VAR

    rc, out, _ = _sync(capsys, repo, harness="codex-user")
    assert rc == 0
    entry = tomllib.loads(codex.read_text())["mcp_servers"]["docs"]
    assert entry["bearer_token_env_var"] == TOKEN_VAR and entry["url"] == URL
    _assert_no_markers(out)


@pytest.mark.parametrize(
    ("catalog_entry", "codex_body", "merged", "skipped", "added"),
    [
        # same name, different URL
        ({"transport": "http", "url": OTHER_URL}, CODEX_DOCS, [], ["docs"], []),
        # different name, same URL: a new entry, never a merge into the other name
        ({"transport": "http", "url": URL}, CODEX_DOCS.replace("docs", "wiki"), [], [], ["wiki"]),
        # existing entry already has native auth: never overwritten
        (
            {"transport": "http", "url": URL, "bearer_token_env_var": "KEEP_ME"},
            CODEX_DOCS,
            [],
            ["docs"],
            [],
        ),
        # existing entry carries a sensitive generic header
        (
            {"transport": "http", "url": URL, "headers": {"Authorization": {"ref": "KEEP_ME"}}},
            CODEX_DOCS,
            [],
            ["docs"],
            [],
        ),
        # existing entry is stdio
        ({"transport": "stdio", "command": "npx"}, CODEX_DOCS, [], ["docs"], []),
        # ANY header on the existing entry counts as auth, not only names that look sensitive
        ({"transport": "http", "url": URL, "headers": {"X-Auth": {"literal": "fake"}}}, CODEX_DOCS, [], ["docs"], []),
        ({"transport": "http", "url": URL, "http_headers": {"X-Team": "blue"}}, CODEX_DOCS, [], ["docs"], []),
        ({"transport": "http", "url": URL, "env_http_headers": {"X-Team": "FAKE_VAR"}}, CODEX_DOCS, [], ["docs"], []),
    ],
)
def test_codex_auth_is_not_merged_unless_name_url_and_authlessness_all_match(
    box, capsys, catalog_entry, codex_body, merged, skipped, added
):
    repo, home = box
    mcp_cmd.canonical_path(repo).write_text(json.dumps({"servers": {"docs": catalog_entry}}))
    _write_codex(home, codex_body)
    rc, out, payload = _import_codex(capsys, repo)
    assert rc == 0
    assert (payload["native_auth_merged"], payload["skipped_existing"], payload["added"]) == (merged, skipped, added)
    servers, _, _ = mcp_cmd.load_canonical(repo)
    expected = catalog_entry.get("bearer_token_env_var")
    assert servers["docs"].bearer_token_env_var == expected
    if "wiki" in added:
        assert servers["wiki"].bearer_token_env_var == TOKEN_VAR


@pytest.mark.parametrize(
    ("catalog_url", "codex_url"),
    [
        (f"https://alice{AT}mcp.example.com/v1", URL),  # userinfo is never "the same server"
        (URL, f"https://alice{AT}mcp.example.com/v1"),
        ("https://[::1]:8443/v1", "https://[::1:8443]/v1"),  # different IPv6 literals
        ("https://mcp.example.com\\@evil.example.net/v1", "https://mcp.example.com\\@evil.example.net/v1"),
        ("https://mcp.example.com:0/v1", "https://mcp.example.com:0/v1"),  # falsy port is not the default
        ("https://mcp.example.com/v1", "https://mcp.example.com/v1 "),
    ],
)
def test_merge_never_attaches_auth_when_either_url_is_not_canonical_or_equal(catalog_url, codex_url):
    existing = mcp_adapters.CanonicalServer(name="docs", transport="http", url=catalog_url)
    imported = mcp_adapters.CanonicalServer(
        name="docs", transport="http", url=codex_url, bearer_token_env_var=TOKEN_VAR
    )
    assert mcp_native_auth.merge_imported_native_auth(existing, imported) is None


def test_merge_attaches_auth_for_the_same_canonical_url_only():
    existing = mcp_adapters.CanonicalServer(name="docs", transport="http", url="https://[::1]:8443/v1")
    imported = mcp_adapters.CanonicalServer(
        name="docs", transport="http", url="HTTPS://[::1]:8443/v1#x", bearer_token_env_var=TOKEN_VAR
    )
    merged = mcp_native_auth.merge_imported_native_auth(existing, imported)
    assert merged is not None and merged.bearer_token_env_var == TOKEN_VAR


def test_codex_auth_merge_is_reported_in_preview_without_writing(box, capsys):
    repo, home = box
    mcp_cmd.canonical_path(repo).write_text(json.dumps({"servers": {"docs": {"transport": "http", "url": URL}}}))
    _write_codex(home, CODEX_DOCS)
    rc, out = _run(capsys, mcp_cmd.import_servers, target=repo, harness="codex-user", user_scope=True, json_output=True)
    assert json.loads(out)["native_auth_merged"] == ["docs"]
    assert mcp_cmd.load_canonical(repo)[0]["docs"].bearer_token_env_var is None


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_no_command_or_token_name_reaches_any_output_or_the_sidecar(box, capsys):
    repo, home = box
    path = _write_claude(home, {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}})
    _write_codex(home, CODEX_DOCS)
    captured: list[str] = []

    def run(fn, **kw):
        for as_json in (True, False):
            rc, out = _run(capsys, fn, json_output=as_json, **kw)
            captured.append(out)
        return rc

    run(mcp_cmd.import_servers, target=repo, harness="claude-user", user_scope=True)
    run(mcp_cmd.import_servers, target=repo, harness="claude-user", merge=True, user_scope=True)
    run(mcp_cmd.import_servers, target=repo, harness="codex-user", merge=True, user_scope=True)
    run(mcp_cmd.list_servers, target=repo)
    run(mcp_cmd.doctor, target=repo)
    for harness in ("claude-user", "codex-user"):
        run(mcp_cmd.plan, target=repo, harness=harness, user_scope=True)
        run(mcp_cmd.sync, target=repo, harness=harness, user_scope=True, adopt=True, force=True, write=True)
    _set_catalog_url(repo, "docs", OTHER_URL)
    run(mcp_cmd.plan, target=repo, harness="claude-user", user_scope=True)
    run(mcp_cmd.sync, target=repo, harness="claude-user", user_scope=True, adopt=True, force=True, write=True)
    for text in captured:
        _assert_no_markers(text)
    # The catalog legitimately holds the Codex token variable name, but never the helper.
    assert "MARKER_HELPER_7f3a" not in mcp_cmd.canonical_path(repo).read_text()
    _assert_no_markers(mcp_cmd.state_path(repo).read_text())
    assert _claude_servers(path)["docs"]["headersHelper"] == HELPER


def test_public_items_fidelity_and_import_errors_stay_value_free(box, capsys):
    repo, home = box
    _write_claude(home, {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}})
    _import_claude(capsys, repo)
    _set_catalog_url(repo, "docs", OTHER_URL)
    servers, _, _ = mcp_cmd.load_canonical(repo)
    state = mcp_cmd._load_state(repo)
    items = mcp_cmd._plan_for_harness(repo, "claude-user", servers, state, force=True, prune=False, adopt=True)
    rendered = json.dumps(
        [mcp_cmd._public_items(items), mcp_cmd._fidelity_report(items, servers, ["claude-user"])], sort_keys=True
    )
    assert "native_auth_origin_changed" in rendered
    _assert_no_markers(rendered, str(mcp_native_auth.NativeAuthEndpointChanged("native_auth_origin_changed")))
    # A malformed Codex variable name is refused without echoing it.
    _write_codex(home, f'[mcp_servers.bad]\nurl = "{URL}"\nbearer_token_env_var = "bad {TOKEN_VAR} value"\n')
    rc, out = _run(
        capsys, mcp_cmd.import_servers, target=repo, harness="codex-user", merge=True, user_scope=True, json_output=True
    )
    assert rc == 2
    _assert_no_markers(out)
