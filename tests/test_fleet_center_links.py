"""Offline metadata and browser-link security contracts for optional Center URLs."""

from __future__ import annotations

import json
from html.parser import HTMLParser

import pytest

from brigade import cli, fleet_client, node

NODE_ID = "00000000-0000-4000-8000-000000000001"
LEGACY_TOML = f'node_id = "{NODE_ID}"\nhostname = "fixture-node"\nroles = ["worker"]\nplatform = "fixture-platform"\n'
LEGACY_JSON = {
    "node_id": NODE_ID,
    "hostname": "fixture-node",
    "roles": ["worker"],
    "platform": "fixture-platform",
    "short_id": "00000000",
}


def write_node(target, optional_line=""):
    path = target / ".brigade" / "node.toml"
    path.parent.mkdir(parents=True)
    path.write_text(LEGACY_TOML + optional_line)
    return path


def test_legacy_constructor_and_node_file_keep_unset_json_shape(tmp_path):
    old = node.NodeIdentity(NODE_ID, "fixture-node", ("worker",), "fixture-platform")
    path = write_node(tmp_path)
    loaded = node.ensure_identity(tmp_path)
    assert loaded == old
    assert loaded.to_dict() == LEGACY_JSON
    assert loaded.center_url is None
    assert loaded.center_url_status == "unset"
    assert path.read_text() == LEGACY_TOML


def test_configured_metadata_is_loaded_without_rewriting_identity(tmp_path, capsys):
    url = "https://center.example.invalid:8443/Center/%E2%98%83"
    text = LEGACY_TOML + f'center_url = "{url}"\n'
    path = write_node(tmp_path, f'center_url = "{url}"\n')
    loaded = node.ensure_identity(tmp_path)
    assert loaded.center_url == url
    assert loaded.center_url_status == "configured"
    assert loaded.to_dict() == {**LEGACY_JSON, "center_url": url, "center_url_status": "configured"}
    assert cli.main(["node", "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == loaded.to_dict()
    assert cli.main(["node", "--target", str(tmp_path)]) == 0
    assert f"center_url: {url} (configured; unverified)" in capsys.readouterr().out
    assert path.read_text() == text


@pytest.mark.parametrize(
    "optional_line",
    [
        "center_url = 42\n",
        "center_url = true\n",
        'center_url = ["https://center.example.invalid"]\n',
        'center_url = { url = "https://center.example.invalid" }\n',
        'center_url = ""\n',
        'center_url = "https://user:secret@example.invalid/"\n',
        'center_url = "https://center.example.invalid/\\n"\n',
    ],
)
def test_invalid_optional_metadata_preserves_identity(tmp_path, capsys, optional_line):
    path = write_node(tmp_path, optional_line)
    before = path.read_bytes()
    loaded = node.ensure_identity(tmp_path)
    assert loaded.node_id == NODE_ID
    assert loaded.hostname == "fixture-node"
    assert loaded.roles == ("worker",)
    assert loaded.platform == "fixture-platform"
    assert loaded.center_url is None
    assert loaded.center_url_status == "invalid"
    assert loaded.to_dict() == {**LEGACY_JSON, "center_url": None, "center_url_status": "invalid"}
    assert node.run(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "center_url: (invalid; link suppressed)" in out
    assert "user:secret" not in out
    assert path.read_bytes() == before


def test_default_init_keeps_url_unset_and_existing_identity_roles(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(node.socket, "gethostname", lambda: "fixture-node.example.invalid")
    assert cli.main(["node", "--target", str(tmp_path), "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["roles"] == []
    assert "center_url" not in first
    path = node.node_path(tmp_path)
    before = path.read_bytes()
    assert b"center_url" not in before
    assert cli.main(["node", "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert node.load_identity(tmp_path).center_url_status == "unset"
    assert path.read_bytes() == before


def test_workspace_metadata_does_not_override_home_machine_authority(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BRIGADE_HOME", raising=False)
    machine_path = write_node(home, 'center_url = "https://machine.example.invalid/"\n')
    workspace_path = write_node(workspace, 'center_url = "https://workspace.example.invalid/"\n')
    workspace_path.write_text(workspace_path.read_text().replace(NODE_ID, "00000000-0000-4000-8000-000000000002"))
    assert cli.main(["node", "--target", str(workspace), "--json"]) == 0
    workspace_json = json.loads(capsys.readouterr().out)
    assert workspace_json["center_url"] == "https://workspace.example.invalid/"
    assert cli.main(["node", "--machine", "--json"]) == 0
    machine_json = json.loads(capsys.readouterr().out)
    assert machine_json["node_id"] == NODE_ID
    assert machine_json["center_url"] == "https://machine.example.invalid/"
    assert workspace_json["node_id"] != machine_json["node_id"]
    assert fleet_client.resolve_node_id(workspace) == NODE_ID
    machine_path.write_text(LEGACY_TOML + 'center_url = "javascript:alert(1)"\n')
    assert fleet_client.resolve_node_id(workspace) == NODE_ID
    assert machine_path.read_text().endswith('center_url = "javascript:alert(1)"\n')


@pytest.mark.parametrize(
    "url",
    [
        "https://center.example.invalid",
        "HTTPS://CENTER.example.invalid/Center",
        "http://CENTER.example.invalid:65535/Center/%E2%98%83",
        "https://center.example.invalid:1/a%2Fb&c'd",
    ],
)
def test_valid_url_path_spelling_is_preserved(url):
    from brigade import fleet_center_links as links

    assert links.validate_center_url(url) == url
    assert links.center_url_status(url) == "configured"
    assert links.render_center_link(url).endswith(">Open Center</a>")


@pytest.mark.parametrize(
    "unsafe",
    [
        7,
        False,
        ["https://center.example.invalid"],
        {"url": "https://center.example.invalid"},
        "",
        " https://center.example.invalid",
        "https://center.example.invalid/has space",
        "https://center.example.invalid/\t",
        "https://center.example.invalid/\n",
        "https://center.example.invalid/\r",
        "https://center.example.invalid/\x00",
        "https://center.example.invalid/\x7f",
        "https://center.example.invalid/\u00a0",
        "https://center.example.invalid/雪",
        "https://cénter.example.invalid/",
        "https://center.example.invalid/\ud800",
        "https://user:secret@center.example.invalid/",
        "https://@center.example.invalid/",
        "javascript:alert(1)",
        "file:///center.example.invalid/",
        "ftp://center.example.invalid/",
        "//center.example.invalid/",
        "https:////center.example.invalid/",
        "https:/center.example.invalid/",
        "https://",
        "https://-center.example.invalid/",
        "https://center_.example.invalid/",
        "https://center..example.invalid/",
        "https://center.example.invalid./",
        "https://" + "a" * 64 + ".invalid/",
        "https://" + ("a" * 63 + ".") * 4 + "invalid/",
        "https://123/",
        "https://0xabcdef/",
        "https://0x/",
        "https://center.0X/",
        "https://center.123/",
        "https://center.example.invalid:/",
        "https://center.example.invalid:0/",
        "https://center.example.invalid:65536/",
        "https://center.example.invalid:-1/",
        "https://center.example.invalid:abc/",
        "https://center.example.invalid:１２/",
        "https://center.example.invalid:80:90/",
        "https://center.example.invalid/?token=secret",
        "https://center.example.invalid/?",
        "https://center.example.invalid/#secret",
        "https://center.example.invalid/#",
        "https://center.example.invalid\\@other.example.invalid/",
        "https://center.example.invalid/path\\next",
        "https://%63enter.example.invalid/",
        "https://center.example.invalid%2fother.example.invalid/",
        "https://center.example.invalid%40other.example.invalid/",
        "https://center.example.invalid%5cother.example.invalid/",
        "https://center.example.invalid%3a443/",
        "https://[center.example.invalid]/",
        "https://center.example.invalid/%",
        "https://center.example.invalid/%GG",
        'https://center.example.invalid/"onmouseover="injected?secret',
    ],
)
def test_unsafe_url_never_becomes_a_link(unsafe):
    from brigade import fleet_center_links as links

    assert links.validate_center_url(unsafe) is None
    assert links.center_url_status(unsafe) == "invalid"
    assert links.render_center_link(unsafe) == ""


def test_unset_metadata_suppresses_link():
    from brigade import fleet_center_links as links

    assert links.center_url_status(None) == "unset"
    assert links.render_center_link(None) == ""


def test_length_bound_is_applied_before_link_rendering():
    from brigade import fleet_center_links as links

    prefix = "https://center.example.invalid/"
    boundary = prefix + "a" * (2048 - len(prefix))
    assert links.validate_center_url(boundary) == boundary
    assert links.render_center_link(boundary)
    assert links.validate_center_url(boundary + "a") is None
    assert links.render_center_link(boundary + "a") == ""


def test_anchor_escapes_attribute_markup_without_changing_destination():
    from brigade import fleet_center_links as links

    url = 'https://center.example.invalid/"onclick="x/<tag>&\'value'
    anchor = links.render_center_link(url)

    class ParsedAnchor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []
            self.labels = []

        def handle_starttag(self, tag, attrs):
            self.tags.append((tag, attrs))

        def handle_data(self, data):
            self.labels.append(data)

    parsed = ParsedAnchor()
    parsed.feed(anchor)
    assert parsed.tags == [("a", [("href", url)])]
    assert parsed.labels == ["Open Center"]
    assert "&quot;" in anchor
    assert "&lt;tag&gt;" in anchor
    assert "&amp;" in anchor
    assert "&#x27;" in anchor
