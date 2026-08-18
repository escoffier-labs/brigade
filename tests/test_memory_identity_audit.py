"""Tests for the read-only projected-memory identity audit."""

from __future__ import annotations

import json

from brigade import cli, memory_identity_audit


def _workspace(tmp_path, name: str, namespace: str):
    workspace = tmp_path / name
    cards = workspace / "memory" / "cards"
    cards.mkdir(parents=True)
    (workspace / "memory" / "NAMESPACE").write_text(namespace + "\n")
    return workspace, cards


def _card(cards, name: str, frontmatter: str) -> None:
    (cards / name).write_text(f"---\n{frontmatter}\n---\n\n# Card\n")


def test_audit_reports_coverage_collisions_legacy_keys_and_deterministic_aliases(tmp_path):
    first, first_cards = _workspace(tmp_path, "first", "memory-11111111-1111-4111-8111-111111111111")
    second, second_cards = _workspace(tmp_path, "second", "memory-22222222-2222-4222-8222-222222222222")
    shared_id = "card-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _card(first_cards, "explicit.md", f"id: {shared_id}\ntopic: release")
    _card(first_cards, "fallback.md", "topic: fallback-topic")
    _card(first_cards, "malformed.md", "id: path:memory/cards/fallback.md")
    _card(second_cards, "same.md", f"id: {shared_id}")

    audit = memory_identity_audit.audit_workspaces([first, second])

    assert audit["summary"] == {
        "workspaces": 2,
        "cards": 4,
        "explicit_ids": 3,
        "path_fallbacks": 1,
        "malformed_ids": 1,
        "duplicate_ids_within_namespace": 1,
        "duplicate_ids_across_namespaces": 1,
        "legacy_keys": 6,
        "collisions": 2,
    }
    assert audit["alias_readiness"]["ready"] is False
    assert audit["alias_readiness"]["blocking_reasons"] == [
        "malformed_ids",
        "duplicate_ids_within_namespace",
        "duplicate_ids_across_namespaces",
    ]
    assert audit["mappings"] == [
        {
            "namespace": "memory-11111111-1111-4111-8111-111111111111",
            "old_id": "path:memory/cards/fallback.md",
            "new_id": "card-a7bee672-2ad2-590a-ad2b-51992afbeddc",
            "path": "memory/cards/fallback.md",
        }
    ]
    assert {item["kind"] for item in audit["legacy_keys"]} == {"filename_stem", "topic"}
    malformed = audit["malformed_ids"]
    assert malformed == [
        {
            "workspace": "first",
            "namespace": "memory-11111111-1111-4111-8111-111111111111",
            "path": "memory/cards/malformed.md",
            "detail": "explicit ID does not meet the future card-<uuid> policy",
        }
    ]


def test_audit_uses_the_engine_id_precedence_and_coercion_rules(tmp_path):
    workspace, cards = _workspace(tmp_path, "one", "memory-99999999-9999-4999-8999-999999999999")
    _card(cards, "precedence.md", "id: primary\ncard_id: ignored")
    _card(cards, "bool.md", "id: true")

    audit = memory_identity_audit.audit_workspaces([workspace])

    assert [card["identity"] for card in audit["cards"]] == ["true", "primary"]
    assert audit["summary"]["explicit_ids"] == 2


def test_audit_reports_duplicates_without_a_namespace_file(tmp_path):
    workspace = tmp_path / "one"
    cards = workspace / "memory" / "cards"
    cards.mkdir(parents=True)
    assert not (workspace / "memory" / "NAMESPACE").exists()
    _card(cards, "a.md", "id: card-99999999-9999-4999-8999-999999999999")
    _card(cards, "b.md", "id: card-99999999-9999-4999-8999-999999999999")

    audit = memory_identity_audit.audit_workspaces([workspace])

    assert audit["summary"]["duplicate_ids_within_namespace"] == 1
    assert audit["collisions"] == [
        {
            "scope": "namespace",
            "namespace": "",
            "id": "card-99999999-9999-4999-8999-999999999999",
            "paths": ["memory/cards/a.md", "memory/cards/b.md"],
        }
    ]


def test_audit_reports_duplicates_inside_a_namespace_without_writing_cards(tmp_path):
    workspace, cards = _workspace(tmp_path, "one", "memory-33333333-3333-4333-8333-333333333333")
    _card(cards, "a.md", "id: card-bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    _card(cards, "b.md", "card_id: card-bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    before = {path.name: path.read_text() for path in cards.glob("*.md")}

    audit = memory_identity_audit.audit_workspaces([workspace])

    assert audit["summary"]["duplicate_ids_within_namespace"] == 1
    assert audit["collisions"] == [
        {
            "scope": "namespace",
            "namespace": "memory-33333333-3333-4333-8333-333333333333",
            "id": "card-bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "paths": ["memory/cards/a.md", "memory/cards/b.md"],
        }
    ]
    assert {path.name: path.read_text() for path in cards.glob("*.md")} == before


def test_evidence_memory_audit_cli_is_read_only_and_json_safe(tmp_path, capsys):
    workspace, cards = _workspace(tmp_path, "one", "memory-44444444-4444-4444-8444-444444444444")
    _card(cards, "card.md", "id: card-cccccccc-cccc-4ccc-8ccc-cccccccccccc\ntopic: safe")
    before = (cards / "card.md").read_text()

    assert cli.main(["evidence", "memory", "audit", str(workspace), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "brigade.memory-identity-audit.v1"
    assert payload["summary"]["explicit_ids"] == 1
    assert "# Card" not in json.dumps(payload)
    assert (cards / "card.md").read_text() == before


def test_audit_reports_persisted_care_search_and_eval_legacy_keys(tmp_path):
    workspace, cards = _workspace(tmp_path, "one", "memory-55555555-5555-4555-8555-555555555555")
    _card(cards, "legacy-card.md", "topic: legacy-topic")
    (workspace / ".brigade" / "memory").mkdir(parents=True)
    (workspace / ".brigade" / "memory" / "search-log.jsonl").write_text('{"card_ids":["legacy-card"]}\n')
    care = workspace / ".brigade" / "memory-care" / "decay"
    care.mkdir(parents=True)
    (care / "scan-latest.json").write_text('{"cards":[{"card_id":"legacy-topic"}]}')
    evaluation = workspace / "evals" / "memory-retrieval"
    evaluation.mkdir(parents=True)
    (evaluation / "queries.json").write_text('{"queries":[{"gold":["legacy-card"]}]}')

    audit = memory_identity_audit.audit_workspaces([workspace])

    kinds = {(item["consumer"], item["kind"], item["key"]) for item in audit["legacy_keys"]}
    assert ("care", "care_card_id", "legacy-topic") in kinds
    assert ("search", "search_log_card_id", "legacy-card") in kinds
    assert ("eval", "eval_gold_card_id", "legacy-card") in kinds
