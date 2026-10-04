"""Offline regressions for the supplied-settings Codex preview contract."""

from __future__ import annotations

import json
import os

import pytest

from brigade.cli import main


def test_preview_cli_counts_cumulative_prefix_without_disclosing_text(tmp_path, capsys):
    repo = tmp_path / "repo"
    cwd = repo / "sub"
    cwd.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "AGENTS.md").write_bytes(b"root")
    (cwd / "AGENTS.md").write_bytes(b"abcdef")
    result = main(
        [
            "context",
            "preview",
            "--cwd",
            str(cwd),
            "--target",
            str(repo),
            "--codex-version",
            "0.160.0",
            "--trust",
            "trusted",
            "--read-access",
            "full",
            "--assume-codex-defaults",
            "--project-doc-max-bytes",
            "7",
            "--json",
        ]
    )
    assert result == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert [row["path"] for row in payload["files"]] == ["AGENTS.md", "sub/AGENTS.md"]
    assert [row["size_bytes"] for row in payload["files"]] == [4, 6]
    assert [row["cumulative_selected_bytes"] for row in payload["files"]] == [4, 10]
    assert [row["consumed_raw_bytes"] for row in payload["files"]] == [4, 3]
    assert payload["totals"]["project"]["consumed_raw_bytes"] == 7
    assert payload["settings"]["project_doc_max_bytes"]["source"] == "caller-supplied"
    assert payload["settings"]["fallback_filenames"]["source"] == "default-assumption"
    assert payload["actual_session_observed"] is False
    assert "abcdef" not in output and str(tmp_path) not in output


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX preview contract")


def inspect(repo, cwd, **kwargs):
    from brigade.context_preview import preview

    return preview(
        target=repo,
        cwd=cwd,
        codex_version="0.160.0",
        trust="trusted",
        read_access="full",
        assume_codex_defaults=True,
        **kwargs,
    )


@pytest.fixture
def tree(tmp_path):
    repo = tmp_path / "repo"
    cwd = repo / "sub"
    cwd.mkdir(parents=True)
    (repo / ".git").mkdir()
    return repo, cwd


# Expectations transcribed from the isolated 0.160.0 oracle, never loaded from
# private artifacts. These inspect byte accounting, never reproduce prompt text.
@pytest.mark.parametrize(
    ("case", "data", "cap", "raw", "rendered", "contribution"),
    [
        ("utf8_split", "éé".encode(), 3, 3, 5, "truncated"),
        ("absent", None, 32768, 0, 0, None),
        ("rust_non_whitespace_controls", b"\x1c\x1d\x1e\x1f", 32768, 4, 4, "loaded"),
        ("invalid_utf8_replacement", b"\xff\xfea", 32768, 3, 7, "loaded"),
        ("utf8_surrogate", b"\xed\xa0\x80", 32768, 3, 9, "loaded"),
        ("utf8_overlong", b"\xe0\x80\x80", 32768, 3, 9, "loaded"),
        ("utf8_above_max", b"\xf4\x90\x80\x80", 32768, 4, 12, "loaded"),
    ],
)
def test_oracle_prefix_bytes(tree, case, data, cap, raw, rendered, contribution):
    repo, cwd = tree
    if data is not None:
        (cwd / "AGENTS.md").write_bytes(data)
    result = inspect(repo, cwd, project_doc_max_bytes=cap)
    assert result["status"] == "complete", case
    assert result["totals"]["project"]["consumed_raw_bytes"] == raw
    assert result["totals"]["project"]["rendered_utf8_bytes"] == rendered
    if contribution:
        assert result["files"][0]["contribution"] == contribution


@pytest.mark.parametrize(
    ("case", "override", "fallbacks", "name", "body", "expected", "raw"),
    [
        ("project_empty_override_blocks_normal", b"", [], "AGENTS.md", b"NORMAL", "AGENTS.override.md", 0),
        ("project_override_priority", b"OVERRIDE", [], "AGENTS.md", b"NORMAL", "AGENTS.override.md", 8),
        ("fallback_selection", None, ["../OUTSIDE.md", "/OUTSIDE.md", "TEAM.md"], "TEAM.md", b"FALLBACK", "TEAM.md", 8),
        ("trim_fallback", None, ["\u0085TEAM.md\u3000"], "TEAM.md", b"TEAM", "TEAM.md", 4),
        ("posix_colon_fallback", None, ["TEAM:note.md"], "TEAM:note.md", b"COLON", "TEAM:note.md", 5),
        ("directory_override_falls_back", "directory", [], "AGENTS.md", b"NORMAL", "AGENTS.md", 6),
    ],
)
def test_oracle_selection(tree, case, override, fallbacks, name, body, expected, raw):
    repo, cwd = tree
    (repo / "OUTSIDE.md").write_bytes(b"EXCLUDED")
    if override == "directory":
        (cwd / "AGENTS.override.md").mkdir()
    elif override is not None:
        (cwd / "AGENTS.override.md").write_bytes(override)
    (cwd / name).write_bytes(body)
    result = inspect(repo, cwd, fallback_filenames=fallbacks)
    assert result["status"] == "complete", case
    assert [row["path"] for row in result["files"]] == ["sub/" + expected]
    assert result["totals"]["project"]["consumed_raw_bytes"] == raw
    assert "/OUTSIDE.md" not in json.dumps(result["settings"])


def test_oracle_whitespace_uncharged(tree):
    repo, cwd = tree
    (repo / "AGENTS.md").write_bytes(b" \t\n")
    (cwd / "AGENTS.md").write_bytes(b"abc")
    result = inspect(repo, cwd, project_doc_max_bytes=3)
    assert [row["consumed_raw_bytes"] for row in result["files"]] == [0, 3]
    assert [row["contribution"] for row in result["files"]] == ["empty", "loaded"]


@pytest.mark.parametrize(
    ("case", "trust", "cap", "raw"),
    [
        ("untrusted_project", "untrusted", 32768, 0),
        ("linked_main_untrusted", "untrusted", 32768, 0),
        ("unset_trust_docs_load", "unset", 32768, 10),
        ("cli_cap_overrides_user", "trusted", 7, 7),
        ("project_config_trusted", "trusted", 7, 7),
        ("project_config_unset_disabled", "unset", 2, 2),
        ("zero_cap_global_unbounded", "trusted", 0, 0),
    ],
)
def test_oracle_supplied_effective_config(tree, tmp_path, case, trust, cap, raw):
    from brigade.context_preview import preview

    repo, cwd = tree
    (repo / "AGENTS.md").write_bytes(b"root")
    (cwd / "AGENTS.md").write_bytes(b"abcdef")
    global_root = tmp_path / "global"
    global_root.mkdir()
    (global_root / "AGENTS.md").write_bytes(b" GLOBAL \n")
    result = preview(
        target=repo,
        cwd=cwd,
        codex_version="0.160.0",
        trust=trust,
        read_access="full",
        assume_codex_defaults=True,
        project_doc_max_bytes=cap,
        codex_home=global_root,
        global_max_bytes=1024,
    )
    assert result["status"] == "complete", case
    assert result["totals"]["project"]["consumed_raw_bytes"] == raw
    assert result["totals"]["global"]["rendered_utf8_bytes"] == 6
    assert result["actual_session_observed"] is False
    assert result["settings"]["trust"]["source"] == "caller-supplied"


def test_oracle_global_empty_override_falls_through(tree, tmp_path):
    repo, cwd = tree
    global_root = tmp_path / "global"
    global_root.mkdir()
    (global_root / "AGENTS.override.md").write_bytes(b" \t\n")
    (global_root / "AGENTS.md").write_bytes(b"GLOBAL")
    result = inspect(repo, cwd, codex_home=global_root, global_max_bytes=1024)
    assert [row["contribution"] for row in result["files"]] == ["empty", "loaded"]
    assert result["totals"]["global"]["rendered_utf8_bytes"] == 6


@pytest.mark.parametrize("case", ["nested_marker", "empty_markers_only_cwd", "no_marker_only_cwd"])
def test_oracle_root_boundary(tree, case):
    repo, cwd = tree
    (repo / "AGENTS.md").write_bytes(b"OUTSIDE")
    (cwd / "AGENTS.md").write_bytes(b"INNER")
    options = {}
    if case == "nested_marker":
        (cwd / ".git").write_bytes(b"metadata not inspected")
    elif case == "empty_markers_only_cwd":
        options["root_markers"] = []
    else:
        (repo / ".git").rmdir()
    result = inspect(repo, cwd, **options)
    if case == "no_marker_only_cwd":
        assert result["status"] == "not_evaluated"
        assert result["matches_codex"] is None
        assert result["files"] == []
    else:
        assert [row["path"] for row in result["files"]] == ["sub/AGENTS.md"]
        assert result["totals"]["project"]["consumed_raw_bytes"] == 5


@pytest.mark.parametrize(
    "case", ["consumer_follows_file_symlink", "lexical_symlink_cwd", "dangling_override_falls_back"]
)
def test_oracle_symlink_divergence(tree, tmp_path, case):
    repo, cwd = tree
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"DO_NOT_DISCLOSE")
    (cwd / "AGENTS.md").write_bytes(b"NORMAL")
    if case == "consumer_follows_file_symlink":
        (cwd / "AGENTS.override.md").symlink_to(outside / "secret")
    elif case == "dangling_override_falls_back":
        (cwd / "AGENTS.override.md").symlink_to(outside / "missing")
    else:
        (repo / "link").symlink_to(cwd, target_is_directory=True)
        cwd = repo / "link"
    result = inspect(repo, cwd)
    assert result["status"] != "complete"
    assert result["matches_codex"] is False
    assert "symlink_not_evaluated" in result["limitations"]
    assert result["totals"]["project"]["consumed_raw_bytes"] is None
    assert "DO_NOT_DISCLOSE" not in json.dumps(result)


def test_oracle_actual_linked_worktree_marker_is_metadata_only(tree, tmp_path):
    repo, cwd = tree
    (repo / ".git").rmdir()
    (repo / ".git").write_text("gitdir: ../common\n")
    common = tmp_path / "common"
    common.mkdir()
    (common / "AGENTS.md").write_text("PRIVATE_COMMON")
    (repo / "AGENTS.md").write_bytes(b"LINKED")
    (cwd / "AGENTS.md").write_bytes(b"CHILD")
    result = inspect(repo, cwd)
    assert [row["size_bytes"] for row in result["files"]] == [6, 5]
    assert result["totals"]["project"]["consumed_raw_bytes"] == 11
    assert "PRIVATE_COMMON" not in json.dumps(result)


@pytest.mark.parametrize(
    "options",
    [
        {"codex_version": "unknown"},
        {"trust": "unknown"},
        {"read_access": "unknown"},
        {"assume_codex_defaults": False},
        {"project_doc_max_bytes": 1048577},
        {"root_markers": ["../private"]},
        {"global_max_bytes": 1048577},
    ],
)
def test_unknown_inputs_refuse_before_content_reads(tree, monkeypatch, options):
    from brigade.context_preview import preview

    repo, cwd = tree
    (repo / "AGENTS.md").write_bytes(b"SECRET")

    def forbidden_read(*args):
        pytest.fail("unknown inputs must not read any document")

    monkeypatch.setattr(os, "read", forbidden_read)
    settings = dict(
        target=repo, cwd=cwd, codex_version="0.160.0", trust="trusted", read_access="full", assume_codex_defaults=True
    )
    settings.update(options)
    result = preview(**settings)
    assert result["status"] == "not_evaluated"
    assert result["files"] == []
    assert str(repo) not in json.dumps(result)


def test_global_oversize_never_reads_or_falls_through(tree, tmp_path, monkeypatch):
    repo, cwd = tree
    home = tmp_path / "global"
    home.mkdir()
    (home / "AGENTS.override.md").write_bytes(b"large")
    (home / "AGENTS.md").write_bytes(b"ok")
    monkeypatch.setattr(os, "read", lambda *args: pytest.fail("oversize global read"))
    result = inspect(repo, cwd, codex_home=home, global_max_bytes=3)
    assert result["matches_codex"] is False
    assert result["status"] == "not_evaluated"
    assert len(result["files"]) == 1
    assert result["files"][0]["contribution"] == "unknown"


def test_cap_exhausted_is_metadata_only_and_separate_advisory(tree):
    repo, cwd = tree
    (repo / "AGENTS.md").write_bytes(b"abc")
    (cwd / "AGENTS.md").write_bytes(b"later")
    result = inspect(repo, cwd, project_doc_max_bytes=3)
    assert [row["consumed_raw_bytes"] for row in result["files"]] == [3, 0]
    assert result["files"][1]["contribution"] == "cap_exhausted"
    assert result["files"][1]["brigade_advisory_budget_bytes"] == 12000


@pytest.mark.parametrize("kind", ["fifo", "device"])
def test_nonregular_candidates_are_never_read(tree, kind):
    repo, cwd = tree
    if kind == "fifo":
        os.mkfifo(cwd / "AGENTS.override.md")
    else:
        # Device symlink must be rejected, never opened or followed.
        (cwd / "AGENTS.override.md").symlink_to("/dev/null")
    (cwd / "AGENTS.md").write_bytes(b"NORMAL")
    result = inspect(repo, cwd)
    if kind == "fifo":
        assert result["totals"]["project"]["consumed_raw_bytes"] == 6
    else:
        assert result["matches_codex"] is False


@pytest.mark.parametrize("mutation", ["rewrite", "replace", "symlink"])
def test_mutation_during_read_invalidates_accounting(tree, monkeypatch, mutation):
    repo, cwd = tree
    doc = cwd / "AGENTS.md"
    doc.write_bytes(b"first")
    original = os.read
    changed = False

    def racing_read(fd, count):
        nonlocal changed
        value = original(fd, count)
        if not changed:
            changed = True
            if mutation == "rewrite":
                doc.write_bytes(b"other")  # same size, still must be unknown
            else:
                doc.unlink()
                if mutation == "replace":
                    doc.write_bytes(b"other")
                else:
                    doc.symlink_to("missing")
        return value

    monkeypatch.setattr(os, "read", racing_read)
    result = inspect(repo, cwd)
    assert result["status"] != "complete"
    assert result["totals"]["project"]["consumed_raw_bytes"] is None
    assert result["files"][0]["consumed_raw_bytes"] is None


def test_double_root_and_lexical_dot_normalization(tree):
    repo, cwd = tree
    (cwd / "AGENTS.md").write_bytes(b"abc")
    result = inspect("/" + str(repo), str(cwd) + "/../sub/./")
    assert result["status"] == "complete"
    assert result["totals"]["project"]["consumed_raw_bytes"] == 3


def test_text_escapes_filename_controls_and_cli_invalid_settings(tree, capsys):
    repo, cwd = tree
    (cwd / "TEAM\n:note\\.md").write_bytes(b"SECRET_BODY")
    args = [
        "context",
        "preview",
        "--cwd",
        str(cwd),
        "--target",
        str(repo),
        "--codex-version",
        "0.160.0",
        "--trust",
        "trusted",
        "--read-access",
        "restricted",
        "--assume-codex-defaults",
        "--fallback-filenames",
        '["TEAM\\n:note\\\\.md"]',
    ]
    assert main(args) == 0
    output = capsys.readouterr().out
    assert "TEAM\\n:note" in output
    assert "SECRET_BODY" not in output and str(repo) not in output
    assert main(args + ["--root-markers", str(repo), "--json"]) == 2
    output = capsys.readouterr()
    assert str(repo) not in output.out + output.err


def test_no_intentional_writes_or_consumer_process(tree, monkeypatch):
    import subprocess

    repo, cwd = tree
    (cwd / "AGENTS.md").write_bytes(b"abc")
    original_open = os.open

    def readonly_open(path, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", readonly_open)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("consumer subprocess"))
    assert inspect(repo, cwd)["status"] == "complete"


@pytest.mark.parametrize("flag", ["--fallback-filenames", "--root-markers"])
def test_cli_unencodable_filename_has_opaque_refusal(tree, capsys, flag):
    repo, cwd = tree
    (cwd / "AGENTS.md").write_bytes(b"PRIVATE_BODY")
    args = [
        "context",
        "preview",
        "--target",
        str(repo),
        "--cwd",
        str(cwd),
        "--codex-version",
        "0.160.0",
        "--trust",
        "trusted",
        "--read-access",
        "full",
        "--assume-codex-defaults",
        flag,
        '["\\ud800"]',
        "--json",
    ]
    assert main(args) == 2
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] == "not_evaluated"
    assert result["files"] == []
    assert "Traceback" not in output.out + output.err
    assert str(repo) not in output.out + output.err
    assert "PRIVATE_BODY" not in output.out


@pytest.mark.parametrize(
    "flag",
    [
        "--trust",
        "--read-access",
        "--codex-version",
        "--project-doc-max-bytes",
        "--root-markers",
        "--fallback-filenames",
        "--global-max-bytes",
    ],
)
def test_cli_invalid_values_never_echo_private_input(tree, capsys, flag):
    repo, cwd = tree
    args = [
        "context",
        "preview",
        "--target",
        str(repo),
        "--cwd",
        str(cwd),
        "--codex-version",
        "0.160.0",
        "--trust",
        "trusted",
        "--read-access",
        "full",
        "--assume-codex-defaults",
        flag,
        str(repo / "private"),
        "--json",
    ]
    assert main(args) == 2
    output = capsys.readouterr()
    assert str(repo) not in output.out + output.err
    assert "Traceback" not in output.out + output.err


def test_valid_posix_surrogateescaped_filename_is_supported(tree):
    repo, cwd = tree
    name = os.fsdecode(b"TEAM-\xff.md")
    (cwd / name).write_bytes(b"abc")
    result = inspect(repo, cwd, fallback_filenames=[name])
    assert result["status"] == "complete"
    assert result["files"][0]["path"] == "sub/" + name
    assert result["totals"]["project"]["consumed_raw_bytes"] == 3


def test_discovery_error_after_would_be_cap_exhaustion_prevents_project_reads(tree, monkeypatch):
    repo, cwd = tree
    (repo / "AGENTS.md").write_bytes(b"abc")
    (cwd / "AGENTS.override.md").symlink_to("missing")
    monkeypatch.setattr(os, "read", lambda *args: pytest.fail("discovery must finish before project reads"))
    result = inspect(repo, cwd, project_doc_max_bytes=3)
    assert result["matches_codex"] is False
    assert result["totals"]["project"]["consumed_raw_bytes"] is None
