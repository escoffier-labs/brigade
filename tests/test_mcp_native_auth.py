"""#2678: remote-server auth survives `mcp import` and `mcp sync`.

Real code paths in a scratch HOME. The Claude Code ``headersHelper`` stays out of the
catalog and the sidecar, is kept on the same live entry while the endpoint is unchanged,
and blocks the projection (no override) when the endpoint would change.
"""

from __future__ import annotations

import hashlib
import json
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
    # Same host, new path and a new generated header: a real rewrite of the entry.
    doc = json.loads(mcp_cmd.canonical_path(repo).read_text())
    doc["servers"]["docs"]["url"] = "https://mcp.example.com:443/v2"
    doc["servers"]["docs"]["headers"] = {"X-Team": {"literal": "blue"}}
    mcp_cmd.canonical_path(repo).write_text(json.dumps(doc, indent=2) + "\n")
    assert _statuses(capsys, repo)["docs"]["status"] == "stale"
    rc, out, payload = _sync(capsys, repo)
    assert rc == 0 and payload["files_written"]
    entry = _claude_servers(path)["docs"]
    assert entry["headersHelper"] == HELPER
    assert entry["url"] == "https://mcp.example.com:443/v2"
    assert entry["headers"] == {"X-Team": "blue"}
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
    doc = json.loads(mcp_cmd.canonical_path(repo).read_text())
    doc["servers"]["docs"]["headers"] = {"X-Team": {"literal": "blue"}}
    mcp_cmd.canonical_path(repo).write_text(json.dumps(doc, indent=2) + "\n")
    assert _sync(capsys, repo)[0] == 0
    entry = _claude_servers(path)["docs"]
    assert entry["headersHelper"] == HELPER and "unrelated" not in entry


def test_origin_and_url_normalization():
    origin = mcp_native_auth.url_origin
    assert origin("HTTPS://Mcp.Example.com/v1") == origin("https://mcp.example.com:443/other")
    assert origin("http://h:80/") == origin("http://h/")
    assert origin("https://h/") != origin("http://h/")
    assert origin("https://h:8443/") != origin("https://h/")
    assert origin("https://a.example.com/") != origin("https://b.example.com/")
    assert origin("not a url") is None and origin(None) is None and origin("https://h:bad/") is None
    norm = mcp_native_auth.normalized_url
    assert norm("https://H.example.com:443") == norm("https://h.example.com/")
    assert norm("https://h/mcp") != norm("https://h/mcp/")
    assert norm("https://h/mcp?a=1") != norm("https://h/mcp")


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
    _assert_no_markers(*(line for line in out.splitlines() if "bearer" not in line))
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
