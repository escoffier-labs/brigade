"""Public instruction diagnosis contracts using isolated project fixtures."""

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from brigade import context_preview

from brigade import cli


_requires_posix_preview = pytest.mark.skipif(
    not context_preview._POSIX_NOFOLLOW_SUPPORTED,
    reason="Instruction preview requires POSIX no-follow descriptors.",
)


def invoke(target, capsys, *extra, expected=0):
    code = cli.main(
        [
            "bug",
            "diagnose",
            "--component",
            "instructions",
            "--target",
            str(target),
            "--cwd",
            str(target),
            "--harness",
            "codex",
            "--codex-version",
            "0.160.0",
            "--trust",
            "trusted",
            "--read-access",
            "full",
            "--assume-codex-defaults",
            "--summary",
            "Expected project instructions",
            "--json",
            *extra,
        ]
    )
    assert code == expected
    return json.loads(capsys.readouterr().out)


@_requires_posix_preview
def test_instruction_cli_models_empty_chain_without_session_claim(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    result = invoke(tmp_path, capsys)
    assert result["actual_session_observed"] is False
    assert result["runtime"] == "unknown"
    assert result["verification"] == "not-run"
    assert result["findings"][0]["status"] == "no_selected_files"


@_requires_posix_preview
def test_order_override_cwd_and_expected_label_do_not_claim_missing(tmp_path, capsys, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("ancestor")
    child = tmp_path / "child"
    child.mkdir()
    (child / "AGENTS.md").write_text("ignored")
    (child / "AGENTS.override.md").write_text("nearer")
    result = invoke(tmp_path, capsys, "--cwd", str(child), "--instruction-path", "child/AGENTS.md")
    assert [f["path"] for f in result["findings"] if "path" in f] == [
        "AGENTS.md",
        "child/AGENTS.override.md",
        "child/AGENTS.md",
    ]
    assert [f["status"] for f in result["findings"]] == ["loaded", "loaded", "not_selected"]
    # The expected label is not an additional filesystem query.
    original = os.stat

    def guarded(path, *args, **kwargs):
        if str(path) == "uninspected.md":
            pytest.fail("expected label was statted")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", guarded)
    result = invoke(tmp_path, capsys, "--instruction-path", "uninspected.md")
    assert result["findings"][-1]["status"] == "not_selected"
    assert "missing" not in result["findings"][-1]["observed"].lower()


@pytest.mark.parametrize(
    "cap,trust,want",
    [
        (3, "trusted", ["truncated", "cap_exhausted"]),
        (0, "trusted", ["disabled"]),
        (30, "untrusted", ["disabled"]),
        (30, "trusted", ["loaded", "empty"]),
    ],
)
@_requires_posix_preview
def test_contributions_and_loading_exclusions(tmp_path, capsys, cap, trust, want):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("ancestor")
    child = tmp_path / "child"
    child.mkdir()
    (child / "AGENTS.md").write_text(" \n")
    result = invoke(tmp_path, capsys, "--cwd", str(child), "--project-doc-max-bytes", str(cap), "--trust", trust)
    assert [f["status"] for f in result["findings"]] == want


@pytest.mark.parametrize(
    "extra",
    [
        ["--harness", "codex-user"],
        ["--name", "demo"],
        ["--target", "relative"],
        ["--cwd", "relative"],
        ["--instruction-path", "../private"],
        ["--instruction-path", "/private"],
        ["--instruction-path", ""],
        ["--instruction-path", "bad\x1b"],
        ["--fallback-filenames", "[" + " " * 20000 + "]"],
        ["--fallback-filenames", '["../private"]'],
        ["--root-markers", '["bad\\u001b"]'],
    ],
)
def test_unsafe_inputs_refused_before_producer(tmp_path, capsys, monkeypatch, extra):
    def forbidden(**kwargs):
        pytest.fail("invalid inputs reached producer")

    monkeypatch.setattr(context_preview, "preview", forbidden)
    result = invoke(tmp_path, capsys, *extra, expected=2)
    assert result["actual_session_observed"] is False


@pytest.mark.parametrize(
    "extra",
    [
        ["--codex-version", "0.159.0"],
        ["--trust", "unknown"],
        ["--read-access", "unknown"],
        ["--project-doc-max-bytes", "1048577"],
        ["--root-markers", '["absent-marker"]'],
    ],
)
def test_unsupported_or_incomplete_is_unevaluated_without_body_reads(tmp_path, capsys, monkeypatch, extra):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("private body")

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported model read content")

    monkeypatch.setattr(context_preview, "_read", forbidden)
    result = invoke(tmp_path, capsys, *extra, expected=2)
    assert result["findings"][0]["status"] == "not_evaluated"
    assert result["runtime"] == "unknown"


def test_mcp_refuses_instruction_flags_before_reads(tmp_path, capsys, monkeypatch):
    from brigade import bug_diagnose

    def forbidden(*args, **kwargs):
        pytest.fail("mixed component reached MCP reads")

    monkeypatch.setattr(bug_diagnose, "_inputs", forbidden)
    assert (
        cli.main(["bug", "diagnose", "--harness", "codex", "--summary", "example", "--cwd", str(tmp_path), "--json"])
        == 2
    )
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"


@_requires_posix_preview
def test_body_free_redaction_and_public_digest(tmp_path, capsys, monkeypatch):
    (tmp_path / ".git").mkdir()
    token = "ghp_" + "FixtureOnlySecret" * 3
    path = tmp_path / "AGENTS.md"
    path.write_text("first private instruction")
    first = invoke(tmp_path, capsys)
    path.write_text("other private instruction")  # same public byte size
    second = invoke(tmp_path, capsys)
    assert first["evidence_digest"] == second["evidence_digest"]
    path.write_text(token + "\x1b[31m")
    destination = tmp_path / "finding.jsonl"
    result = invoke(
        tmp_path, capsys, "--summary", token + "\x1b[31m " + str(tmp_path), "--save-finding", str(destination)
    )
    rendered = json.dumps(result) + destination.read_text()
    assert token not in rendered
    assert str(tmp_path) not in rendered
    assert "\\u001b" not in rendered
    assert "private instruction" not in rendered


@_requires_posix_preview
def test_preview_and_refusal_skip_hooks_and_all_runtime_transports(tmp_path, capsys, monkeypatch):
    from brigade import bug_last_error, update_notify

    (tmp_path / ".git").mkdir()
    before = sorted(tmp_path.rglob("*"))

    def forbidden(*args, **kwargs):
        pytest.fail("ambient hook or transport")

    monkeypatch.setattr(bug_last_error, "record", forbidden)
    monkeypatch.setattr(update_notify, "maybe_notify", forbidden)
    monkeypatch.setattr(subprocess.Popen, "__init__", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    invoke(tmp_path, capsys)
    invoke(tmp_path, capsys, "--harness", "unsupported", expected=2)
    assert sorted(tmp_path.rglob("*")) == before


@_requires_posix_preview
def test_export_import_pending_and_exclusive(tmp_path, capsys):
    from tests.work_cmd_test_helpers import _init_git_repo

    _init_git_repo(tmp_path)
    destination = tmp_path / "finding.jsonl"
    invoke(tmp_path, capsys, "--save-finding", str(destination))
    assert cli.main(["work", "import", "validate", str(destination), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    assert cli.main(["work", "import", "ingest", str(destination), "--target", str(tmp_path), "--json"]) == 0
    item = json.loads(capsys.readouterr().out)["imports"][0]
    assert item["status"] == "pending"
    assert item["metadata"]["declared_source"] == "assisted-diagnosis"
    before = destination.read_bytes()
    invoke(tmp_path, capsys, "--save-finding", str(destination), expected=2)
    assert destination.read_bytes() == before


@_requires_posix_preview
def test_local_report_has_instruction_title_no_collection_or_submission(tmp_path, capsys, monkeypatch):
    from brigade import bug_cmd, bug_collect, bug_last_error, extras

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    parser = cli._build_parser()
    monkeypatch.setattr(cli, "_build_parser", lambda: parser)

    def forbidden(*args, **kwargs):
        pytest.fail("ambient report collection or submission")

    for module, names in [
        (bug_collect, ["_install_channel", "recent_failures"]),
        (bug_last_error, ["read"]),
        (extras, ["enabled"]),
        (bug_cmd, ["collect_diagnostics", "collect", "gh", "urlopen"]),
    ]:
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    result = invoke(tmp_path, capsys, "--prepare-report")
    assert result["submitted"] is False
    bundle = json.loads(Path(result["preview_path"]).with_name("bundle.json").read_text())
    assert bundle["user"]["title"] == "Codex project instruction diagnosis"
    assert bundle["last_error"] is None
    assert bundle["recent_failures"] == {"runs": [], "verify": []}
    assert "actual_session_observed: false" in bundle["user"]["summary"]
    receipt = json.loads(Path(result["receipt_path"]).read_text())
    assert receipt["consent"]["mode"] == "none"
    assert receipt["destination"] == "file"


@_requires_posix_preview
def test_security_report_stays_blocked(tmp_path, capsys, monkeypatch):
    (tmp_path / ".git").mkdir()
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    result = invoke(tmp_path, capsys, "--summary", "Suspected security vulnerability", "--prepare-report", expected=2)
    assert "SECURITY.md" in result["text"]
    assert not data.exists()


@_requires_posix_preview
def test_restricted_access_preserves_producer_supported_semantics(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("project instructions")
    result = invoke(tmp_path, capsys, "--read-access", "restricted")
    assert result["findings"][0]["status"] == "loaded"
    assert result["settings"]["read_access"]["value"] == "restricted"


@_requires_posix_preview
def test_symlink_inspection_stays_unevaluated(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").symlink_to(tmp_path / "private")
    result = invoke(tmp_path, capsys, expected=2)
    assert result["findings"][0]["status"] == "not_evaluated"
    assert "no_selected_files" not in result["text"]


@_requires_posix_preview
def test_explicit_settings_and_default_provenance(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    result = invoke(tmp_path, capsys)
    assert result["settings"]["project_doc_max_bytes"] == {"value": 32768, "source": "default-assumption"}
    (tmp_path / "custom.md").write_text("custom")
    result = invoke(
        tmp_path,
        capsys,
        "--project-doc-max-bytes",
        "12",
        "--fallback-filenames",
        '["custom.md"]',
        "--root-markers",
        "[]",
    )
    assert result["findings"][0]["path"] == "custom.md"
    assert all(
        result["settings"][key]["source"] == "caller-supplied"
        for key in ("project_doc_max_bytes", "fallback_filenames", "root_markers")
    )


@pytest.mark.parametrize("omit", ["--target", "--cwd", "--codex-version", "--trust", "--read-access"])
def test_required_project_inputs_do_not_infer_ambient_state(tmp_path, capsys, monkeypatch, omit):
    def forbidden(**kwargs):
        pytest.fail("missing explicit input reached producer")

    monkeypatch.setattr(context_preview, "preview", forbidden)
    options = {
        "--target": str(tmp_path),
        "--cwd": str(tmp_path),
        "--codex-version": "0.160.0",
        "--trust": "trusted",
        "--read-access": "full",
    }
    argv = ["bug", "diagnose", "--component", "instructions", "--harness", "codex", "--summary", "example", "--json"]
    for key, value in options.items():
        if key != omit:
            argv.extend([key, value])
    assert cli.main(argv) == 2
    assert json.loads(capsys.readouterr().out)["actual_session_observed"] is False


def test_unknown_project_settings_remain_unevaluated(tmp_path, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unknown effective settings triggered body read")

    monkeypatch.setattr(context_preview, "_read", forbidden)
    assert (
        cli.main(
            [
                "bug",
                "diagnose",
                "--component",
                "instructions",
                "--target",
                str(tmp_path),
                "--cwd",
                str(tmp_path),
                "--harness",
                "codex",
                "--codex-version",
                "0.160.0",
                "--trust",
                "trusted",
                "--read-access",
                "full",
                "--summary",
                "example",
                "--json",
            ]
        )
        == 2
    )
    result = json.loads(capsys.readouterr().out)
    assert result["findings"][0]["status"] == "not_evaluated"
    assert result["settings"]["root_markers"] == {"value": None, "source": "unknown"}


@pytest.mark.parametrize("extra", [["--harness", "unsupported"], ["--save-finding", "existing.jsonl"]])
def test_plain_refusal_keeps_explicit_session_runtime_and_verification_unknown(tmp_path, capsys, monkeypatch, extra):
    (tmp_path / ".git").mkdir()
    (tmp_path / "existing.jsonl").write_text("preserve")
    monkeypatch.chdir(tmp_path)
    assert (
        cli.main(
            [
                "bug",
                "diagnose",
                "--component",
                "instructions",
                "--target",
                str(tmp_path),
                "--cwd",
                str(tmp_path),
                "--harness",
                "codex",
                "--codex-version",
                "0.160.0",
                "--trust",
                "trusted",
                "--read-access",
                "full",
                "--assume-codex-defaults",
                "--summary",
                "example",
                *extra,
            ]
        )
        == 2
    )
    text = capsys.readouterr().out
    assert "actual_session_observed: false" in text
    assert "runtime: unknown" in text
    assert "verification: not-run" in text


def test_unsupported_platform_refuses_before_reads_and_output(tmp_path, capsys, monkeypatch):
    from tests._home import set_home

    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / "AGENTS.md").write_text("private fixture instructions")
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setenv("BRIGADE_HOME", str(home / ".brigade"))
    monkeypatch.setenv("BRIGADE_USER_DIR", str(home / ".brigade"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    destination = tmp_path / "finding.jsonl"

    def snapshot():
        return {str(p.relative_to(tmp_path)): p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")}

    before = snapshot()

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported platform reached instruction inspection")

    monkeypatch.setattr(context_preview, "_POSIX_NOFOLLOW_SUPPORTED", False)
    monkeypatch.setattr(context_preview, "_read", forbidden)
    monkeypatch.setattr(context_preview, "_project_directories", forbidden)
    result = invoke(project, capsys, "--save-finding", str(destination), expected=2)
    assert result["status"] == "unavailable"
    assert result["findings"][0]["status"] == "not_evaluated"
    assert result["actual_session_observed"] is False
    assert result["runtime"] == "unknown"
    assert result["verification"] == "not-run"
    assert not destination.exists()
    assert snapshot() == before
