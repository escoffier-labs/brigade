"""Tests for bounded session-start memory recall (#466 Slice 1)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

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
