"""Deterministic selected-project diagnosis and its explicit output boundaries."""

import json
from pathlib import Path

import pytest

from brigade import cli


def seed(tmp_path):
    target = tmp_path / "project"
    (target / ".brigade").mkdir(parents=True)
    (target / ".brigade/mcp.json").write_text(
        json.dumps({"version": 1, "servers": {"demo": {"command": "example-server"}}})
    )
    return target


def call(target, capsys, *extra, expected=0):
    assert (
        cli.main(
            [
                "bug",
                "diagnose",
                "--target",
                str(target),
                "--harness",
                "codex",
                "--summary",
                "Missing selected server",
                "--json",
                *extra,
            ]
        )
        == expected
    )
    return json.loads(capsys.readouterr().out)


def test_missing_is_real_comparison_and_read_only(tmp_path, capsys):
    target = seed(tmp_path)
    before = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    result = call(target, capsys)
    assert result["findings"][0]["status"] == "missing"
    assert result["verification"] == "not-run"
    assert result["runtime"] == "unknown"
    assert "Evidence digest:" in result["text"]
    assert "--write" not in result["text"]
    assert {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("malformed", [False, True])
def test_preview_never_records_last_error_or_invokes_updates(tmp_path, capsys, monkeypatch, malformed):
    from brigade import bug_last_error, update_notify

    target = seed(tmp_path)
    if malformed:
        (target / ".brigade/mcp.json").write_text("{")

    def forbidden(*args, **kwargs):
        pytest.fail("diagnosis invoked a global failure or update hook")

    monkeypatch.setattr(bug_last_error, "record", forbidden)
    monkeypatch.setattr(update_notify, "maybe_notify", forbidden)
    result = call(target, capsys, expected=2 if malformed else 0)
    assert result["status"] == ("unavailable" if malformed else "available")


@pytest.mark.parametrize("native", ["[broken", "mcp_servers = []"])
def test_malformed_native_is_evidence_error(tmp_path, capsys, native):
    target = seed(tmp_path)
    (target / ".codex").mkdir()
    (target / ".codex/config.toml").write_text(native)
    assert call(target, capsys, expected=2)["status"] == "unavailable"


def native(target, text='[mcp_servers.demo]\ncommand = "example-server"\n'):
    (target / ".codex").mkdir(exist_ok=True)
    (target / ".codex/config.toml").write_text(text)


def test_current_conflict_stale_and_excluded(tmp_path, capsys):
    from brigade import localio, mcp_adapters

    target = seed(tmp_path)
    native(target)
    assert call(target, capsys)["findings"][0]["status"] == "current"
    server, _ = mcp_adapters.server_from_dict("demo", {"command": "example-server"})
    owned = {
        "canonical_fingerprint": localio.stable_hash(mcp_adapters.server_to_dict(server)),
        "projected_fingerprint": localio.stable_hash({"command": "example-server"}),
    }
    (target / ".brigade/mcp").mkdir()
    (target / ".brigade/mcp/state.json").write_text(
        json.dumps({"ownership": {"codex": {".codex/config.toml": {"demo": owned}}}})
    )
    native(target, '[mcp_servers.demo]\ncommand = "external-change"\n')
    assert call(target, capsys)["findings"][0]["status"] == "conflicted"
    native(target)
    catalog = target / ".brigade/mcp.json"
    catalog.write_text(json.dumps({"servers": {"demo": {"command": "catalog-change"}}}))
    assert call(target, capsys)["findings"][0]["status"] == "stale"
    catalog.write_text(json.dumps({"servers": {"demo": {"command": "catalog-change", "enabled": False}}}))
    assert call(target, capsys)["findings"][0]["status"] == "excluded"


@pytest.mark.parametrize("relative", [".brigade/mcp.json", ".brigade/mcp/state.json", ".codex/config.toml"])
@pytest.mark.parametrize("attack", ["symlink", "hardlink", "oversized", "directory"])
def test_unsafe_inputs_fail_closed(tmp_path, capsys, relative, attack):
    target = seed(tmp_path)
    path = target / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    outside = tmp_path / "private"
    outside.write_text("private data")
    if attack == "symlink":
        path.symlink_to(outside)
    elif attack == "hardlink":
        path.hardlink_to(outside)
    elif attack == "oversized":
        path.write_bytes(b" " * (65536 + 1))
    else:
        path.mkdir()
    assert call(target, capsys, expected=2)["status"] == "unavailable"


@pytest.mark.parametrize("relative", [".brigade", ".codex"])
def test_parent_links_fail_closed(tmp_path, capsys, relative):
    target = seed(tmp_path)
    directory = target / relative
    if directory.exists():
        directory.rename(tmp_path / "outside")
    else:
        (tmp_path / "outside").mkdir()
    directory.symlink_to(tmp_path / "outside", target_is_directory=True)
    assert call(target, capsys, expected=2)["status"] == "unavailable"


def test_reject_user_scope_before_any_read(tmp_path, monkeypatch, capsys):
    from brigade import bug_diagnose

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported adapter caused a read")

    monkeypatch.setattr(bug_diagnose, "_inputs", forbidden)
    for harness in ("codex-user", "hermes", "missing"):
        assert cli.main(["bug", "diagnose", "--harness", harness, "--summary", "example", "--json"]) == 2
        assert json.loads(capsys.readouterr().out)["status"] == "unavailable"


def test_exact_read_footprint_and_no_ambient_projection(tmp_path, capsys, monkeypatch):
    from brigade import bug_diagnose, mcp_cmd

    target = seed(tmp_path)
    seen = []
    read = bug_diagnose._read

    def tracked(root, relative):
        seen.append(relative)
        return read(root, relative)

    def forbidden(*args, **kwargs):
        pytest.fail("ambient read or broad doctor")

    monkeypatch.setattr(bug_diagnose, "_read", tracked)
    monkeypatch.setattr(mcp_cmd, "_load_state", forbidden)
    monkeypatch.setattr(mcp_cmd, "doctor", forbidden)
    (target / ".mcp.json").symlink_to(tmp_path / "absent")
    call(target, capsys)
    assert seen == [".brigade/mcp.json", ".brigade/mcp/state.json", ".codex/config.toml"]


def test_malformed_sidecar_and_limits(tmp_path, capsys):
    target = seed(tmp_path)
    (target / ".brigade/mcp").mkdir()
    state = target / ".brigade/mcp/state.json"
    for value in ("broken", "[]", '{"ownership": []}', '{"ownership": {"codex": []}}'):
        state.write_text(value)
        assert call(target, capsys, expected=2)["status"] == "unavailable"
    state.unlink()
    (target / ".brigade/mcp.json").write_text(
        json.dumps({"servers": {str(i): {"command": "example"} for i in range(101)}})
    )
    assert call(target, capsys, "--name", "0", expected=2)["status"] == "unavailable"


def test_export_validates_imports_pending_and_is_exclusive(tmp_path, capsys):
    from tests.work_cmd_test_helpers import _init_git_repo

    target = seed(tmp_path)
    _init_git_repo(target)
    path = tmp_path / "finding.jsonl"
    result = call(target, capsys, "--save-finding", str(path))
    record = json.loads(path.read_text())
    assert record == {"kind": "finding", "source": "assisted-diagnosis", "text": result["text"]}
    assert cli.main(["work", "import", "validate", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    args = ["work", "import", "ingest", str(path), "--target", str(target), "--json"]
    assert cli.main([*args, "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["created"] == 1
    assert cli.main(args) == 0
    item = json.loads(capsys.readouterr().out)["imports"][0]
    assert item["status"] == "pending"
    assert item["metadata"]["declared_source"] == "assisted-diagnosis"
    assert "task_id" not in item
    before = path.read_bytes()
    assert call(target, capsys, "--save-finding", str(path), expected=2)["status"] == "unavailable"
    assert path.read_bytes() == before


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("output", ["finding", "security-report"])
def test_output_refusal_is_distinct_from_evidence_failure(tmp_path, capsys, monkeypatch, structured, output):
    target = seed(tmp_path)
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    destination = tmp_path / "finding.jsonl"
    destination.write_text("preserve existing finding\n")
    args = ["bug", "diagnose", "--target", str(target), "--harness", "codex", "--summary"]
    if output == "finding":
        args += ["Missing selected server", "--save-finding", str(destination)]
    else:
        args += ["Suspected security vulnerability", "--prepare-report"]
    if structured:
        args.append("--json")
    assert cli.main(args) == 2
    rendered = capsys.readouterr().out
    message = json.loads(rendered)["text"] if structured else rendered
    assert "evidence is unsupported" not in message
    assert ("SECURITY.md" if output == "security-report" else "destination") in message
    assert destination.read_text() == "preserve existing finding\n"
    assert not data.exists()


def test_redaction_of_all_output_inputs(tmp_path, capsys, monkeypatch):
    target = seed(tmp_path)
    token = "ghp_" + "a" * 36
    label = token + "\x1b[31m"
    monkeypatch.setenv("EXAMPLE_SECRET_TOKEN", "secret-fixture-value")
    (target / ".brigade/mcp.json").write_text(
        json.dumps({"servers": {label: {"command": "example", "env": {"TOKEN": {"ref": "EXAMPLE_SECRET_TOKEN"}}}}})
    )
    output = tmp_path / "finding.jsonl"
    assert (
        cli.main(
            [
                "bug",
                "diagnose",
                "--target",
                str(target),
                "--harness",
                "codex",
                "--name",
                label,
                "--summary",
                token + "\x1b[31m secret-fixture-value https://example.test/private",
                "--save-finding",
                str(output),
                "--json",
            ]
        )
        == 0
    )
    rendered = capsys.readouterr().out + output.read_text()
    for secret in (token, "\x1b", "secret-fixture-value", "https://example.test/private"):
        assert secret not in rendered
    result = json.loads(rendered.splitlines()[0])
    assert result["runtime"] == "unknown" and result["verification"] == "not-run"


def test_prepare_uses_local_report_controls(tmp_path, capsys, monkeypatch):
    import socket
    import subprocess
    from brigade import bug_cmd, bug_collect, bug_last_error, extras

    target = seed(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # Select the CLI surface before guarding evidence collection. Parser setup
    # consults the installed extras marker independently of report preparation.
    parser = cli._build_parser()
    monkeypatch.setattr(cli, "_build_parser", lambda: parser)

    def forbidden(*args, **kwargs):
        pytest.fail("report preparation attempted ambient collection or transport")

    monkeypatch.setattr(bug_last_error, "read", forbidden)
    monkeypatch.setattr(bug_collect, "_install_channel", forbidden)
    monkeypatch.setattr(bug_collect, "recent_failures", forbidden)
    monkeypatch.setattr(extras, "enabled", forbidden)
    monkeypatch.setattr(bug_cmd, "collect_diagnostics", forbidden)
    monkeypatch.setattr(bug_cmd, "collect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(bug_cmd, "gh", forbidden)
    monkeypatch.setattr(bug_cmd, "urlopen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    result = call(target, capsys, "--prepare-report")
    assert result["submitted"] is False
    receipt = json.loads(Path(result["receipt_path"]).read_text())
    assert receipt["consent"]["mode"] == "none"
    assert receipt["destination"] == "file"
    assert receipt["submitted"] is False
    bundle = json.loads(Path(result["preview_path"]).with_name("bundle.json").read_text())
    assert bundle["schema"] == "brigade.bug-report.v1"
    assert "verification: not-run" in bundle["user"]["summary"]
    assert bundle["last_error"] is None
    assert bundle["recent_failures"] == {"runs": [], "verify": []}


@pytest.mark.parametrize(
    "field,value",
    [
        ("env", ["INVALID"]),
        ("env", {"EXAMPLE": {"ref": 7}}),
        ("headers", 7),
        ("headers", {"Example": {"literal": []}}),
        ("timeout", "not-a-number"),
        ("timeout", True),
        ("timeout", -1),
        ("http_headers", []),
        ("env_http_headers", False),
        ("bearer_token_env_var", 7),
    ],
)
def test_malformed_catalog_field_cannot_report_current(tmp_path, capsys, field, value):
    target = seed(tmp_path)
    native(target)
    (target / ".brigade/mcp.json").write_text(
        json.dumps({"servers": {"demo": {"command": "example-server", field: value}}})
    )
    result = call(target, capsys, expected=2)
    assert result["status"] == "unavailable"


@pytest.mark.parametrize(
    "extra,native_text",
    [
        ({"targets": None}, '[mcp_servers.demo]\ncommand = "example-server"\n'),
        (
            {"env": {"MODE": "development"}},
            '[mcp_servers.demo]\ncommand = "example-server"\n[mcp_servers.demo.env]\nMODE = "development"\n',
        ),
        (
            {"headers": {"X-Example": "development"}},
            '[mcp_servers.demo]\ncommand = "example-server"\n',
        ),
    ],
)
def test_supported_catalog_forms_remain_available(tmp_path, capsys, extra, native_text):
    target = seed(tmp_path)
    (target / ".brigade/mcp.json").write_text(json.dumps({"servers": {"demo": {"command": "example-server", **extra}}}))
    native(target, native_text)
    result = call(target, capsys)
    assert result["status"] == "available"
    assert result["verification"] == "not-run" and result["runtime"] == "unknown"


@pytest.mark.parametrize(
    "harness,relative,content",
    [
        ("codex", ".codex/config.toml", "[mcp_servers.demo]\ncommand = 7\n"),
        ("claude", ".mcp.json", '{"mcpServers":{"demo":{"command":7}}}'),
        ("cursor", ".cursor/mcp.json", '{"mcpServers":{"demo":{"env":[]}}}'),
        ("vscode", ".vscode/mcp.json", '{"servers":{"demo":{"args":7}}}'),
        ("grok", ".grok/config.toml", "[mcp_servers.demo]\nheaders = []\n"),
        ("opencode", "opencode.json", '{"mcp":{"demo":{"type":"local","command":7}}}'),
    ],
)
def test_malformed_native_fields_are_unavailable(tmp_path, capsys, harness, relative, content):
    target = seed(tmp_path)
    path = target / relative
    path.parent.mkdir(exist_ok=True)
    path.write_text(content)
    result = call(target, capsys, "--harness", harness, expected=2)
    assert result["status"] == "unavailable"


def test_selected_reads_request_binary_mode(tmp_path, capsys, monkeypatch):
    from brigade import bug_diagnose

    target = seed(tmp_path)
    binary_flag = 1 << 29
    monkeypatch.setattr(bug_diagnose.os, "O_BINARY", binary_flag, raising=False)
    open_file = bug_diagnose.dirfd.open_child_file
    opened = []

    def checked(parent, name, flags, *args, **kwargs):
        assert flags & binary_flag
        opened.append(name)
        return open_file(parent, name, flags & ~binary_flag, *args, **kwargs)

    monkeypatch.setattr(bug_diagnose.dirfd, "open_child_file", checked)
    assert call(target, capsys)["status"] == "available"
    assert "mcp.json" in opened


def test_binary_reader_preserves_crlf_and_control_z(tmp_path):
    import os
    from brigade import bug_diagnose, dirfd

    target = seed(tmp_path)
    raw = b'{"servers":{}}\r\n\x1aINVALID'
    (target / ".brigade/mcp.json").write_bytes(raw)
    root = dirfd.open_directory_chain(target, writable=False)
    try:
        assert bug_diagnose._read(root, ".brigade/mcp.json") == raw
    finally:
        os.close(root)


def test_missing_catalog_and_unsupported_platform(tmp_path, capsys, monkeypatch):
    from brigade import dirfd

    target = seed(tmp_path)
    (target / ".brigade/mcp.json").unlink()
    assert call(target, capsys, expected=2)["status"] == "unavailable"
    target = seed(tmp_path / "other")
    monkeypatch.setattr(dirfd, "posix_available", lambda: False)
    monkeypatch.setattr(dirfd, "nt_available", lambda: False)
    assert call(target, capsys, expected=2)["status"] == "unavailable"


def test_selected_project_ancestor_refused(tmp_path, capsys):
    target = seed(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    assert call(alias, capsys, expected=2)["status"] == "unavailable"


@pytest.mark.parametrize(
    "harness,relative,content",
    [
        ("claude", ".mcp.json", "broken"),
        ("claude", ".mcp.json", "[]"),
        ("claude", ".mcp.json", ""),
        ("opencode", "opencode.json", '{"mcp": []}'),
    ],
)
def test_json_native_validation_precedes_permissive_parser(tmp_path, capsys, harness, relative, content):
    target = seed(tmp_path)
    (target / relative).write_text(content)
    assert (
        cli.main(["bug", "diagnose", "--target", str(target), "--harness", harness, "--summary", "example", "--json"])
        == 2
    )
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"


def test_suggestion_quotes_selected_name_after_redaction(tmp_path, capsys):
    import shlex

    target = seed(tmp_path)
    label = "example; $(untrusted) ' name"
    (target / ".brigade/mcp.json").write_text(json.dumps({"servers": {label: {"command": "example"}}}))
    result = call(target, capsys, "--name", label)
    command = result["next_check"].removeprefix("From the selected project: ")
    assert shlex.split(command) == ["brigade", "mcp", "plan", "--target", ".", "--harness", "codex", "--name", label]


def test_hundred_servers_export_tracks_public_findings_not_whitespace(tmp_path, capsys):
    target = seed(tmp_path)
    path = target / ".brigade/mcp.json"
    path.write_text(json.dumps({"servers": {f"server-{i}": {"command": "example"} for i in range(100)}}))
    first = call(target, capsys, "--save-finding", str(tmp_path / "first.jsonl"))
    assert len(first["findings"]) == 100
    assert cli.main(["work", "import", "validate", str(tmp_path / "first.jsonl"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    path.write_text(path.read_text() + "\n")
    second = call(target, capsys)
    assert first["text"] == second["text"]
    catalog = json.loads(path.read_text())
    catalog["servers"]["server-0"]["enabled"] = False
    path.write_text(json.dumps(catalog))
    third = call(target, capsys)
    assert first["findings"] != third["findings"]
    assert first["text"] != third["text"]


def test_digest_is_reproducible_from_public_findings_and_ignores_secret_rotation(tmp_path, capsys):
    import hashlib

    target = seed(tmp_path)
    (target / ".brigade/mcp").mkdir()
    outputs = []
    for secret in ("example-pin-a", "example-pin-b"):
        (target / ".brigade/mcp.json").write_text(
            json.dumps(
                {
                    "servers": {
                        "demo": {
                            "transport": "http",
                            "url": "https://example.invalid/mcp",
                            "headers": {"Authorization": {"literal": secret}},
                        }
                    }
                }
            )
        )
        native(
            target,
            '[mcp_servers.demo]\nurl = "https://example.invalid/mcp"\n'
            f'[mcp_servers.demo.http_headers]\nAuthorization = "{secret}"\n',
        )
        (target / ".brigade/mcp/state.json").write_text(json.dumps({"ownership": {}, "note": secret}))
        result = call(target, capsys)
        projection = {"harness": result["harness"], "findings": result["findings"]}
        digest = hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert f"Evidence digest: {digest}" in result["text"]
        assert secret not in json.dumps(result)
        outputs.append(result)
    assert outputs[0] == outputs[1]
