"""Tests for bounded session-start memory recall (#466 Slice 1)."""

from __future__ import annotations

import json
import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import cli, config, memory_hooks
from brigade.config import Config
from brigade.selection import Selection


def _write_card(target: Path, name: str, title: str, body: str, tags: list[str] | None = None) -> None:
    cards = target / "memory" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    tags = tags or []
    tag_line = ""
    if tags:
        rendered = "[" + ", ".join(f'"{t}"' for t in tags) + "]"
        tag_line = f"tags: {rendered}\n"
    (cards / name).write_text(f"---\ntitle: {title}\n{tag_line}---\n{body}\n", encoding="utf-8")


def _write_brigade_config(target: Path, *, depth: str = "repo", memory_recall_target: str | None = None) -> None:
    cfg = Config(
        version=1,
        selection=Selection(depth=depth, harnesses=["claude"], owner="claude", includes=[]),
        memory_recall_target=memory_recall_target,
    )
    config.write_config(target, cfg)


def test_astro_portfolio_cwd_becomes_astro_portfolio_terms():
    assert memory_hooks.query_from_cwd(Path("/tmp/astro-portfolio")) == "astro portfolio"
    assert memory_hooks.split_cwd_terms("astro_portfolio") == ["astro", "portfolio"]
    assert memory_hooks.split_cwd_terms("Astro-Portfolio") == ["astro", "portfolio"]


def test_memory_root_cwd_uses_generic_workspace_fallback(tmp_path: Path):
    hub = tmp_path / "agent-workspace"
    hub.mkdir()
    assert memory_hooks.query_from_cwd(hub, memory_root=hub) == "workspace"


def test_recall_output_has_title_tags_path_only_no_body(tmp_path: Path, capsys):
    hub = tmp_path / "hub"
    session = tmp_path / "astro-portfolio"
    hub.mkdir()
    session.mkdir()
    card_body = "UNIQUE_BODY_TOKEN_SHOULD_NOT_LEAK"
    _write_card(hub, "astro.md", "Astro Notes", card_body, tags=["astro"])
    _write_card(hub, "other.md", "Unrelated", "no match here", tags=["other"])

    rc = memory_hooks.recall(target=hub, cwd=session, limit=5, json_output=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "memory recall: astro portfolio" in out
    assert "Astro Notes" in out
    assert "astro.md" in out
    assert "tags: astro" in out
    assert card_body not in out
    assert "UNIQUE_BODY" not in out
    assert out.count("\n") <= memory_hooks.RECALL_MAX_LINES


def test_recall_json_omits_body_and_summary(tmp_path: Path, capsys):
    hub = tmp_path / "hub"
    session = tmp_path / "astro-portfolio"
    hub.mkdir()
    session.mkdir()
    _write_card(hub, "astro.md", "Astro Notes", "body text must stay out of json", tags=["astro"])
    rc = memory_hooks.recall(target=hub, cwd=session, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["query"] == "astro portfolio"
    assert payload["matches"]
    for match in payload["matches"]:
        assert set(match) <= {"path", "card_id", "card_aliases", "title", "tags", "score"}
        assert "card_id" in match
        assert "card_aliases" in match
        assert "summary" not in match
        assert "body" not in match
        assert "body text" not in json.dumps(match)


def test_recall_json_resolves_explicit_id_and_legacy_alias(tmp_path: Path):
    hub = tmp_path / "hub"
    session = tmp_path / "astro-portfolio"
    hub.mkdir()
    session.mkdir()
    card_id = "card-123e4567-e89b-42d3-a456-426614174000"
    cards = hub / "memory" / "cards"
    cards.mkdir(parents=True)
    (cards / "renamed.md").write_text(
        f'---\nid: {card_id}\ntitle: Astro Notes\ntags: ["astro"]\n---\nastro body\n',
        encoding="utf-8",
    )
    payload = memory_hooks.recall_cards_payload(target=hub, cwd=session)
    assert payload["matches"]
    match = payload["matches"][0]
    assert match["card_id"] == card_id
    assert "memory/cards/renamed.md" in match["card_aliases"]
    assert "renamed" in match["card_aliases"]


def test_recall_caps_matches_and_stable_equal_score_order(tmp_path: Path):
    hub = tmp_path / "hub"
    session = tmp_path / "demo-repo"
    hub.mkdir()
    session.mkdir()
    for name in ("zeta.md", "alpha.md", "mid.md", "beta.md", "gamma.md", "delta.md"):
        _write_card(hub, name, f"Demo {name}", f"mentions demo in body for {name}", tags=["demo"])
    payload = memory_hooks.recall_cards_payload(target=hub, cwd=session, limit=99)
    assert len(payload["matches"]) == memory_hooks.DEFAULT_RECALL_LIMIT
    scores = [m["score"] for m in payload["matches"]]
    assert scores == sorted(scores, reverse=True)
    # Equal scores order by path for stability.
    equal = [m for m in payload["matches"] if m["score"] == scores[0]]
    assert [m["path"] for m in equal] == sorted(m["path"] for m in equal)


def test_missing_and_broken_targets_exit_zero_without_output(tmp_path: Path, capsys):
    missing = tmp_path / "no-such-hub"
    cwd = tmp_path / "astro-portfolio"
    cwd.mkdir()
    assert memory_hooks.recall(target=missing, cwd=cwd) == 0
    assert capsys.readouterr().out == ""

    broken = tmp_path / "broken-hub"
    broken.mkdir()
    (broken / "memory").mkdir()
    # Unreadable cards dir is still fail-open with no rendered matches required.
    assert memory_hooks.recall(target=broken, cwd=cwd) == 0
    # No matches means no text output.
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("query", "expected", "source"),
    [
        (None, "missing hub", "cwd"),
        ("\x00\n\t", "missing hub", "cwd"),
        ("\x00comet\n guide", "comet guide", "explicit"),
    ],
)
def test_missing_target_equal_to_cwd_preserves_basename_query(tmp_path: Path, capsys, query, expected, source):
    target = tmp_path / "missing-hub"
    assert memory_hooks.recall(target=target, cwd=target, query=query, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == expected
    assert payload["query_source"] == source
    assert payload["status"] == "missing-target"
    assert payload["matches"] == []


def test_cli_memory_recall_json(tmp_path: Path, capsys):
    hub = tmp_path / "hub"
    session = tmp_path / "astro-portfolio"
    hub.mkdir()
    session.mkdir()
    _write_card(hub, "astro.md", "Astro Notes", "secret-body", tags=["astro"])
    assert (
        cli.main(
            [
                "memory",
                "recall",
                "--target",
                str(hub),
                "--cwd",
                str(session),
                "--limit",
                "5",
                "--json",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["query"] == "astro portfolio"
    assert payload["matches"][0]["title"] == "Astro Notes"
    assert "secret-body" not in out


def test_resolve_memory_recall_target_workspace_defaults(tmp_path: Path):
    _write_brigade_config(tmp_path, depth="workspace")
    path, status = memory_hooks.resolve_memory_recall_target(tmp_path)
    assert status == "active"
    assert path == tmp_path.resolve()


def test_resolve_memory_recall_target_repo_unconfigured(tmp_path: Path):
    _write_brigade_config(tmp_path, depth="repo")
    path, status = memory_hooks.resolve_memory_recall_target(tmp_path)
    assert status == "unconfigured"
    assert path is None
    assert memory_hooks.recall_text_for_hook(wired_target=tmp_path, cwd=tmp_path / "astro-portfolio") == ""


def test_resolve_memory_recall_target_explicit_mirror(tmp_path: Path):
    repo = tmp_path / "repo"
    mirror = tmp_path / "mirror"
    repo.mkdir()
    mirror.mkdir()
    _write_brigade_config(repo, depth="repo", memory_recall_target=str(mirror))
    path, status = memory_hooks.resolve_memory_recall_target(repo)
    assert status == "active"
    assert path == mirror.resolve()


def _stub_recall_subprocess(
    monkeypatch,
    *,
    timeout: bool = False,
    returncode: int = 0,
    stdout: str = "",
) -> None:
    def fake_run(*args, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired(
                cmd=kwargs.get("args") or (args[0] if args else []),
                timeout=kwargs.get("timeout") or 0.0,
            )
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(memory_hooks.subprocess, "run", fake_run)


def test_recall_cards_payload_enforces_timeout_and_fail_open(tmp_path: Path, monkeypatch):
    hub = tmp_path / "hub"
    session = tmp_path / "astro-portfolio"
    hub.mkdir()
    session.mkdir()
    _write_card(hub, "astro.md", "Astro Notes", "body", tags=["astro"])
    _stub_recall_subprocess(monkeypatch, timeout=True)
    payload = memory_hooks.recall_cards_payload(target=hub, cwd=session)
    assert payload["status"] == "timeout"
    assert payload["matches"] == []
    assert payload["match_count"] == 0
    assert payload["target"] == str(hub)
    assert payload["cwd"] == str(session)


def test_recall_cards_payload_nonzero_exit_and_malformed_stdout_fail_open(tmp_path: Path, monkeypatch):
    hub = tmp_path / "hub"
    session = tmp_path / "astro-portfolio"
    hub.mkdir()
    session.mkdir()

    _stub_recall_subprocess(monkeypatch, returncode=1)
    failed = memory_hooks.recall_cards_payload(target=hub, cwd=session)
    assert failed["status"] == "error"
    assert failed["matches"] == []

    _stub_recall_subprocess(monkeypatch, stdout="not-json")
    malformed = memory_hooks.recall_cards_payload(target=hub, cwd=session)
    assert malformed["status"] == "error"
    assert malformed["matches"] == []


def test_recall_ignores_legacy_hang_env_variable(tmp_path: Path, monkeypatch):
    hub = tmp_path / "hub"
    session = tmp_path / "astro-portfolio"
    hub.mkdir()
    session.mkdir()
    _write_card(hub, "astro.md", "Astro Notes", "body", tags=["astro"])
    monkeypatch.setenv("BRIGADE_RECALL_TEST_HANG_SECONDS", "30")
    payload = memory_hooks.recall_cards_payload(target=hub, cwd=session)
    assert payload["status"] == "ok"
    assert payload["matches"]


def test_recall_text_for_hook_timeout_returns_empty(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    hub = tmp_path / "hub"
    session = tmp_path / "astro-portfolio"
    repo.mkdir()
    hub.mkdir()
    session.mkdir()
    _write_brigade_config(repo, depth="repo", memory_recall_target=str(hub))
    _write_card(hub, "astro.md", "Astro Notes", "body", tags=["astro"])
    _stub_recall_subprocess(monkeypatch, timeout=True)
    text = memory_hooks.recall_text_for_hook(wired_target=repo, cwd=session)
    assert text == ""


def test_recall_cli_timeout_json_is_fail_open(tmp_path: Path, monkeypatch, capsys):
    hub = tmp_path / "hub"
    session = tmp_path / "astro-portfolio"
    hub.mkdir()
    session.mkdir()
    _stub_recall_subprocess(monkeypatch, timeout=True)
    rc = memory_hooks.recall(target=hub, cwd=session, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "timeout"
    assert payload["matches"] == []


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
    )


def _repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "orbit-notes"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(
        repo,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.invalid",
        "commit",
        "-qm",
        "initial",
        "--allow-empty",
    )
    worktree = tmp_path / "task-1563"
    _git(repo, "worktree", "add", "-qb", "example-task", str(worktree))
    return repo, worktree


def test_explicit_query_precedes_repo_and_workspace(tmp_path: Path):
    repo, _ = _repo_with_worktree(tmp_path)
    hub = tmp_path / "hub"
    _write_card(hub, "comet.md", "Comet guide", "private body", tags=["comet"])
    _write_card(hub, "orbit.md", "Orbit notes", "private body", tags=["orbit"])
    _write_card(hub, "workspace.md", "Workspace notes", "private body", tags=["workspace"])
    for cwd in (repo, hub):
        payload = memory_hooks.recall_cards_payload(target=hub, cwd=cwd, query="comet")
        assert payload["query"] == "comet"
        assert payload["query_source"] == "explicit"
        assert [match["title"] for match in payload["matches"]] == ["Comet guide"]


def test_primary_and_linked_checkout_recall_same_cards_only_from_target(tmp_path: Path):
    repo, worktree = _repo_with_worktree(tmp_path)
    hub = tmp_path / "hub"
    _write_card(hub, "orbit.md", "Orbit notes", "private body", tags=["orbit"])
    _write_card(repo, "private.md", "Orbit private primary", "must not leak", tags=["orbit"])
    _write_card(worktree, "private.md", "Orbit private worktree", "must not leak", tags=["orbit"])
    for cwd in (worktree, repo):
        payload = memory_hooks.recall_cards_payload(target=hub, cwd=cwd)
        assert payload["query"] == "orbit notes"
        assert payload["query_source"] == "repo"
        assert [match["title"] for match in payload["matches"]] == ["Orbit notes"]
        assert str(repo / ".git") not in json.dumps(payload)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("  comet\n\tguide\x00\x1b\u2028next  ", "comet guide next"),
        ("a " * 126 + "tailword", " ".join(["a"] * 126)),
        ("x" * 300, "x" * 256),
        ("x" * 256 + " next", "x" * 256),
    ],
)
def test_explicit_query_normalization_and_bounds(tmp_path: Path, query: str, expected: str):
    _write_card(tmp_path, "match.md", "Comet guide next", "a " + "x" * 256, tags=["comet"])
    payload = memory_hooks.recall_cards_payload(target=tmp_path, cwd=tmp_path, query=query)
    assert payload["query"] == expected
    assert payload["query_source"] == "explicit"
    assert payload["matches"]
    assert len(payload["query"]) <= 256
    assert all(char.isprintable() for char in payload["query"])


def test_blank_explicit_query_falls_through_repo_workspace_and_cwd(tmp_path: Path):
    repo, worktree = _repo_with_worktree(tmp_path)
    hub = tmp_path / "hub"
    _write_card(hub, "orbit.md", "Orbit notes", "workspace sub section", tags=["orbit"])
    subdir = worktree / "sub-section"
    subdir.mkdir()
    for cwd, expected, source in (
        (worktree, "orbit notes", "repo"),
        (hub, "workspace", "workspace"),
        (subdir, "sub section", "cwd"),
    ):
        payload = memory_hooks.recall_cards_payload(target=hub, cwd=cwd, query="\x00\n\t")
        assert payload["query"] == expected
        assert payload["query_source"] == source
        assert payload["matches"]


def test_workspace_query_precedes_repository_identity(tmp_path: Path):
    repo, _ = _repo_with_worktree(tmp_path)
    _write_card(repo, "workspace.md", "Workspace guide", "private body", tags=["workspace"])
    _write_card(repo, "orbit.md", "Orbit guide", "private body", tags=["orbit"])
    payload = memory_hooks.recall_cards_payload(target=repo, cwd=repo)
    assert payload["query"] == "workspace"
    assert payload["query_source"] == "workspace"
    assert [match["title"] for match in payload["matches"]] == ["Workspace guide"]


@pytest.mark.parametrize("layout", ["bare", "bare-worktree", "separate", "submodule"])
def test_nonconventional_git_layouts_keep_cwd_fallback(tmp_path: Path, layout: str):
    cwd = tmp_path / "fallback-notes"
    cwd.mkdir()
    if layout in {"bare", "bare-worktree"}:
        _git(cwd, "init", "-q", "--bare")
        if layout == "bare-worktree":
            source, _ = _repo_with_worktree(tmp_path)
            _git(cwd, "fetch", str(source), "HEAD:refs/heads/example")
            bare = cwd
            cwd = tmp_path / "fallback-checkout"
            _git(bare, "worktree", "add", "-q", str(cwd), "example")
    elif layout == "separate":
        _git(cwd, "init", "-q", "--separate-git-dir", str(tmp_path / "metadata"))
    else:
        parent, _ = _repo_with_worktree(tmp_path)
        _git(parent, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(parent), "fallback-module")
        cwd = parent / "fallback-module"
    hub = tmp_path / "hub"
    _write_card(hub, "fallback.md", "Fallback notes", "private body", tags=["fallback"])
    payload = memory_hooks.recall_cards_payload(target=hub, cwd=cwd)
    assert payload["query"] == cwd.name.replace("-", " ")
    assert payload["query_source"] == "cwd"
    assert payload["matches"][0]["title"] == "Fallback notes"


def test_separate_git_dir_named_dotgit_keeps_cwd_fallback(tmp_path: Path):
    cwd = tmp_path / "fallback-notes"
    cwd.mkdir()
    external = tmp_path / "external-name"
    external.mkdir()
    _git(cwd, "init", "-q", "--separate-git-dir", str(external / ".git"))
    hub = tmp_path / "hub"
    _write_card(hub, "fallback.md", "Fallback notes", "private body", tags=["fallback"])
    _write_card(hub, "external.md", "External metadata", "private body", tags=["external"])
    payload = memory_hooks.recall_cards_payload(target=hub, cwd=cwd)
    assert payload["query"] == "fallback notes"
    assert payload["query_source"] == "cwd"
    assert [match["title"] for match in payload["matches"]] == ["Fallback notes"]


@pytest.mark.parametrize(
    "marker_kind",
    [
        "direct",
        "nested",
        "empty",
        "malformed",
        "control",
        "multiline",
        "oversized",
        "decode",
        "directory",
        pytest.param("fifo", marks=pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO")),
    ],
)
def test_git_marker_rejects_nonconventional_shape(tmp_path: Path, monkeypatch, marker_kind: str):
    cwd = tmp_path / "fallback-notes"
    cwd.mkdir()
    common_dir = tmp_path / "orbit-notes" / ".git"
    marker = cwd / ".git"
    gitdir = common_dir / "worktrees" / "example"
    if marker_kind == "directory":
        marker.mkdir()
    elif marker_kind == "fifo":
        os.mkfifo(marker)
    else:
        contents = {
            "direct": f"gitdir: {common_dir}\n".encode(),
            "nested": f"gitdir: {gitdir / 'nested'}\n".encode(),
            "empty": b"gitdir: \n",
            "malformed": f"other: {gitdir}\n".encode(),
            "control": f"gitdir: {gitdir}\x00\n".encode(),
            "multiline": f"gitdir: {gitdir}\nextra\n".encode(),
            "oversized": f"gitdir: {gitdir}".encode() + b" " * 4096,
            "decode": b"gitdir: \xff\n",
        }
        marker.write_bytes(contents[marker_kind])
    hub = tmp_path / "hub"
    _write_card(hub, "fallback.md", "Fallback notes", "private body", tags=["fallback"])

    def fake_git(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=f"{cwd}\n{common_dir}\n".encode())

    monkeypatch.setattr(memory_hooks.subprocess, "run", fake_git)
    payload = memory_hooks._recall_cards_payload_impl(target=hub, cwd=cwd)
    assert payload["query"] == "fallback notes"
    assert payload["query_source"] == "cwd"
    assert payload["matches"][0]["title"] == "Fallback notes"


def test_poisoned_git_environment_does_not_redirect_identity(tmp_path: Path, monkeypatch):
    repo, worktree = _repo_with_worktree(tmp_path)
    hub = tmp_path / "hub"
    _write_card(hub, "orbit.md", "Orbit notes", "private body", tags=["orbit"])
    for key, value in {
        "GIT_DIR": str(tmp_path / "missing"),
        "GIT_WORK_TREE": str(hub),
        "GIT_COMMON_DIR": str(hub),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.bare",
        "GIT_CONFIG_VALUE_0": "true",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "poison-config"),
    }.items():
        monkeypatch.setenv(key, value)
    payload = memory_hooks.recall_cards_payload(target=hub, cwd=worktree)
    assert payload["query"] == "orbit notes"
    assert payload["query_source"] == "repo"
    assert payload["matches"][0]["title"] == "Orbit notes"
    assert str(repo / ".git") not in json.dumps(payload)


@pytest.mark.parametrize(
    "response",
    ["nonzero", "unavailable", "timeout", "value-error", "decode", "control", "extra", "empty", "above", "odd-common"],
)
def test_git_protocol_rejects_bad_identity_and_fails_open(tmp_path: Path, monkeypatch, response: str):
    cwd = tmp_path / "fallback-notes"
    cwd.mkdir()
    (cwd / ".git").mkdir()
    hub = tmp_path / "hub"
    _write_card(hub, "fallback.md", "Fallback notes", "private body", tags=["fallback"])

    def fake_git(*args, **kwargs):
        if response == "unavailable":
            raise OSError("unavailable")
        if response == "timeout":
            raise subprocess.TimeoutExpired(args[0], 0.5)
        if response == "value-error":
            raise ValueError("bad invocation")
        output = {
            "decode": b"\xff\n.git\n",
            "control": f"{cwd}\x1b\n.git\n".encode(),
            "extra": f"{cwd}\n.git\nextra\n".encode(),
            "empty": f"{cwd}\n\n".encode(),
            "above": f"{tmp_path}\n.git\n".encode(),
            "odd-common": f"{cwd}\nmetadata\n".encode(),
        }.get(response, f"{cwd}\n.git\n".encode())
        return SimpleNamespace(returncode=1 if response == "nonzero" else 0, stdout=output)

    monkeypatch.setattr(memory_hooks.subprocess, "run", fake_git)
    payload = memory_hooks._recall_cards_payload_impl(target=hub, cwd=cwd)
    assert payload["query"] == "fallback notes"
    assert payload["query_source"] == "cwd"
    assert payload["matches"][0]["title"] == "Fallback notes"


def test_git_probe_confined_to_worker_and_hardened(tmp_path: Path, monkeypatch):
    repo, worktree = _repo_with_worktree(tmp_path)
    hub = tmp_path / "hub"
    _write_card(hub, "orbit.md", "Orbit notes", "private body", tags=["orbit"])
    monkeypatch.setenv("GIT_EXAMPLE_POISON", "poison")
    calls = []

    def fake_git(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=f"{worktree}\n{repo / '.git'}\n".encode())

    monkeypatch.setattr(memory_hooks.subprocess, "run", fake_git)
    payload = memory_hooks._recall_cards_payload_impl(target=hub, cwd=worktree)
    assert payload["query"] == "orbit notes"
    assert payload["matches"][0]["title"] == "Orbit notes"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == [
        "git",
        "-C",
        str(worktree),
        "--no-replace-objects",
        "rev-parse",
        "--show-toplevel",
        "--git-common-dir",
    ]
    assert kwargs["stdin"] == kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["shell"] is False and kwargs["check"] is False
    assert 0 < kwargs["timeout"] <= 0.5
    assert {key: value for key, value in kwargs["env"].items() if key.startswith("GIT_")} == {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    calls.clear()
    for cwd, query in ((worktree, "explicit"), (hub, None), (worktree / "missing-subdir", None)):
        memory_hooks._recall_cards_payload_impl(target=hub, cwd=cwd, query=query)
    assert calls == []

    def timed_worker(args, **kwargs):
        assert args[0] == sys.executable
        assert kwargs["timeout"] == 5
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(memory_hooks.subprocess, "run", timed_worker)
    payload = memory_hooks.recall_cards_payload(target=hub, cwd=worktree)
    assert payload["query"] == "task 1563"
    assert payload["query_source"] == "cwd"
    assert payload["status"] == "timeout"


def test_worker_renormalizes_query_before_search(tmp_path: Path, monkeypatch):
    _write_card(tmp_path, "comet.md", "Comet notes", "private body", tags=["comet"])
    request = {"target": str(tmp_path), "cwd": str(tmp_path), "limit": 99, "query": "\x00comet\n"}
    output = io.StringIO()
    monkeypatch.setattr(memory_hooks.sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(memory_hooks.sys, "stdout", output)
    memory_hooks._recall_worker_main()
    payload = json.loads(output.getvalue())
    assert payload["query"] == "comet"
    assert payload["query_source"] == "explicit"
    assert payload["matches"][0]["title"] == "Comet notes"
    assert payload["limit"] == 5


def test_module_query_and_hook_api_recall_real_cards(tmp_path: Path):
    hub = tmp_path / "hub"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_card(hub, "comet.md", "Comet notes", "SECRET_BODY", tags=["comet"])
    _write_brigade_config(repo, memory_recall_target=str(hub))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "brigade.memory_hooks",
            "--target",
            str(hub),
            "--cwd",
            str(repo),
            "--query",
            "comet",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["query_source"] == "explicit"
    assert payload["matches"][0]["title"] == "Comet notes"
    text = memory_hooks.recall_text_for_hook(wired_target=repo, cwd=repo, query="comet")
    assert "memory recall: comet" in text
    assert "Comet notes" in text
    assert "SECRET_BODY" not in text + result.stdout


@pytest.mark.parametrize("failure", ["missing", "timeout", "exception"])
def test_parent_failure_payload_attributes_normalized_explicit_query(tmp_path: Path, monkeypatch, capsys, failure: str):
    target = tmp_path / "hub"
    if failure != "missing":
        target.mkdir()
    if failure == "timeout":
        _stub_recall_subprocess(monkeypatch, timeout=True)
    if failure == "exception":

        def fail(**kwargs):
            raise ValueError("unavailable")

        monkeypatch.setattr(memory_hooks, "recall_cards_payload", fail)
    assert memory_hooks.recall(target=target, cwd=tmp_path, query="\x00comet\n", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == "comet"
    assert payload["query_source"] == "explicit"
    assert payload["matches"] == []
    assert payload["status"] == {"missing": "missing-target", "timeout": "timeout", "exception": "error"}[failure]


@pytest.mark.parametrize("query", [None, "comet"])
def test_recall_context_resolution_failure_stays_fail_open(tmp_path: Path, monkeypatch, capsys, query: str | None):
    def fail(*args, **kwargs):
        raise RuntimeError("context unavailable")

    monkeypatch.setattr(Path, "resolve", fail)
    assert memory_hooks.recall(target=tmp_path, cwd=tmp_path, query=query, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["query"] == (query or "")
    assert payload["query_source"] == ("explicit" if query else "cwd")
    assert payload["matches"] == []


def test_rendered_fields_are_printable_physical_lines_without_field_truncation():
    title = "Long " + "x" * 300 + "\nsecond\x1b"
    match = {
        "title": title,
        "tags": ["tag\r\nnext", "\x00another"],
        "path": "memory/\ncard\u2028name.md",
        "body": "SECRET_BODY",
    }
    payload = {"query": "comet\n\x00" + "q" * 300, "matches": [match] * 12}
    text = memory_hooks.format_recall_text(payload)
    assert len(text.splitlines()) == 6
    assert len(text.splitlines()) <= 10
    assert all(char.isprintable() for line in text.splitlines() for char in line)
    assert "x" * 300 in text
    assert "tags: tag next, another" in text
    assert "memory/ card name.md" in text
    assert text.splitlines()[0] == "memory recall: comet"
    assert "SECRET_BODY" not in text
