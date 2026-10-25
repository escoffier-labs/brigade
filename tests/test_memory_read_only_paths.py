"""read_only_paths keeps operator-owned cards out of memory care and proposals."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from brigade import memory_cmd
from tests.test_memory_proposal_capabilities import SAFE_DIRECTORY_CAPABILITY
from tests.test_memory_proposals import LOSER, SURVIVOR, accepted, apply, create, pair_target

POLICY = "memory/cards/policy/rules.md"


def _write_config(target: Path, *patterns: str) -> None:
    path = memory_cmd.config_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"read_only_paths = {json.dumps(list(patterns))}\n", encoding="utf-8")


def _card(path: Path, topic: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\ntopic: {topic}\nconfidence: high\nevidence: ["README.md"]\n---\nBody.\n',
        encoding="utf-8",
    )


def test_config_defaults_to_empty_and_parses_patterns(tmp_path):
    assert memory_cmd.MemoryCareConfig().read_only_paths == ()
    assert memory_cmd._config_or_default(tmp_path).read_only_paths == ()
    _write_config(tmp_path, "memory/cards/policy/**", " memory/cards/*.lock.md ")
    config = memory_cmd.load_config(tmp_path)
    assert config is not None
    assert config.read_only_paths == ("memory/cards/policy/**", "memory/cards/*.lock.md")


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/*.md",
        "../outside/*.md",
        "memory/../x",
        r"\memory\cards\*.md",
        r"C:\memory\cards\*.md",
        "C:memory/cards/*.md",
        r"\\server\share\*.md",
        r"memory\..\outside\*.md",
    ],
)
def test_config_rejects_unsafe_patterns(tmp_path, bad):
    _write_config(tmp_path, bad)
    with pytest.raises(ValueError, match="read_only_paths"):
        memory_cmd.load_config(tmp_path)


def test_config_rejects_non_list(tmp_path):
    memory_cmd.config_path(tmp_path).parent.mkdir(parents=True)
    memory_cmd.config_path(tmp_path).write_text('read_only_paths = "memory/cards/*.md"\n')
    with pytest.raises(ValueError, match="read_only_paths must be a list of strings"):
        memory_cmd.load_config(tmp_path)


def test_init_template_round_trips_read_only_paths(tmp_path):
    assert memory_cmd.init(target=tmp_path) == 0
    config = memory_cmd.load_config(tmp_path)
    assert config is not None and config.read_only_paths == ()
    assert "read_only_paths = []" in memory_cmd.config_path(tmp_path).read_text()


@pytest.mark.parametrize(
    ("path", "patterns", "expected"),
    [
        ("memory/cards/policy/rules.md", ("memory/cards/policy/**",), "memory/cards/policy/**"),
        ("memory/cards/policy/deep/rules.md", ("memory/cards/policy/**",), "memory/cards/policy/**"),
        ("memory/cards/policy/rules.md", ("memory/cards/policy",), "memory/cards/policy"),
        ("memory/cards/a.lock.md", ("memory/cards/*.lock.md",), "memory/cards/*.lock.md"),
        ("memory/cards/sub/a.lock.md", ("memory/cards/*.lock.md",), None),
        ("memory/cards/sub/a.lock.md", ("**/*.lock.md",), "**/*.lock.md"),
        ("memory/cards/a.md", ("memory/cards/?.md",), "memory/cards/?.md"),
        ("memory/cards/other.md", ("memory/cards/policy/**", "nothing"), None),
        ("memory/cards/other.md", (), None),
        ("memory\\cards\\policy\\x.md", ("memory/cards/policy/**",), "memory/cards/policy/**"),
        ("memory/cards/policy/rules.md", ("memory/cards/policy/",), "memory/cards/policy/"),
        ("memory/cards/policy/rules.md", ("./memory/cards/policy",), "./memory/cards/policy"),
        ("./memory/cards/policy/rules.md", ("memory/cards/policy/**",), "memory/cards/policy/**"),
        ("memory/cards/policy/rules.md", ("./memory/cards/policy/",), "./memory/cards/policy/"),
    ],
)
def test_read_only_pattern_matching(path, patterns, expected):
    assert memory_cmd.read_only_pattern(path, patterns) == expected


def test_scan_skips_read_only_cards(tmp_path):
    _card(tmp_path / POLICY, "policy")
    _card(tmp_path / "memory/cards/normal.md", "normal")
    unprotected = memory_cmd._scan_payload(tmp_path, memory_cmd.MemoryCareConfig())
    assert any(issue["file"] == POLICY for issue in unprotected["issues"])
    config = memory_cmd.MemoryCareConfig(read_only_paths=("memory/cards/policy/**",))
    payload = memory_cmd._scan_payload(tmp_path, config)
    assert payload["issues"], "non-protected cards must still be scanned"
    assert all(issue["file"] != POLICY for issue in payload["issues"])
    assert all(row["file"] != POLICY for row in payload["cards"])


def test_search_still_finds_read_only_cards(tmp_path):
    _card(tmp_path / POLICY, "policy")
    _write_config(tmp_path, "memory/cards/policy/**")
    payload = memory_cmd.search_cards_payload(tmp_path, "policy")
    assert [match["path"] for match in payload["matches"]] == [POLICY]


def test_scan_reports_duplicate_id_on_the_normal_card(tmp_path):
    _card(tmp_path / POLICY, "shared")
    _card(tmp_path / "memory/cards/normal.md", "shared")
    config = memory_cmd.MemoryCareConfig(read_only_paths=("memory/cards/policy/**",))
    payload = memory_cmd._scan_payload(tmp_path, config)
    contradictory = [issue for issue in payload["issues"] if issue["issue_type"] == "contradictory"]
    assert {issue["file"] for issue in contradictory} == {"memory/cards/normal.md"}
    assert all(issue["file"] != POLICY for issue in payload["issues"])
    assert "shared" in payload["identity_collisions"]
    assert payload["card_count"] == 2
    assert payload["read_only_card_count"] == 1


def test_scan_reports_zero_read_only_cards_by_default(tmp_path):
    _card(tmp_path / POLICY, "policy")
    payload = memory_cmd._scan_payload(tmp_path, memory_cmd.MemoryCareConfig())
    assert payload["card_count"] == 1
    assert payload["read_only_card_count"] == 0


def test_plan_skips_read_only_cards_from_a_stale_scan(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    _card(tmp_path / POLICY, "policy")
    _card(tmp_path / "memory/cards/normal.md", "normal")
    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    _write_config(tmp_path, "memory/cards/policy/**")
    assert memory_cmd.plan_fixes(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    files = {item["card_file"] for item in payload["items"]}
    assert POLICY not in files
    assert "memory/cards/normal.md" in files


def test_backfill_skips_read_only_cards(tmp_path, capsys):
    _card(tmp_path / POLICY, "policy")
    _card(tmp_path / "memory/cards/normal.md", "normal")
    before = (tmp_path / POLICY).read_text()
    _write_config(tmp_path, "memory/cards/policy/**")
    assert memory_cmd.backfill(target=tmp_path, apply=True, json_output=True) == 0
    capsys.readouterr()
    assert (tmp_path / POLICY).read_text() == before
    assert "last_reviewed" in (tmp_path / "memory/cards/normal.md").read_text()


def test_backfill_refuses_alias_collision_with_a_read_only_card(tmp_path, capsys):
    _card(tmp_path / POLICY, "shared")
    _card(tmp_path / "memory/cards/normal.md", "shared")
    before = (tmp_path / "memory/cards/normal.md").read_text()
    _write_config(tmp_path, "memory/cards/policy/**")
    assert memory_cmd.backfill(target=tmp_path, apply=True, json_output=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert "identity alias collisions" in payload["error"]
    assert (tmp_path / "memory/cards/normal.md").read_text() == before


def test_backfill_never_mints_an_id_a_read_only_card_already_claims(tmp_path, capsys):
    from brigade.card_identity import mint_stable_card_id

    normal = "memory/cards/normal.md"
    taken = mint_stable_card_id(f"path:{normal}")
    _card(tmp_path / POLICY, "policy")
    policy = tmp_path / POLICY
    policy.write_text(policy.read_text().replace("topic: policy", f"id: {taken}\ntopic: policy"))
    _card(tmp_path / normal, "normal")
    _write_config(tmp_path, "memory/cards/policy/**")
    assert memory_cmd.backfill(target=tmp_path, apply=False, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    minted = {row["path"]: row["new_id"] for row in payload["identity_receipt"]["mappings"]}
    assert POLICY not in minted
    assert minted[normal] and minted[normal] != taken


def test_status_and_doctor_list_active_patterns(tmp_path, capsys):
    _write_config(tmp_path, "memory/cards/policy/**", "memory/cards/*.lock.md")
    assert memory_cmd.status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["read_only_paths"] == ["memory/cards/policy/**", "memory/cards/*.lock.md"]
    assert memory_cmd.status(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "read_only_paths: memory/cards/policy/**, memory/cards/*.lock.md" in out
    assert memory_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "memory_read_only_paths" in out and "memory/cards/policy/**" in out


def test_status_reports_no_patterns_by_default(tmp_path, capsys):
    assert memory_cmd.status(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["read_only_paths"] == []


pytestmark_proposals = pytest.mark.skipif(
    not SAFE_DIRECTORY_CAPABILITY, reason="safe directory descriptors unavailable"
)


@pytest.fixture
def api():
    import importlib

    return importlib.import_module("brigade.memory_proposals")


@pytestmark_proposals
def test_create_refuses_read_only_card_naming_pattern(api, tmp_path):
    owner = (tmp_path, pair_target(tmp_path))
    _write_config(tmp_path, "memory/cards/previous.md")
    with pytest.raises(api.ProposalError, match=r"read_only_paths.*memory/cards/previous\.md"):
        create(api, owner)
    assert not (tmp_path / ".brigade/memory/proposals").exists() or not list(
        (tmp_path / ".brigade/memory/proposals").iterdir()
    )


@pytestmark_proposals
def test_apply_refuses_when_card_became_read_only_and_writes_nothing(api, tmp_path):
    owner = (tmp_path, pair_target(tmp_path))
    proposal = accepted(api, owner)
    snapshot = {name: (tmp_path / name).read_bytes() for name in (SURVIVOR, LOSER, "MEMORY.md")}
    _write_config(tmp_path, "memory/cards/*.md")
    with pytest.raises(api.ProposalError, match=r"read_only_paths pattern memory/cards/\*\.md") as caught:
        apply(api, owner, proposal)
    assert caught.value.exit_code == 1
    assert {name: (tmp_path / name).read_bytes() for name in snapshot} == snapshot


@pytestmark_proposals
def test_proposals_unaffected_without_matching_patterns(api, tmp_path):
    owner = (tmp_path, pair_target(tmp_path))
    _write_config(tmp_path, "memory/cards/policy/**")
    proposal = accepted(api, owner)
    assert apply(api, owner, proposal)["status"] == "committed"


@pytestmark_proposals
def test_create_refuses_when_an_index_mutation_is_read_only(api, tmp_path):
    owner = (tmp_path, pair_target(tmp_path))
    _write_config(tmp_path, "MEMORY.md")
    with pytest.raises(api.ProposalError, match=r"MEMORY\.md is read-only.*read_only_paths pattern MEMORY\.md"):
        create(api, owner)


@pytestmark_proposals
def test_reapply_returns_the_receipt_after_a_pattern_is_added(api, tmp_path):
    owner = (tmp_path, pair_target(tmp_path))
    proposal = accepted(api, owner)
    first = apply(api, owner, proposal)
    assert first["status"] == "committed"
    _write_config(tmp_path, "memory/cards/*.md")
    replay = apply(api, owner, proposal)
    assert replay["status"] == "already-applied"
    assert replay["operation_id"] == first["operation_id"]


@pytestmark_proposals
def test_adding_an_unrelated_pattern_while_pending_is_config_drift(api, tmp_path):
    owner = (tmp_path, pair_target(tmp_path))
    proposal = accepted(api, owner)
    snapshot = {name: (tmp_path / name).read_bytes() for name in (SURVIVOR, LOSER, "MEMORY.md")}
    _write_config(tmp_path, "memory/cards/policy/**")
    with pytest.raises(api.ProposalError, match="config drift"):
        apply(api, owner, proposal)
    assert {name: (tmp_path / name).read_bytes() for name in snapshot} == snapshot
