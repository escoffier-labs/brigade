"""Tests for bounded session-start memory recall (#466 Slice 1)."""

from __future__ import annotations

import json
from pathlib import Path

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
        assert set(match) <= {"path", "title", "tags", "score"}
        assert "summary" not in match
        assert "body" not in match
        assert "body text" not in json.dumps(match)


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
