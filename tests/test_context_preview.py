"""Offline regressions for the supplied-settings Codex preview contract."""

from __future__ import annotations

import json
import os

import pytest

from brigade.cli import main
from brigade.context_preview import preview
from tests._posix import requires_dirfd


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
        ("utf8_overlong", b"\xe0\x80\xaf", 32768, 3, 9, "loaded"),
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
        ("supplied_untrusted_linked_outcome", "untrusted", 32768, 0),
        ("unset_trust_docs_load", "unset", 32768, 10),
        ("supplied_cli_cap", "trusted", 7, 7),
        ("supplied_trusted_project_cap", "trusted", 7, 7),
        ("supplied_unset_user_cap", "unset", 2, 2),
        ("zero_cap_global_unbounded", "trusted", 0, 0),
    ],
)
def test_supplied_effective_config_outcomes(tree, tmp_path, case, trust, cap, raw):
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
    initial = doc.stat()
    original = os.read
    changed = False

    def racing_read(fd, count):
        nonlocal changed
        value = original(fd, count)
        if not changed:
            changed = True
            if mutation == "rewrite":
                doc.write_bytes(b"other")  # same size, still must be unknown
                os.utime(doc, ns=(initial.st_atime_ns, initial.st_mtime_ns + 2_000_000_000))
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


@requires_dirfd
def test_no_intentional_writes_or_consumer_process(tree, monkeypatch):
    import subprocess

    repo, cwd = tree
    (cwd / "AGENTS.md").write_bytes(b"abc")
    original_open = os.open
    opened = []

    def readonly_open(path, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        assert flags & os.O_NOFOLLOW
        assert flags & os.O_NONBLOCK
        assert kwargs["dir_fd"] is not None or path == "/"
        fd = original_open(path, flags, *args, **kwargs)
        if path != "AGENTS.md":
            assert flags & os.O_DIRECTORY
        opened.append(path)
        return fd

    monkeypatch.setattr(os, "open", readonly_open)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("consumer subprocess"))
    assert inspect(repo, cwd)["status"] == "complete"
    assert "/" in opened and "sub" in opened and "AGENTS.md" in opened


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (OSError, "directory_not_evaluated"),
        (TypeError, "posix_nofollow_required"),
        (NotImplementedError, "posix_nofollow_required"),
    ],
)
def test_directory_flag_factory_failure_is_opaque_before_open(tree, monkeypatch, error, reason):
    from brigade import dirfd

    repo, cwd = tree
    (cwd / "AGENTS.md").write_bytes(b"PRIVATE_BODY")

    def unavailable(*, nofollow):
        assert nofollow is True
        raise error(str(repo / "private"))

    monkeypatch.setattr(dirfd, "directory_flags", unavailable)
    monkeypatch.setattr(os, "open", lambda *args, **kwargs: pytest.fail("factory failure must prevent open"))
    monkeypatch.setattr(os, "read", lambda *args: pytest.fail("factory failure must prevent content reads"))
    result = inspect(repo, cwd)
    assert result["status"] == "not_evaluated"
    assert result["matches_codex"] is None
    assert reason in result["limitations"]
    assert result["files"] == []
    assert result["totals"]["project"]["consumed_raw_bytes"] is None
    output = json.dumps(result)
    assert str(repo) not in output and "private" not in output and "PRIVATE_BODY" not in output


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


@pytest.mark.parametrize("global_state", ["not_requested", "absent", "empty", "loaded"])
@pytest.mark.parametrize("read_access", ["full", "restricted"])
def test_failed_project_status_uses_only_completed_requested_scope(
    tree, tmp_path, monkeypatch, global_state, read_access
):
    from brigade.context_preview import preview

    repo, cwd = tree
    (repo / "AGENTS.md").write_bytes(b"root")
    child = cwd / "AGENTS.md"
    child.write_bytes(b"child")
    options = {}
    if global_state != "not_requested":
        home = tmp_path / "global"
        home.mkdir()
        if global_state != "absent":
            (home / "AGENTS.md").write_bytes(b"global" if global_state == "loaded" else b"")
        options.update(codex_home=home, global_max_bytes=100)
    original_read = os.read
    changed = False

    def racing_read(fd, count):
        nonlocal changed
        data = original_read(fd, count)
        if data == b"root" and not changed:
            changed = True
            child.unlink()
        return data

    monkeypatch.setattr(os, "read", racing_read)
    result = preview(
        target=repo,
        cwd=cwd,
        codex_version="0.160.0",
        trust="trusted",
        read_access=read_access,
        assume_codex_defaults=True,
        **options,
    )
    assert result["status"] == ("not_evaluated" if global_state == "not_requested" else "partial")
    assert result["totals"]["project"]["consumed_raw_bytes"] is None
    assert all(row["consumed_raw_bytes"] is None for row in result["files"] if row["scope"] == "project")
    if global_state != "not_requested":
        assert result["totals"]["global"]["consumed_raw_bytes"] == (6 if global_state == "loaded" else 0)


@pytest.mark.parametrize("shortcut", ["untrusted", "zero_cap"])
@pytest.mark.parametrize("invalid", ["missing_scope", "missing_cwd", "symlink_scope", "symlink_cwd"])
def test_shortcuts_validate_scope_and_cwd_without_content_reads(tree, monkeypatch, shortcut, invalid):
    from brigade.context_preview import preview

    repo, cwd = tree
    if invalid == "missing_scope":
        repo = repo / "missing"
        cwd = repo / "child"
    elif invalid == "missing_cwd":
        cwd = cwd / "missing"
    elif invalid == "symlink_scope":
        link = repo.parent / "link"
        link.symlink_to(repo, target_is_directory=True)
        repo, cwd = link, link / "sub"
    else:
        link = repo / "link"
        link.symlink_to(cwd, target_is_directory=True)
        cwd = link
    monkeypatch.setattr(os, "read", lambda *args: pytest.fail("shortcut read document content"))
    result = preview(
        target=repo,
        cwd=cwd,
        codex_version="0.160.0",
        trust="untrusted" if shortcut == "untrusted" else "trusted",
        read_access="full",
        assume_codex_defaults=True,
        project_doc_max_bytes=0 if shortcut == "zero_cap" else 32768,
    )
    assert result["status"] == "not_evaluated"
    assert result["totals"]["project"]["consumed_raw_bytes"] is None
    assert result["files"] == []
    assert ("symlink_not_evaluated" if invalid.startswith("symlink") else "directory_not_evaluated") in result[
        "limitations"
    ]
    assert str(repo) not in json.dumps(result)


@pytest.mark.parametrize(
    "missing", ["open_dir_fd", "stat_dir_fd", "stat_nofollow", "O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"]
)
def test_native_capability_is_checked_at_import(tree, monkeypatch, missing):
    import importlib.util
    from brigade import context_preview

    repo, cwd = tree
    if missing.endswith("dir_fd"):
        primitive = os.open if missing.startswith("open") else os.stat
        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd - {primitive})
    elif missing == "stat_nofollow":
        monkeypatch.setattr(os, "supports_follow_symlinks", os.supports_follow_symlinks - {os.stat})
    else:
        monkeypatch.delattr(os, missing)
    spec = importlib.util.spec_from_file_location("brigade._preview_capability_probe", context_preview.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(os, "read", lambda *args: pytest.fail("unsupported native capabilities read content"))
    result = module.preview(
        target=repo, cwd=cwd, codex_version="0.160.0", trust="trusted", read_access="full", assume_codex_defaults=True
    )
    assert result["status"] == "not_evaluated"
    assert "posix_nofollow_required" in result["limitations"]


@pytest.mark.parametrize("primitive", ["open", "stat", "fstat", "read"])
@pytest.mark.parametrize("error", [TypeError, NotImplementedError])
def test_runtime_unsupported_filesystem_operations_are_opaque(tree, monkeypatch, primitive, error):
    repo, cwd = tree
    (cwd / "AGENTS.md").write_bytes(b"SECRET")

    def unsupported(*args, **kwargs):
        raise error(str(repo / "private"))

    monkeypatch.setattr(os, primitive, unsupported)
    result = inspect(repo, cwd)
    assert result["status"] == "not_evaluated"
    assert "posix_nofollow_required" in result["limitations"]
    assert str(repo) not in json.dumps(result)


@pytest.mark.parametrize("mutation", ["grow", "replace", "remove", "override", "absent_addition", "symlink"])
@pytest.mark.parametrize("cap", [4, 100])
def test_final_project_selection_revalidation_is_metadata_only(tree, monkeypatch, mutation, cap):
    repo, cwd = tree
    (repo / "AGENTS.md").write_bytes(b"root")
    child = cwd / "AGENTS.md"
    if mutation != "absent_addition":
        child.write_bytes(b"child")
    original_read = os.read
    reads = []
    changed = False

    def racing_read(fd, count):
        nonlocal changed
        data = original_read(fd, count)
        reads.append(data)
        if data == b"root" and not changed:
            changed = True
            if mutation == "grow":
                child.write_bytes(b"larger child")
            elif mutation == "replace":
                replacement = cwd / "replacement.md"
                replacement.write_bytes(b"other")
                replacement.replace(child)
            elif mutation in {"remove", "symlink"}:
                child.unlink()
                if mutation == "symlink":
                    child.symlink_to("missing")
            elif mutation == "override":
                (cwd / "AGENTS.override.md").write_bytes(b"override")
            else:
                child.write_bytes(b"new")
        return data

    monkeypatch.setattr(os, "read", racing_read)
    result = inspect(repo, cwd, project_doc_max_bytes=cap)
    assert changed
    assert result["status"] == "not_evaluated"
    assert result["totals"]["project"]["selected_bytes"] is None
    assert result["totals"]["project"]["consumed_raw_bytes"] is None
    assert all(row["contribution"] == "unknown" and row["cumulative_selected_bytes"] is None for row in result["files"])
    if cap == 4:
        assert reads == [b"root", b""]  # no child content read, including final validation


@pytest.mark.parametrize("initial", ["absent", "empty"])
def test_examined_global_override_revalidated_after_normal_read(tree, tmp_path, monkeypatch, initial):
    repo, cwd = tree
    home = tmp_path / "global"
    home.mkdir()
    override = home / "AGENTS.override.md"
    if initial == "empty":
        override.write_bytes(b"")
    (home / "AGENTS.md").write_bytes(b"global")
    original_read = os.read

    def racing_read(fd, count):
        data = original_read(fd, count)
        if data == b"global":
            override.write_bytes(b"new override")
        return data

    monkeypatch.setattr(os, "read", racing_read)
    result = inspect(repo, cwd, codex_home=home, global_max_bytes=100)
    assert result["status"] != "complete"
    assert result["totals"]["global"]["consumed_raw_bytes"] is None
    assert all(row["contribution"] == "unknown" for row in result["files"] if row["scope"] == "global")


def test_global_selection_revalidated_after_project_read(tree, tmp_path, monkeypatch):
    repo, cwd = tree
    (cwd / "AGENTS.md").write_bytes(b"project")
    home = tmp_path / "global"
    home.mkdir()
    (home / "AGENTS.md").write_bytes(b"global")
    original_read = os.read

    def racing_read(fd, count):
        data = original_read(fd, count)
        if data == b"project":
            (home / "AGENTS.override.md").write_bytes(b"new override")
        return data

    monkeypatch.setattr(os, "read", racing_read)
    result = inspect(repo, cwd, codex_home=home, global_max_bytes=100)
    assert result["status"] == "partial"
    assert result["totals"]["global"]["consumed_raw_bytes"] is None
    assert result["totals"]["project"]["consumed_raw_bytes"] == 7


def test_discarded_invalid_fallbacks_are_labeled_without_values(tree):
    repo, cwd = tree
    (cwd / "TEAM.md").write_bytes(b"team")
    result = inspect(repo, cwd, fallback_filenames=[str(repo / "private"), "../private", "", "TEAM.md"])
    assert result["status"] == "complete"
    assert "invalid_fallback_entries_ignored" in result["limitations"]
    assert result["settings"]["fallback_filenames"]["value"] == ["TEAM.md"]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("global_state", ["not_requested", "absent", "empty", "loaded"])
@pytest.mark.parametrize("read_access", ["full", "restricted"])
def test_project_permission_error_retains_only_completed_global(tree, tmp_path, global_state, read_access):
    from brigade.context_preview import preview

    repo, cwd = tree
    (repo / "AGENTS.md").write_bytes(b"root")
    child = cwd / "AGENTS.md"
    child.write_bytes(b"child")
    home = tmp_path / "global"
    home.mkdir()
    if global_state in {"empty", "loaded"}:
        (home / "AGENTS.md").write_bytes(b"global" if global_state == "loaded" else b"")
    options = {} if global_state == "not_requested" else dict(codex_home=home, global_max_bytes=100)
    child.chmod(0)
    try:
        result = preview(
            target=repo,
            cwd=cwd,
            codex_version="0.160.0",
            trust="trusted",
            read_access=read_access,
            assume_codex_defaults=True,
            **options,
        )
    finally:
        child.chmod(0o600)
    assert result["status"] == ("not_evaluated" if global_state == "not_requested" else "partial")
    assert "file_read_not_evaluated" in result["limitations"]
    assert result["totals"]["project"]["consumed_raw_bytes"] is None
    assert all(row["contribution"] == "unknown" for row in result["files"] if row["scope"] == "project")


@pytest.mark.parametrize("global_state", ["absent", "empty"])
def test_completed_zero_byte_global_is_partial_after_project_discovery_error(tree, tmp_path, global_state):
    repo, cwd = tree
    (cwd / "AGENTS.override.md").symlink_to("missing")
    home = tmp_path / "global"
    home.mkdir()
    if global_state == "empty":
        (home / "AGENTS.md").write_bytes(b"")
    result = inspect(repo, cwd, codex_home=home, global_max_bytes=100)
    assert result["status"] == "partial"
    assert result["totals"]["global"]["consumed_raw_bytes"] == 0
    assert result["totals"]["project"]["consumed_raw_bytes"] is None


@pytest.mark.parametrize("read_access", ["full", "restricted"])
def test_partial_scope_is_revalidated_when_project_mutates_global_then_fails(tree, tmp_path, monkeypatch, read_access):
    from brigade.context_preview import preview

    repo, cwd = tree
    (cwd / "AGENTS.md").write_bytes(b"project")
    home = tmp_path / "global"
    home.mkdir()
    (home / "AGENTS.md").write_bytes(b"global")
    original_read = os.read
    changed = False

    def racing_read(fd, count):
        nonlocal changed
        data = original_read(fd, count)
        if data == b"project":
            changed = True
            (home / "AGENTS.override.md").write_bytes(b"new override")
            raise PermissionError(str(repo / "private"))
        return data

    monkeypatch.setattr(os, "read", racing_read)
    result = preview(
        target=repo,
        cwd=cwd,
        codex_version="0.160.0",
        trust="trusted",
        read_access=read_access,
        assume_codex_defaults=True,
        codex_home=home,
        global_max_bytes=100,
    )
    assert changed
    assert result["status"] == "not_evaluated"
    assert "file_read_not_evaluated" in result["limitations"]
    assert "selection_changed" in result["limitations"]
    assert all(total["consumed_raw_bytes"] is None for total in result["totals"].values())
    assert all(row["contribution"] == "unknown" for row in result["files"])
    assert str(repo) not in json.dumps(result)
