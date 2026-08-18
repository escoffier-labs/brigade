"""Acceptance tests for the optional Obsidian vault projection (#888)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import cli, obsidian_vault
from brigade.projection import kernel


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A throwaway Obsidian vault, never an operator vault."""
    path = tmp_path / "vault"
    path.mkdir()
    return path


def _write_card(
    target: Path, name: str, *, title: str, body: str, tags: list[str], category: str = "operations"
) -> None:
    path = target / "memory" / "cards" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = {"alpha": "00000000000a", "beta": "00000000000b"}[name]
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: card-00000000-0000-4000-8000-{suffix}",
                f"title: {title}",
                f"category: {category}",
                f"tags: {json.dumps(tags)}",
                "fresh_until: 2099-01-01",
                "last_reviewed: 2026-08-14",
                "source_harness: codex",
                'refs: ["memory/cards/beta.md"]' if name == "alpha" else "refs: []",
                "---",
                body,
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_cards(target: Path, count: int, *, category: str, tags: list[str]) -> None:
    """Write `count` sibling cards that all share one category, the shape that blew up the canvas."""
    root = target / "memory" / "cards"
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / f"bulk-{index:03d}.md").write_text(
            "\n".join(
                [
                    "---",
                    f"id: card-00000000-0000-4000-8000-{index:012d}",
                    f"title: Bulk {index:03d}",
                    f"category: {category}",
                    f"tags: {json.dumps(tags)}",
                    "refs: []",
                    "---",
                    f"Bulk body {index:03d}.",
                    "",
                ]
            ),
            encoding="utf-8",
        )


def _project(target: Path, vault: Path, capsys) -> dict:
    assert cli.main(["memory", "project-vault", "--target", str(target), "--vault", str(vault), "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_project_vault_is_linked_redacted_idempotent_and_reports_drift(tmp_path: Path, vault: Path, capsys) -> None:
    _write_card(
        tmp_path,
        "alpha",
        title="Alpha",
        tags=["operations", "shared"],
        body="Use the canonical process. Authorization: Bearer should-not-escape.",
    )
    _write_card(tmp_path, "beta", title="Beta", tags=["operations", "shared"], body="Related guidance.")
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "retention.md").write_text("---\ntitle: Retention\n---\nKeep records.\n", encoding="utf-8")
    (tmp_path / "TOOLS.md").write_text("---\ntitle: Tool Notes\n---\nUse approved tools.\n", encoding="utf-8")

    first = _project(tmp_path, vault, capsys)

    projection = vault / "Brigade Memory"
    alpha = projection / "Cards" / "Alpha.md"
    beta = projection / "Cards" / "Beta.md"
    assert first["written"] > 0
    assert alpha.is_file() and beta.is_file()
    assert (projection / "Rules" / "Retention.md").is_file()
    assert (projection / "Tools" / "Tool Notes.md").is_file()
    text = alpha.read_text(encoding="utf-8")
    assert "generated: true" in text
    assert "care_state: fresh" in text
    assert "freshness_date: 2099-01-01" in text
    assert "source_harness: codex" in text
    assert "care/fresh" in text and "harness/codex" in text
    assert "[[Cards/Beta|Beta]]" in text
    assert "should-not-escape" not in text
    assert (projection / "Maps" / "Categories" / "operations.md").read_text(encoding="utf-8").count("[[") == 2
    assert "generated: true" in (projection / "Maps" / "Harnesses" / "codex.md").read_text(encoding="utf-8")
    canvas = json.loads((projection / "Brigade Memory.canvas").read_text(encoding="utf-8"))
    assert canvas["nodes"] and canvas["edges"]
    assert any(node["id"] == "topology:canonical:cards" for node in canvas["nodes"])
    assert any(edge["id"] == "topology:edge:ingest:write:cards" for edge in canvas["edges"])

    before = {path.relative_to(projection): path.stat().st_mtime_ns for path in projection.rglob("*") if path.is_file()}
    second = _project(tmp_path, vault, capsys)
    after = {path.relative_to(projection): path.stat().st_mtime_ns for path in projection.rglob("*") if path.is_file()}
    assert second["written"] == 0
    assert second["receipt"]["mutation_counts"]["total"] == 0
    assert before == after

    alpha.write_text(text + "\nmanual vault edit\n", encoding="utf-8")
    drifted = _project(tmp_path, vault, capsys)
    assert "Cards/Alpha.md" in drifted["drift"]
    assert "manual vault edit" in alpha.read_text(encoding="utf-8")
    beta_text = beta.read_text(encoding="utf-8")
    assert "[[Cards/Alpha.conflict-" in beta_text

    assert cli.main(["memory", "topology", "--target", str(tmp_path), "--json"]) == 0
    topology = json.loads(capsys.readouterr().out)
    node = next(item for item in topology["nodes"] if item["id"] == "derived:obsidian_vault")
    assert node["owner"] == "brigade"
    assert node["latest_run"] is not None


def test_project_vault_redacts_basic_authorization_credentials(tmp_path: Path, vault: Path, capsys) -> None:
    _write_card(
        tmp_path,
        "alpha",
        title="Alpha",
        tags=["operations"],
        body="Authorization: Basic should-not-escape",
    )

    _project(tmp_path, vault, capsys)

    text = (vault / "Brigade Memory" / "Cards" / "Alpha.md").read_text(encoding="utf-8")
    assert "should-not-escape" not in text
    assert "Authorization: [redacted]" in text


def test_project_vault_restores_the_vault_when_a_transaction_fails(
    tmp_path: Path, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_card(tmp_path, "alpha", title="Alpha", tags=["operations"], body="Canonical note.")
    real_execute = obsidian_vault.kernel.execute

    def fail_during_commit(plan, *, target):
        return real_execute(plan, target=target, inject=kernel.FailureInjector(boundary="commit:2:before"))

    monkeypatch.setattr(obsidian_vault.kernel, "execute", fail_during_commit)

    result = obsidian_vault._project_payload(target=tmp_path, vault=vault)

    assert result["receipt"]["terminal_state"] == "restored"
    assert result["written"] == 0
    projection = vault / "Brigade Memory"
    assert not [path for path in projection.rglob("*") if path.is_file()]
    assert not list((tmp_path / ".brigade" / "memory-vault").glob("*.json"))


def test_project_vault_writes_a_conflict_copy_without_clobbering_existing_vault_data(
    tmp_path: Path, vault: Path, capsys
) -> None:
    _write_card(tmp_path, "alpha", title="Alpha", tags=["operations"], body="Canonical note.")
    existing = vault / "Brigade Memory" / "Cards" / "Alpha.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("operator-owned note\n", encoding="utf-8")

    receipt = _project(tmp_path, vault, capsys)

    assert "untracked:Cards/Alpha.md" in receipt["drift"]
    assert existing.read_text(encoding="utf-8") == "operator-owned note\n"
    assert list(existing.parent.glob("Alpha.conflict-*.md"))


def test_project_vault_preserves_directory_and_symlink_collisions(tmp_path: Path, vault: Path, capsys) -> None:
    _write_card(tmp_path, "alpha", title="Alpha", tags=["operations"], body="Canonical alpha.")
    _write_card(tmp_path, "beta", title="Beta", tags=["operations"], body="Canonical beta.")
    root = vault / "Brigade Memory" / "Cards"
    alpha = root / "Alpha.md"
    beta = root / "Beta.md"
    alpha.mkdir(parents=True)
    operator_note = tmp_path / "operator-note.md"
    operator_note.write_text("operator-owned note\n", encoding="utf-8")
    beta.symlink_to(operator_note)

    receipt = _project(tmp_path, vault, capsys)

    assert "untracked:Cards/Alpha.md" in receipt["drift"]
    assert "untracked:Cards/Beta.md" in receipt["drift"]
    assert alpha.is_dir()
    assert beta.is_symlink()
    assert list(root.glob("Alpha.conflict-*.md"))
    assert list(root.glob("Beta.conflict-*.md"))


def test_project_vault_keeps_mocs_with_colliding_sanitized_names(tmp_path: Path, vault: Path, capsys) -> None:
    _write_card(tmp_path, "alpha", title="Alpha", tags=["shared"], body="Alpha.", category="ops/a")
    _write_card(tmp_path, "beta", title="Beta", tags=["shared"], body="Beta.", category="ops:a")

    _project(tmp_path, vault, capsys)

    maps = list((vault / "Brigade Memory" / "Maps" / "Categories").glob("ops-a*.md"))
    assert len(maps) == 2
    assert sorted(path.read_text(encoding="utf-8").count("[[") for path in maps) == [1, 1]


def test_project_vault_escapes_wikilink_delimiters_in_titles(tmp_path: Path, vault: Path, capsys) -> None:
    _write_card(tmp_path, "alpha", title="Alpha]]|Map", tags=["shared"], body="Alpha.")
    _write_card(tmp_path, "beta", title="Beta", tags=["shared"], body="Beta.")

    _project(tmp_path, vault, capsys)

    root = vault / "Brigade Memory" / "Cards"
    assert (root / "Alpha-Map.md").is_file()
    beta = (root / "Beta.md").read_text(encoding="utf-8")
    assert "[[Cards/Alpha-Map|Alpha\\]\\]\\|Map]]" in beta


def test_project_vault_skips_a_manually_edited_conflict_copy_when_linking(tmp_path: Path, vault: Path, capsys) -> None:
    _write_card(tmp_path, "alpha", title="Alpha", tags=["operations"], body="Canonical alpha.")
    _write_card(tmp_path, "beta", title="Beta", tags=["operations"], body="Canonical beta.")
    root = vault / "Brigade Memory" / "Cards"
    root.mkdir(parents=True)
    (root / "Alpha.md").write_text("operator-owned base\n", encoding="utf-8")
    (root / "Alpha.conflict-ea62791a.md").write_text("operator-owned conflict\n", encoding="utf-8")

    _project(tmp_path, vault, capsys)

    beta = (root / "Beta.md").read_text(encoding="utf-8")
    assert "[[Cards/Alpha.conflict-ea62791a-2|Alpha]]" in beta
    assert (root / "Alpha.conflict-ea62791a.md").read_text(encoding="utf-8") == "operator-owned conflict\n"


def test_project_vault_titles_a_note_from_its_leading_heading(tmp_path: Path, vault: Path, capsys) -> None:
    """Cards without a frontmatter title fall back to their slug, which reads badly in a vault."""
    path = tmp_path / "memory" / "cards" / "rocinante-gateway-beta-worktree-steering.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nid: card-00000000-0000-4000-8000-00000000000c\ncategory: operations\ntags: []\n---\n"
        "# Rocinante gateway from a worktree\n\nSteer the beta from a worktree.\n",
        encoding="utf-8",
    )

    _project(tmp_path, vault, capsys)

    note = vault / "Brigade Memory" / "Cards" / "Rocinante gateway from a worktree.md"
    assert note.is_file()
    text = note.read_text(encoding="utf-8")
    assert 'title: "Rocinante gateway from a worktree"' in text
    assert text.count("# Rocinante gateway from a worktree") == 1
    # Provenance still points at the slug file the card actually lives in.
    assert "canonical_path: " in text and "rocinante-gateway-beta-worktree-steering.md" in text


def test_project_vault_keeps_an_explicit_frontmatter_title_over_the_body_heading(
    tmp_path: Path, vault: Path, capsys
) -> None:
    """An explicit title is operator intent; a differing body heading stays part of the body."""
    _write_card(
        tmp_path,
        "alpha",
        title="Solomon jobs and profile cheatsheet",
        tags=["shared"],
        body="# Targeted resume variants\n\nPick the lane.",
    )

    _project(tmp_path, vault, capsys)

    text = (vault / "Brigade Memory" / "Cards" / "Solomon jobs and profile cheatsheet.md").read_text(encoding="utf-8")
    assert "# Solomon jobs and profile cheatsheet" in text
    assert "# Targeted resume variants" in text


def test_project_vault_caps_related_links_and_canvas_edges(tmp_path: Path, vault: Path, capsys) -> None:
    """Same-category cards used to form a complete graph, so 1,254 notes produced 83,586 edges."""
    _write_cards(tmp_path, 30, category="workflow", tags=["shared"])

    _project(tmp_path, vault, capsys)

    projection = vault / "Brigade Memory"
    for note in (projection / "Cards").glob("Bulk *.md"):
        assert note.read_text(encoding="utf-8").count("[[") <= obsidian_vault.MAX_RELATED
    canvas = json.loads((projection / "Brigade Memory.canvas").read_text(encoding="utf-8"))
    card_edges = [edge for edge in canvas["edges"] if not str(edge["id"]).startswith("topology:")]
    assert card_edges
    assert len(card_edges) <= 30 * obsidian_vault.MAX_RELATED
    assert len(card_edges) < 30 * 29 // 2  # not the complete graph the cap replaced


def test_project_vault_omits_a_map_that_covers_every_note(tmp_path: Path, vault: Path, capsys) -> None:
    """A map listing every note groups nothing; the harness map was one 1,254-entry file."""
    _write_cards(tmp_path, 3, category="workflow", tags=["shared"])

    _project(tmp_path, vault, capsys)

    projection = vault / "Brigade Memory"
    assert not list((projection / "Maps" / "Harnesses").glob("*.md"))
    assert not list((projection / "Maps" / "Categories").glob("*.md"))


def test_project_vault_lays_out_canvas_notes_in_a_grid(tmp_path: Path, vault: Path, capsys) -> None:
    """A single row put 1,254 nodes across roughly 345,000 pixels of canvas."""
    _write_cards(tmp_path, 30, category="workflow", tags=["shared"])

    _project(tmp_path, vault, capsys)

    canvas = json.loads((vault / "Brigade Memory" / "Brigade Memory.canvas").read_text(encoding="utf-8"))
    card_nodes = [node for node in canvas["nodes"] if str(node["id"]).startswith("card-")]
    assert len(card_nodes) == 30
    assert len({node["y"] for node in card_nodes}) > 1
    assert max(node["x"] for node in card_nodes) < 30 * 260


def test_project_vault_does_not_reuse_an_unfinished_transaction_journal(
    tmp_path: Path, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_card(tmp_path, "alpha", title="Alpha", tags=["operations"], body="Canonical note.")
    real_execute = obsidian_vault.kernel.execute
    operation_ids: list[str] = []

    def crash_before_first_write(plan, *, target):
        operation_ids.append(plan.operation_id)
        return real_execute(plan, target=target, inject=kernel.FailureInjector(crash_after=0))

    monkeypatch.setattr(obsidian_vault.kernel, "execute", crash_before_first_write)
    first = obsidian_vault._project_payload(target=tmp_path, vault=vault)
    second = obsidian_vault._project_payload(target=tmp_path, vault=vault)

    assert first["receipt"]["terminal_state"] == "recovery-required"
    assert second["receipt"]["terminal_state"] == "failed"
    assert len(operation_ids) == 2
    assert operation_ids[0] != operation_ids[1]
    assert kernel.operation_dir(tmp_path, operation_ids[0]).is_dir()
    assert not kernel.operation_dir(tmp_path, operation_ids[1]).exists()


def _write_ref_card(
    target: Path,
    name: str,
    uuid_suffix: str,
    *,
    title: str,
    category: str,
    refs: list[str],
    extra_frontmatter: list[str] | None = None,
) -> None:
    """A card with explicit refs and no shared tags, so only refs can link it."""
    path = target / "memory" / "cards" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: card-00000000-0000-4000-8000-{uuid_suffix}",
                f"title: {title}",
                f"category: {category}",
                "tags: []",
                "fresh_until: 2099-01-01",
                "last_reviewed: 2026-08-14",
                "source_harness: codex",
                f"refs: {json.dumps(refs)}",
                *(extra_frontmatter or []),
                "---",
                f"{title} body.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_project_vault_resolves_uppercase_card_id_references(tmp_path: Path, vault: Path, capsys) -> None:
    # Regression: an un-normalized CARD-... ref validated but never resolved (#934 review).
    _write_ref_card(
        tmp_path,
        "alpha",
        "00000000000a",
        title="Alpha",
        category="ops-a",
        refs=["CARD-00000000-0000-4000-8000-00000000000B"],
    )
    _write_ref_card(tmp_path, "beta", "00000000000b", title="Beta", category="ops-b", refs=[])

    _project(tmp_path, vault, capsys)

    text = (vault / "Brigade Memory" / "Cards" / "Alpha.md").read_text(encoding="utf-8")
    assert "[[Cards/Beta|Beta]]" in text


def test_project_vault_resolves_bare_legacy_alias_references(tmp_path: Path, vault: Path, capsys) -> None:
    # Regression: bare stem/topic refs were dropped before reaching the alias index (#931).
    _write_ref_card(tmp_path, "alpha", "00000000000a", title="Alpha", category="ops-a", refs=["beta"])
    _write_ref_card(
        tmp_path,
        "beta",
        "00000000000b",
        title="Beta",
        category="ops-b",
        refs=["legacy-rollout-topic"],
    )
    _write_ref_card(
        tmp_path,
        "gamma",
        "00000000000c",
        title="Gamma",
        category="ops-c",
        refs=[],
        extra_frontmatter=["topic: legacy-rollout-topic"],
    )

    _project(tmp_path, vault, capsys)

    cards = vault / "Brigade Memory" / "Cards"
    assert "[[Cards/Beta|Beta]]" in (cards / "Alpha.md").read_text(encoding="utf-8")
    beta = (cards / "Beta.md").read_text(encoding="utf-8")
    assert "[[Cards/Gamma|Gamma]]" in beta


def test_project_vault_neutralizes_colliding_dual_read_keys(tmp_path: Path, vault: Path, capsys) -> None:
    # Regression: an alias that equals another record's canonical path must not silently
    # overwrite the index and mis-link (#867 deterministic collision detection).
    _write_ref_card(
        tmp_path,
        "alpha",
        "00000000000a",
        title="Alpha",
        category="ops-a",
        refs=[],
        extra_frontmatter=["topic: memory/cards/beta.md"],
    )
    _write_ref_card(tmp_path, "beta", "00000000000b", title="Beta", category="ops-b", refs=[])
    _write_ref_card(tmp_path, "gamma", "00000000000c", title="Gamma", category="ops-c", refs=["memory/cards/beta.md"])

    _project(tmp_path, vault, capsys)

    gamma = (vault / "Brigade Memory" / "Cards" / "Gamma.md").read_text(encoding="utf-8")
    assert "[[" not in gamma


def test_project_vault_emits_alias_frontmatter_for_rename_resolution(tmp_path: Path, vault: Path, capsys) -> None:
    # aliases: frontmatter lets Obsidian resolve [[old-stem]] links across renames (#931).
    _write_ref_card(
        tmp_path,
        "alpha",
        "00000000000a",
        title="Alpha",
        category="ops-a",
        refs=[],
        extra_frontmatter=["topic: legacy-rollout-topic"],
    )

    _project(tmp_path, vault, capsys)

    text = (vault / "Brigade Memory" / "Cards" / "Alpha.md").read_text(encoding="utf-8")
    assert "aliases:" in text
    assert "  - memory/cards/alpha.md" in text
    assert "  - alpha" in text
    assert "  - legacy-rollout-topic" in text
    assert "  - card-00000000-0000-4000-8000-00000000000a" not in text
