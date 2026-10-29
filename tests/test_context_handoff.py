"""Producer contract, filesystem races and shared accounting conformance."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from brigade import context_preview as cp
from brigade.cli import main

pytestmark = pytest.mark.skipif(not cp._POSIX_NOFOLLOW_SUPPORTED, reason="POSIX nofollow contract")
CORPUS = json.loads(Path("docs/fixtures/codex-context-handoff-v1.json").read_text())


def producer():
    assert importlib.util.find_spec("brigade.context_handoff") is not None, "handoff producer is not implemented"
    return importlib.import_module("brigade.context_handoff")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_bytes(b"root")
    return root


def export(repo, cwd=None, **settings):
    inputs = dict(
        target=repo,
        cwd=cwd or repo,
        codex_version="0.160.0",
        trust="trusted",
        read_access="full",
        assume_codex_defaults=True,
    )
    inputs.update(settings)
    return producer().export(**inputs)


def refused(result, reason):
    assert result == {
        "schema": "brigade.context_handoff.error",
        "schema_version": 1,
        "actual_session_observed": False,
        "reason": reason,
    }


def test_export_complete_private_and_locally_bound(repo):
    result = export(repo)
    assert result["schema"] == "brigade.context_handoff"
    assert result["cwd"] == "."
    info = repo.stat()
    assert result["repository"] == {
        "location_sha256": hashlib.sha256(b"brigade.context_handoff.repository.v1\0" + str(repo).encode()).hexdigest(),
        "identity": {"device": info.st_dev, "inode": info.st_ino, "kind": "directory"},
    }
    assert result["sources"][0]["sha256"] == hashlib.sha256(b"root").hexdigest()
    assert result["baseline"] == {"selected_bytes": 4, "consumed_raw_bytes": 4, "rendered_utf8_bytes": 4}
    assert result["completeness"] == {
        "inspection": "complete",
        "source_hashes": True,
        "selection": True,
        "directories": True,
        "root_markers": True,
    }
    assert result["consumer"]["source_commit"] == CORPUS["source_commit"]
    from brigade import __version__

    assert result["producer"] == {"name": "brigade", "version": __version__}
    assert result["actual_session_observed"] is False
    serialized = json.dumps(result)
    assert str(repo.parent) not in serialized and '"root"' not in serialized
    digest = result.pop("digest")
    assert (
        digest
        == "sha256:"
        + hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
    )
    assert (repo / "AGENTS.md").read_bytes() == b"root"


@pytest.mark.parametrize("vector", CORPUS["vectors"], ids=lambda v: v["id"])
def test_shared_accounting_vectors(tmp_path, vector):
    producer()
    for directory in vector["directories"]:
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    for directory in vector["marker_directories"]:
        (tmp_path / directory / ".git").mkdir(parents=True)
    for name, value in vector["files"].items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes.fromhex(value))
    result = export(
        tmp_path,
        tmp_path / vector["cwd"],
        **{key: vector[key] for key in ("project_doc_max_bytes", "fallback_filenames", "root_markers")},
    )
    assert result["baseline"] == vector["expected_baseline"]
    assert [
        {k: row[k] for k in expected}
        for row, expected in zip(result["sources"], vector["expected_sources"], strict=True)
    ] == vector["expected_sources"]
    for name, value in vector["files"].items():
        assert (tmp_path / name).read_bytes() == bytes.fromhex(value)


@pytest.mark.parametrize("vector", CORPUS["canonical_digest_vectors"])
def test_canonical_vectors(vector):
    api = producer()
    assert api.canonical_bytes(vector["value"]).hex() == vector["canonical_utf8_hex"]
    assert api.integrity_digest(vector["value"]) == vector["digest"]


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), 1.0, -1, 2**64, "\ud800", {1: "x"}, (1,), {"x": True, "y": None}]
)
def test_canonical_strict_values(value):
    api = producer()
    if value == {"x": True, "y": None}:
        assert api.canonical_bytes(value) == b'{"x":true,"y":null}'
    else:
        with pytest.raises(ValueError):
            api.canonical_bytes(value)


@pytest.mark.parametrize(
    "settings,reason",
    [
        ({"project_doc_max_bytes": 0}, "inspection_disabled"),
        ({"trust": "untrusted"}, "inspection_disabled"),
        ({"read_access": "restricted"}, "inspection_disabled"),
        ({"codex_version": "unknown"}, "unsupported_version"),
        ({"trust": "unknown"}, "invalid_settings"),
        ({"project_doc_max_bytes": True}, "invalid_settings"),
        ({"project_doc_max_bytes": -1}, "invalid_settings"),
        ({"project_doc_max_bytes": 1.0}, "invalid_settings"),
        ({"project_doc_max_bytes": 1048577}, "resource_limit"),
        ({"fallback_filenames": [" x "]}, "invalid_settings"),
        ({"fallback_filenames": ["a/b"]}, "invalid_settings"),
        ({"fallback_filenames": ["a\\b"]}, "invalid_settings"),
        ({"fallback_filenames": ["x:y"]}, "invalid_settings"),
        ({"fallback_filenames": ["\n"]}, "invalid_settings"),
        ({"fallback_filenames": ["\ud800"]}, "invalid_settings"),
        ({"fallback_filenames": [True]}, "invalid_settings"),
        ({"root_markers": ".git"}, "invalid_settings"),
        ({"root_markers": [".."]}, "invalid_settings"),
        ({"root_markers": [".git"] * 65}, "resource_limit"),
        ({"assume_codex_defaults": False}, "invalid_settings"),
        ({"assume_codex_defaults": 1}, "invalid_settings"),
        ({"cwd": Path("relative")}, "unsafe_path"),
    ],
)
def test_strict_refusals(repo, settings, reason):
    refused(export(repo, **settings), reason)


def test_explicit_settings_and_duplicates(repo):
    result = export(
        repo,
        assume_codex_defaults=False,
        project_doc_max_bytes=8,
        fallback_filenames=["X", "X"],
        root_markers=[".git", ".git"],
        trust="unset",
    )
    assert result["settings"]["fallback_filenames"] == {"value": ["X", "X"], "source": "caller-supplied"}
    assert result["settings"]["project_doc_max_bytes"] == {"value": 8, "source": "caller-supplied"}


def test_selection_trace_includes_empty_directories_and_nonregulars(repo):
    (repo / "AGENTS.md").unlink()
    os.mkfifo(repo / "AGENTS.override.md")
    (repo / "AGENTS.md").mkdir()
    cwd = repo / "sub"
    cwd.mkdir()
    result = export(repo, cwd, fallback_filenames=["F"])
    observations = result["selection_observations"]
    assert [(o["directory"], o["name"], o["state"]) for o in observations] == [
        (".", "AGENTS.override.md", "fifo"),
        (".", "AGENTS.md", "directory"),
        (".", "F", "absent"),
        ("sub", "AGENTS.override.md", "absent"),
        ("sub", "AGENTS.md", "absent"),
        ("sub", "F", "absent"),
    ]
    assert result["sources"] == []
    assert result["root_marker_observations"][0] == {
        "directory": "sub",
        "name": ".git",
        "state": "absent",
        "identity": None,
    }


@pytest.mark.parametrize("kind", ["source", "marker", "cwd", "ancestor"])
def test_symlinks_refused(repo, kind):
    producer()
    cwd = repo
    if kind == "source":
        (repo / "AGENTS.override.md").symlink_to(repo / "AGENTS.md")
    elif kind == "marker":
        (repo / ".git").rmdir()
        (repo / ".git").symlink_to(repo, target_is_directory=True)
    elif kind == "cwd":
        cwd = repo / "sub"
        cwd.symlink_to(repo, target_is_directory=True)
    else:
        alias = repo.parent / "alias"
        alias.symlink_to(repo.parent, target_is_directory=True)
        repo = alias / "repo"
        cwd = repo
    refused(export(repo, cwd), "unsafe_path")


def test_source_socket_is_observed_not_opened(repo):
    producer()
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(repo / "AGENTS.override.md"))
        result = export(repo)
    assert result["selection_observations"][0]["state"] == "socket"
    assert result["sources"][0]["path"] == "AGENTS.md"


def test_cap_exhausted_hash_and_worktree_binding(repo, tmp_path):
    cwd = repo / "sub"
    cwd.mkdir()
    source = cwd / "AGENTS.md"
    source.write_bytes(b"private-tail-a")
    first = export(repo, cwd, project_doc_max_bytes=1)
    source.write_bytes(b"private-tail-b")
    second = export(repo, cwd, project_doc_max_bytes=1)
    assert first["baseline"] == second["baseline"]
    assert first["sources"][1]["sha256"] != second["sources"][1]["sha256"]
    assert first["sources"][1]["contribution"] == "cap_exhausted"
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").mkdir()
    (other / "AGENTS.md").write_bytes(b"root")
    assert export(other)["repository"] != first["repository"]


@pytest.mark.parametrize(
    "mutation", ["same_size", "replace", "override", "empty_winner", "marker", "directory", "nonregular"]
)
def test_final_revalidation_discards_export(repo, monkeypatch, mutation):
    producer()
    cwd = repo / "sub"
    cwd.mkdir()
    original = cp._revalidate_directories

    def change(bindings):
        if mutation == "same_size":
            (repo / "AGENTS.md").write_bytes(b"ROOT")
        elif mutation == "replace":
            replacement = repo / "replacement"
            replacement.write_bytes(b"root")
            replacement.replace(repo / "AGENTS.md")
        elif mutation == "override":
            (repo / "AGENTS.override.md").write_bytes(b"x")
        elif mutation == "empty_winner":
            (cwd / "AGENTS.md").write_bytes(b"x")
        elif mutation == "marker":
            (cwd / ".git").mkdir()
        elif mutation == "directory":
            cwd.rename(repo / "old")
            cwd.mkdir()
        else:
            (cwd / "AGENTS.override.md").mkdir()
        original(bindings)

    monkeypatch.setattr(cp, "_revalidate_directories", change)
    refused(export(repo, cwd), "inspection_changed")


def test_changed_during_read(repo, monkeypatch):
    producer()
    original = os.read

    def read(fd, count):
        value = original(fd, count)
        (repo / "AGENTS.md").write_bytes(b"ROOT")
        return value

    monkeypatch.setattr(os, "read", read)
    refused(export(repo), "inspection_changed")


def test_unreadable_has_no_raw_exception(repo, monkeypatch):
    producer()

    def read(fd, count):
        raise PermissionError("private body /secret/location")

    monkeypatch.setattr(os, "read", read)
    refused(export(repo), "identity_not_established")


def test_no_marker_and_outside_cwd_refuse(repo):
    (repo / ".git").rmdir()
    refused(export(repo), "identity_not_established")
    refused(export(repo, repo.parent), "unsafe_path")


@pytest.mark.parametrize(
    "limit,at,over",
    [
        ("HANDOFF_MAX_SOURCE_BYTES", 4, 3),
        ("HANDOFF_MAX_TOTAL_BYTES", 4, 3),
        ("HANDOFF_MAX_SOURCES", 1, 0),
        ("HANDOFF_MAX_DIRECTORIES", 1, 0),
    ],
)
def test_resource_boundaries(repo, monkeypatch, limit, at, over):
    producer()
    monkeypatch.setattr(cp, limit, at)
    assert export(repo)["schema"] == "brigade.context_handoff"
    monkeypatch.setattr(cp, limit, over)
    refused(export(repo), "resource_limit")


def test_complete_read_overflow_is_only_one_byte(repo, monkeypatch):
    producer()
    (repo / "AGENTS.md").write_bytes(b"x" * 5)
    monkeypatch.setattr(cp, "HANDOFF_MAX_SOURCE_BYTES", 4)
    original = os.read
    reads = []

    def read(fd, count):
        reads.append(count)
        return original(fd, count)

    monkeypatch.setattr(os, "read", read)
    refused(export(repo, project_doc_max_bytes=1), "resource_limit")
    assert sum(reads) <= 5  # Metadata may reject without a read.


def test_total_full_source_limit_includes_exhausted_sources(repo, monkeypatch):
    producer()
    cwd = repo / "sub"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_bytes(b"xy")
    monkeypatch.setattr(cp, "HANDOFF_MAX_TOTAL_BYTES", 5)
    refused(export(repo, cwd, project_doc_max_bytes=1), "resource_limit")


def test_list_boundary_and_path_and_envelope_limits(repo, monkeypatch):
    api = producer()
    assert export(repo, fallback_filenames=["X"] * 64)["schema"] == "brigade.context_handoff"
    refused(export(repo, fallback_filenames=["X"] * 65), "resource_limit")
    monkeypatch.setattr(api, "MAX_PATH_BYTES", len(str(repo).encode()))
    assert export(repo)["schema"] == "brigade.context_handoff"
    monkeypatch.setattr(api, "MAX_PATH_BYTES", len(str(repo).encode()) - 1)
    refused(export(repo), "resource_limit")
    monkeypatch.undo()
    size = len(api.canonical_bytes(export(repo)))
    monkeypatch.setattr(api, "MAX_ENVELOPE_BYTES", size)
    assert export(repo)["schema"] == "brigade.context_handoff"
    monkeypatch.setattr(api, "MAX_ENVELOPE_BYTES", size - 1)
    refused(export(repo), "resource_limit")


def cli_args(repo):
    return [
        "context",
        "handoff",
        "--target",
        str(repo),
        "--cwd",
        str(repo),
        "--codex-version",
        "0.160.0",
        "--trust",
        "trusted",
        "--read-access",
        "full",
        "--assume-codex-defaults",
        "--json",
    ]


def test_cli_source_import_and_json(repo):
    producer()
    env = {**os.environ, "PYTHONPATH": str(Path("src").absolute())}
    probe = subprocess.run(
        [sys.executable, "-c", "import brigade; print(brigade.__file__)"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert Path(probe.stdout.strip()).is_relative_to(Path("src").absolute())
    result = subprocess.run(
        [sys.executable, "-m", "brigade", *cli_args(repo)], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["schema"] == "brigade.context_handoff"
    assert result.stderr == ""


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--project-doc-max-bytes", "true"),
        ("--project-doc-max-bytes", "1.0"),
        ("--fallback-filenames", '["x",NaN]'),
        ("--root-markers", "{}"),
        ("--root-markers", '[".git"] trailing'),
        ("--fallback-filenames", "\ufeff[]"),
        ("--fallback-filenames", '{"x":[],"x":[]}'),
        ("--codex-home", "/private"),
        ("--global-max-bytes", "4"),
    ],
)
def test_cli_strict_input(repo, capsys, flag, value):
    producer()
    assert main([*cli_args(repo), flag, value]) == 2
    captured = capsys.readouterr()
    refused(json.loads(captured.out), "invalid_settings")
    assert captured.err == "" and value not in captured.out


def test_cli_requires_json(repo, capsys):
    producer()
    assert main(cli_args(repo)[:-1]) == 2
    refused(json.loads(capsys.readouterr().out), "json_required")


@pytest.mark.parametrize("mutation", ["same_size_tail", "replace_tail", "grow", "unlink", "truncate"])
def test_complete_source_races_after_cap_exhaustion(repo, monkeypatch, mutation):
    producer()
    cwd = repo / "sub"
    cwd.mkdir()
    source = cwd / "AGENTS.md"
    source.write_bytes(b"abcdef")
    info = source.stat()
    original = os.read

    def read(fd, count):
        value = original(fd, count)
        if os.fstat(fd).st_ino == info.st_ino:
            if mutation == "same_size_tail":
                source.write_bytes(b"abcdeF")
            elif mutation == "replace_tail":
                alternate = cwd / "alternate"
                alternate.write_bytes(b"abcdef")
                alternate.replace(source)
            elif mutation == "grow":
                with source.open("ab") as stream:
                    stream.write(b"z")
            elif mutation == "unlink":
                source.unlink(missing_ok=True)
            else:
                source.write_bytes(b"a")
        return value

    monkeypatch.setattr(os, "read", read)
    refused(export(repo, cwd, project_doc_max_bytes=1), "inspection_changed")


def test_reads_are_full_single_acquisitions_held_to_final_validation(repo, monkeypatch):
    api = producer()
    (repo / "AGENTS.md").write_bytes(b"abcdefgh")
    original_read, original_validate = os.read, api._finish_validation
    counts = []

    def read(fd, count):
        counts.append(count)
        return original_read(fd, min(count, 2))

    def validate(bindings, markers, selections, evidence):
        assert len(evidence.held_sources) == 1
        assert os.fstat(evidence.held_sources[0][0]).st_size == 8
        original_validate(bindings, markers, selections, evidence)

    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(api, "_finish_validation", validate)
    result = export(repo, project_doc_max_bytes=1)
    assert result["sources"][0]["sha256"] == hashlib.sha256(b"abcdefgh").hexdigest()
    assert result["sources"][0]["consumed_raw_bytes"] == 1
    assert len(counts) == 5  # Four chunks and EOF, one acquisition.


def test_source_open_flags_and_final_fstat_race(repo, monkeypatch):
    producer()
    original_open, original_revalidate = os.open, cp._revalidate

    def open_file(name, flags, *args, **kwargs):
        if name == "AGENTS.md":
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
            assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC) == 0
        return original_open(name, flags, *args, **kwargs)

    def revalidate(selections):
        original_revalidate(selections)
        (repo / "AGENTS.md").write_bytes(b"ROOT")

    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(cp, "_revalidate", revalidate)
    refused(export(repo), "inspection_changed")


def test_absolute_depth_and_canonical_depth_boundaries(repo, monkeypatch):
    api = producer()
    depth = len(repo.parts) - 1
    monkeypatch.setattr(api, "MAX_ABSOLUTE_DEPTH", depth)
    assert export(repo)["schema"] == "brigade.context_handoff"
    monkeypatch.setattr(api, "MAX_ABSOLUTE_DEPTH", depth - 1)
    refused(export(repo), "resource_limit")
    value = None
    for _ in range(64):
        value = [value]
    assert api.canonical_bytes(value) == b"[" * 64 + b"null" + b"]" * 64
    with pytest.raises(ValueError):
        api.canonical_bytes([value])


def test_real_source_ceiling_and_overflow_growth(repo, monkeypatch):
    producer()
    source = repo / "AGENTS.md"
    source.write_bytes(b"x" * 1048576)
    assert export(repo, project_doc_max_bytes=1)["sources"][0]["size_bytes"] == 1048576
    source.write_bytes(b"x" * 1048577)
    refused(export(repo, project_doc_max_bytes=1), "resource_limit")
    source.write_bytes(b"abcd")
    monkeypatch.setattr(cp, "HANDOFF_MAX_SOURCE_BYTES", 4)
    original = os.read
    total = 0

    def read(fd, count):
        nonlocal total
        if total == 0:
            with source.open("ab") as stream:
                stream.write(b"x" * 100)
        value = original(fd, count)
        total += len(value)
        return value

    monkeypatch.setattr(os, "read", read)
    refused(export(repo, project_doc_max_bytes=1), "inspection_changed")
    assert total == 5


def test_no_implicit_home_config_discovery(repo, tmp_path, monkeypatch):
    producer()
    home = tmp_path / "home"
    home.mkdir()
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text("project_doc_max_bytes = 0\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    assert export(repo)["baseline"]["consumed_raw_bytes"] == 4


def test_unsupported_platform_refuses_without_io(repo, monkeypatch):
    producer()
    monkeypatch.setattr(cp, "_POSIX_NOFOLLOW_SUPPORTED", False)
    refused(export(repo), "unsupported_platform")


@pytest.mark.parametrize(
    "key,value",
    [("--project-doc-max-bytes", "null"), ("--root-markers", "null"), ("--fallback-filenames", '["\\ud800"]')],
)
def test_cli_null_and_surrogates_refused(repo, capsys, key, value):
    producer()
    assert main([*cli_args(repo), key, value]) == 2
    refused(json.loads(capsys.readouterr().out), "invalid_settings")


def test_portable_export_fixture_materializes_local_identity_expectations(tmp_path):
    fixture = json.loads(Path("docs/fixtures/codex-context-handoff-export-v1.json").read_text())
    assert fixture["artifact_kind"] == "portable-test-template-not-a-consumable-handoff"
    for directory in fixture["setup"]["directories"]:
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    for name, content in fixture["setup"]["files"].items():
        (tmp_path / name).write_bytes(bytes.fromhex(content))
    expected = fixture["expected_export"]

    def materialize_identity(record, path):
        info = path.stat(follow_symlinks=False)
        values = {
            "device": info.st_dev,
            "inode": info.st_ino,
            "size_bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
        }
        for key in record:
            if key != "kind":
                assert record[key] == "${" + key + "}"
                record[key] = values[key]

    materialize_identity(expected["repository"]["identity"], tmp_path)
    for directory in expected["directories"]:
        materialize_identity(directory["identity"], tmp_path / directory["path"])
    for field in ("root_marker_observations", "selection_observations"):
        for observation in expected[field]:
            if observation["identity"] is not None:
                materialize_identity(observation["identity"], tmp_path / observation["directory"] / observation["name"])
    for source in expected["sources"]:
        materialize_identity(source["identity"], tmp_path / source["path"])
    expected["repository"]["location_sha256"] = hashlib.sha256(
        b"brigade.context_handoff.repository.v1\0" + str(tmp_path).encode()
    ).hexdigest()
    del expected["digest"]
    expected["digest"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(expected, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
    )
    actual = export(
        tmp_path,
        tmp_path / fixture["setup"]["cwd"],
        project_doc_max_bytes=4,
        fallback_filenames=[],
        root_markers=[".git"],
        assume_codex_defaults=False,
    )
    assert actual == expected


def test_digest_binds_nested_values_and_excludes_only_top_level_digest():
    api = producer()
    value = {"digest": "ignored", "nested": {"digest": "bound"}, "order": [2, 1]}
    expected = "sha256:" + hashlib.sha256(b'{"nested":{"digest":"bound"},"order":[2,1]}').hexdigest()
    assert api.integrity_digest(value) == expected
    value["digest"] = "also ignored"
    assert api.integrity_digest(value) == expected
    value["nested"]["digest"] = "changed"
    assert api.integrity_digest(value) != expected


def test_late_observation_and_marker_mutations_are_revalidated(repo, monkeypatch):
    producer()
    cwd = repo / "sub"
    cwd.mkdir()
    original = cp._revalidate_markers
    calls = 0

    def revalidate(observations):
        nonlocal calls
        calls += 1
        original(observations)
        if calls == 1:
            (cwd / "AGENTS.override.md").write_bytes(b"new")

    monkeypatch.setattr(cp, "_revalidate_markers", revalidate)
    refused(export(repo, cwd), "inspection_changed")


def test_absent_marker_becomes_nonregular_boundary(repo):
    cwd = repo / "sub"
    cwd.mkdir()
    original = export(repo, cwd)
    os.mkfifo(cwd / ".git")
    changed = export(repo, cwd)
    assert original["root_marker_observations"][0]["state"] == "absent"
    assert changed["root_marker_observations"][0]["state"] == "fifo"
    assert original["sources"][0]["path"] == "AGENTS.md"
    assert changed["sources"] == []
    assert original["digest"] != changed["digest"]
