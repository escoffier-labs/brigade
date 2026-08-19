"""Acceptance tests for the operator-vault read path (#943)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from brigade import cli, memory_vault


def _write_vault_config(
    target: Path,
    vault: Path,
    roots: list[tuple[str, str, bool]] | None = None,
    *,
    schema_version: int = 1,
) -> None:
    path = target / ".brigade" / "vault.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"schema_version = {schema_version}",
        f"vault = {json.dumps(str(vault))}",
        "",
    ]
    for scope, rel, optional in roots or [("notes", "Notes", False)]:
        lines.extend(
            [
                "[[roots]]",
                f"scope = {json.dumps(scope)}",
                f"path = {json.dumps(rel)}",
                f"optional = {'true' if optional else 'false'}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_note(
    vault: Path,
    relative: str,
    *,
    title: str | None = None,
    body: str = "",
    tags: list[str] | None = None,
    canonical_id: str | None = None,
    aliases: list[str] | None = None,
    archived: bool | None = None,
    extra_frontmatter: list[str] | None = None,
) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if title is not None:
        lines.append(f"title: {title}")
    if canonical_id is not None:
        lines.append(f"canonical_id: {canonical_id}")
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {tag}" for tag in tags)
    if aliases:
        lines.append("aliases:")
        lines.extend(f"  - {alias}" for alias in aliases)
    if archived is not None:
        lines.append(f"archived: {'true' if archived else 'false'}")
    if extra_frontmatter:
        lines.extend(extra_frontmatter)
    lines.append("---")
    lines.append("")
    if title is None and body.startswith("# "):
        lines.append(body.rstrip())
    elif title is not None:
        lines.append(f"# {title}")
        if body:
            lines.append("")
            lines.append(body.rstrip())
    else:
        lines.append(body.rstrip())
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "repo"
    vault = tmp_path / "vault"
    target.mkdir()
    (vault / "Notes").mkdir(parents=True)
    (vault / "Archive").mkdir(parents=True)
    _write_vault_config(target, vault, [("notes", "Notes", False), ("archive", "Archive", False)])
    return target, vault


def test_doctor_is_inert_without_config(tmp_path: Path, capsys) -> None:
    target = tmp_path / "repo"
    target.mkdir()
    assert cli.main(["memory", "vault-doctor", "--target", str(target), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["vault"] == "redacted:operator-vault"
    names = {check["name"]: check for check in payload["checks"]}
    assert names["vault_config"]["status"] == "warn"


def test_search_without_config_is_an_error(tmp_path: Path, capsys) -> None:
    target = tmp_path / "repo"
    target.mkdir()
    assert cli.main(["memory", "vault-search", "rotation", "--target", str(target), "--json"]) == 2
    assert "not configured" in capsys.readouterr().err


def test_future_schema_version_fails_closed(tmp_path: Path, capsys) -> None:
    target = tmp_path / "repo"
    vault = tmp_path / "vault"
    target.mkdir()
    vault.mkdir()
    _write_vault_config(target, vault, schema_version=2)
    assert cli.main(["memory", "vault-index", "--target", str(target), "--json"]) == 2
    assert "unsupported vault schema_version" in capsys.readouterr().err


def test_unknown_scope_is_an_error_not_an_empty_result(workspace, capsys) -> None:
    target, vault = workspace
    _write_note(vault, "Notes/rotation.md", title="Rotation Policy", body="how keys rotate")
    assert cli.main(["memory", "vault-search", "rotation", "--scope", "secrets", "--target", str(target)]) == 2
    assert "unknown scope" in capsys.readouterr().err


def test_scope_must_be_a_name_not_a_path(tmp_path: Path, capsys) -> None:
    target = tmp_path / "repo"
    vault = tmp_path / "vault"
    target.mkdir()
    (vault / "Notes").mkdir(parents=True)
    _write_vault_config(target, vault, [("Notes/sub", "Notes", False)])
    assert cli.main(["memory", "vault-index", "--target", str(target)]) == 2
    assert "must be a name, not a path" in capsys.readouterr().err


def test_root_path_cannot_escape_the_vault(tmp_path: Path, capsys) -> None:
    target = tmp_path / "repo"
    vault = tmp_path / "vault"
    target.mkdir()
    vault.mkdir()
    _write_vault_config(target, vault, [("escape", "../outside", False)])
    assert cli.main(["memory", "vault-index", "--target", str(target)]) == 2
    assert "vault-relative" in capsys.readouterr().err


def test_limit_out_of_range_is_an_explicit_error(workspace, capsys) -> None:
    target, _vault = workspace
    with pytest.raises(SystemExit) as exc:
        cli.main(["memory", "vault-search", "rotation", "--target", str(target), "--limit", "0"])
    assert exc.value.code == 2
    assert "--limit must be between 1 and 100" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        cli.main(["memory", "vault-search", "rotation", "--target", str(target), "--limit", "101"])
    assert exc.value.code == 2


def test_read_commands_do_not_accept_vault_flag(workspace) -> None:
    target, _vault = workspace
    for command in (
        ["memory", "vault-index", "--vault", "/tmp/fake-vault", "--target", str(target)],
        ["memory", "vault-search", "q", "--vault", "/tmp/fake-vault", "--target", str(target)],
        ["memory", "vault-show", "id", "--vault", "/tmp/fake-vault", "--target", str(target)],
        ["memory", "vault-doctor", "--vault", "/tmp/fake-vault", "--target", str(target)],
        [
            "memory",
            "vault-propose",
            "--title",
            "x",
            "--scope",
            "notes",
            "--vault",
            "/tmp/fake-vault",
            "--target",
            str(target),
        ],
    ):
        with pytest.raises(SystemExit) as exc:
            cli.main(command)
        assert exc.value.code == 2


def test_index_search_show_round_trip_and_result_contract(workspace, capsys) -> None:
    target, vault = workspace
    path = _write_note(
        vault,
        "Notes/rotation-policy.md",
        title="Rotation Policy",
        body="Rotate operator vault keys quarterly.",
        tags=["security", "vault"],
    )
    before = {item: item.stat().st_mtime_ns for item in vault.rglob("*") if item.is_file()}

    assert cli.main(["memory", "vault-index", "--target", str(target), "--json"]) == 0
    indexed = json.loads(capsys.readouterr().out)
    assert indexed["indexed"] == 1
    assert indexed["vault"] == "redacted:operator-vault"
    assert indexed["index_path"] == ".brigade/vault-index/index.json"

    index_file = target / ".brigade" / "vault-index" / "index.json"
    assert stat.S_IMODE(index_file.stat().st_mode) == 0o600
    assert "vault" not in json.loads(index_file.read_text()) or json.loads(index_file.read_text()).get("vault") in {
        None,
        "redacted:operator-vault",
    }
    assert str(vault) not in index_file.read_text()

    after = {item: item.stat().st_mtime_ns for item in vault.rglob("*") if item.is_file()}
    assert after == before

    assert cli.main(["memory", "vault-search", "rotation policy", "--target", str(target), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "brigade.vault-search.v1"
    assert payload["match_count"] == 1
    hit = payload["hits"][0]
    expected_fields = {
        "id",
        "title",
        "scope",
        "relative_path",
        "content_hash",
        "updated_at",
        "tags",
        "snippet",
        "trust",
        "score",
    }
    assert expected_fields <= set(hit)
    assert hit["title"] == "Rotation Policy"
    assert hit["scope"] == "notes"
    assert hit["relative_path"] == "Notes/rotation-policy.md"
    assert hit["content_hash"] == f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    assert hit["content_hash"].startswith("sha256:")
    assert hit["trust"] == "untrusted_vault_content"
    assert hit["score"] > 0
    assert "Rotate operator vault keys" in hit["snippet"]

    assert cli.main(["memory", "vault-show", hit["id"], "--target", str(target), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == hit["id"]
    assert shown["trust"] == "untrusted_vault_content"
    assert "quarterly" in shown["body"]
    assert shown["vault"] == "redacted:operator-vault"


def test_ranking_weights_match_the_settled_contract() -> None:
    note = {
        "title": "Rotation Policy",
        "tags": ["security/vault"],
        "relative_path": "Notes/rotation-policy.md",
        "body": "rotation happens twice: rotation",
    }
    query = "rotation policy"
    tokens = memory_vault._query_tokens(memory_vault._normalize(query))
    score = memory_vault.score_note(note, memory_vault._normalize(query), tokens)
    # exact title +1000; each title token +50; path segments include both
    # "rotation" and "policy" from the hyphenated stem (+10 each); body has
    # "rotation" twice.
    assert score == 1000 + 50 + 50 + 10 + 10 + 2


def test_title_token_and_tag_and_path_scores() -> None:
    note = {
        "title": "Operator Handbook",
        "tags": ["obsidian"],
        "relative_path": "Notes/handbook.md",
        "body": "nothing matching here",
    }
    query = "obsidian handbook"
    tokens = memory_vault._query_tokens(memory_vault._normalize(query))
    score = memory_vault.score_note(note, memory_vault._normalize(query), tokens)
    # no exact title; handbook title token +50; obsidian tag +30; handbook path +10
    assert score == 50 + 30 + 10


def test_snippet_is_capped_after_whitespace_collapse(workspace, capsys) -> None:
    target, vault = workspace
    body = "alpha " + ("word " * 200) + "tail"
    _write_note(vault, "Notes/long.md", title="Long Note", body=body)
    assert cli.main(["memory", "vault-search", "alpha", "--target", str(target), "--json"]) == 0
    hit = json.loads(capsys.readouterr().out)["hits"][0]
    assert len(hit["snippet"]) <= memory_vault.SNIPPET_CHARS
    assert "\n" not in hit["snippet"]


def test_output_runs_through_guard_redaction(workspace, capsys) -> None:
    target, vault = workspace
    _write_note(
        vault,
        "Notes/contact.md",
        title="Contact Rotation",
        # Built by concatenation so the repo's own secret scanner does not
        # match a contiguous bearer token in this fixture's source.
        body="Bearer " + "super-secret-token-value-" + "123456 about rotation.",
    )
    assert cli.main(["memory", "vault-search", "rotation", "--target", str(target), "--json"]) == 0
    hit = json.loads(capsys.readouterr().out)["hits"][0]
    assert "super-secret-token-value-" + "123456" not in hit["snippet"]
    assert "Bearer [redacted-secret]" in hit["snippet"]


def test_projected_notes_key_on_canonical_id(workspace, capsys) -> None:
    target, vault = workspace
    card_id = "card-00000000-0000-4000-8000-00000000000a"
    _write_note(
        vault,
        "Notes/Brigade Memory/Cards/Alpha.md",
        title="Alpha",
        body="Projected alpha body.",
        canonical_id=card_id,
        aliases=["memory/cards/alpha.md", "alpha"],
        extra_frontmatter=["generated: true"],
    )
    assert cli.main(["memory", "vault-search", "alpha", "--target", str(target), "--json"]) == 0
    hit = json.loads(capsys.readouterr().out)["hits"][0]
    assert hit["id"] == card_id

    assert cli.main(["memory", "vault-show", "alpha", "--target", str(target), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == card_id
    assert shown["relative_path"] == "Notes/Brigade Memory/Cards/Alpha.md"


def test_operator_notes_fall_back_to_path_identity(workspace, capsys) -> None:
    target, vault = workspace
    _write_note(vault, "Notes/meeting-notes.md", title="Standup", body="standup notes about rotation")
    assert cli.main(["memory", "vault-search", "standup", "--target", str(target), "--json"]) == 0
    hit = json.loads(capsys.readouterr().out)["hits"][0]
    assert hit["id"] == "Notes/meeting-notes.md"

    assert cli.main(["memory", "vault-show", "Notes/meeting-notes.md", "--target", str(target), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == "Notes/meeting-notes.md"


def test_archived_notes_are_excluded_unless_requested(workspace, capsys) -> None:
    target, vault = workspace
    _write_note(vault, "Notes/live.md", title="Live Rotation", body="current rotation")
    _write_note(vault, "Archive/old.md", title="Old Rotation", body="archived rotation")
    _write_note(vault, "Notes/flagged.md", title="Flagged Rotation", body="flagged", archived=True)

    assert cli.main(["memory", "vault-search", "rotation", "--target", str(target), "--json"]) == 0
    live = json.loads(capsys.readouterr().out)
    assert [hit["relative_path"] for hit in live["hits"]] == ["Notes/live.md"]

    assert (
        cli.main(["memory", "vault-search", "rotation", "--include-archived", "--target", str(target), "--json"]) == 0
    )
    all_hits = json.loads(capsys.readouterr().out)
    paths = {hit["relative_path"] for hit in all_hits["hits"]}
    assert paths == {"Notes/live.md", "Archive/old.md", "Notes/flagged.md"}


def test_removing_a_root_from_vault_toml_stops_serving_that_root(tmp_path: Path, capsys) -> None:
    """Revoking a root must invalidate the index; config is authoritative (#983)."""
    target = tmp_path / "repo"
    vault = tmp_path / "vault"
    target.mkdir()
    (vault / "notes").mkdir(parents=True)
    (vault / "other").mkdir(parents=True)
    _write_note(vault, "notes/alpha.md", title="Alpha Note", body="Alpha Note alpha token")
    _write_note(vault, "other/beta.md", title="Beta Note", body="Beta Note alpha token")
    _write_vault_config(target, vault, [("notes", "notes", False), ("other", "other", False)])

    assert cli.main(["memory", "vault-index", "--target", str(target), "--json"]) == 0
    indexed = json.loads(capsys.readouterr().out)
    assert indexed["indexed"] == 2
    assert {scope["scope"] for scope in indexed["scopes"]} == {"notes", "other"}

    assert cli.main(["memory", "vault-search", "alpha", "--target", str(target), "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["match_count"] == 2
    assert {hit["scope"] for hit in first["hits"]} == {"notes", "other"}

    _write_vault_config(target, vault, [("notes", "notes", False)])

    assert cli.main(["memory", "vault-doctor", "--target", str(target), "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    names = {check["name"]: check for check in doctor["checks"]}
    assert names["vault_index_stale"]["status"] == "warn"
    assert "allowlist" in names["vault_index_stale"]["detail"]
    assert names["vault_index_roots"]["status"] == "warn"
    assert "other" in names["vault_index_roots"]["detail"]

    assert cli.main(["memory", "vault-search", "alpha", "--scope", "other", "--target", str(target)]) == 2
    assert "unknown scope: other" in capsys.readouterr().err

    assert cli.main(["memory", "vault-search", "alpha", "--target", str(target), "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["match_count"] == 1
    assert [hit["scope"] for hit in second["hits"]] == ["notes"]
    assert [hit["relative_path"] for hit in second["hits"]] == ["notes/alpha.md"]


def test_vault_fingerprint_includes_sorted_roots(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    two_roots = memory_vault.VaultConfig(
        schema_version=1,
        vault=vault,
        roots=(
            memory_vault.VaultRoot(scope="other", path="other", optional=False),
            memory_vault.VaultRoot(scope="notes", path="notes", optional=False),
        ),
    )
    notes_only = memory_vault.VaultConfig(
        schema_version=1,
        vault=vault,
        roots=(memory_vault.VaultRoot(scope="notes", path="notes", optional=False),),
    )
    optional_flip = memory_vault.VaultConfig(
        schema_version=1,
        vault=vault,
        roots=(memory_vault.VaultRoot(scope="notes", path="notes", optional=True),),
    )
    path_change = memory_vault.VaultConfig(
        schema_version=1,
        vault=vault,
        roots=(memory_vault.VaultRoot(scope="notes", path="renamed", optional=False),),
    )
    same_as_two = memory_vault.VaultConfig(
        schema_version=1,
        vault=vault,
        roots=(
            memory_vault.VaultRoot(scope="notes", path="notes", optional=False),
            memory_vault.VaultRoot(scope="other", path="other", optional=False),
        ),
    )
    assert memory_vault._vault_fingerprint(two_roots) == memory_vault._vault_fingerprint(same_as_two)
    assert memory_vault._vault_fingerprint(two_roots) != memory_vault._vault_fingerprint(notes_only)
    assert memory_vault._vault_fingerprint(notes_only) != memory_vault._vault_fingerprint(optional_flip)
    assert memory_vault._vault_fingerprint(notes_only) != memory_vault._vault_fingerprint(path_change)


def test_does_not_follow_symlinks_out_of_the_vault(workspace, capsys) -> None:
    target, vault = workspace
    outside = target.parent / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text(
        "---\ntitle: Secret\n---\n\nOUTSIDEONLY leaked from outside the vault\n",
        encoding="utf-8",
    )
    _write_note(vault, "Notes/ok.md", title="Ok Rotation", body="safe rotation")
    _write_note(vault, "Notes/live.md", title="Live Note", body="live rotation inside the vault")
    (vault / "Notes" / "escape-dir").symlink_to(outside)
    (vault / "Notes" / "escape-file.md").symlink_to(outside / "secret.md")

    assert cli.main(["memory", "vault-index", "--target", str(target), "--json"]) == 0
    indexed = json.loads(capsys.readouterr().out)
    assert indexed["indexed"] == 2
    index_text = (target / ".brigade" / "vault-index" / "index.json").read_text(encoding="utf-8")
    assert "OUTSIDEONLY" not in index_text
    assert "escape-dir" not in index_text
    assert "escape-file.md" not in index_text

    assert (
        cli.main(
            [
                "memory",
                "vault-search",
                "OUTSIDEONLY",
                "--target",
                str(target),
                "--include-archived",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["hits"] == []
    assert payload["match_count"] == 0

    assert cli.main(["memory", "vault-search", "rotation", "--target", str(target), "--json"]) == 0
    hits = json.loads(capsys.readouterr().out)["hits"]
    assert {hit["relative_path"] for hit in hits} == {"Notes/live.md", "Notes/ok.md"}
    assert all("OUTSIDEONLY" not in json.dumps(hit) for hit in hits)


def test_optional_missing_root_is_skipped_and_doctor_warns(tmp_path: Path, capsys) -> None:
    target = tmp_path / "repo"
    vault = tmp_path / "vault"
    target.mkdir()
    (vault / "Notes").mkdir(parents=True)
    _write_note(vault, "Notes/ok.md", title="Ok", body="rotation")
    _write_vault_config(
        target,
        vault,
        [("notes", "Notes", False), ("maybe", "Missing", True)],
    )
    assert cli.main(["memory", "vault-index", "--target", str(target), "--json"]) == 0
    indexed = json.loads(capsys.readouterr().out)
    assert indexed["indexed"] == 1
    assert indexed["skipped"] == [{"reason": "optional-missing", "scope": "maybe"}]

    assert cli.main(["memory", "vault-doctor", "--target", str(target), "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["valid"] is True
    warn = next(check for check in doctor["checks"] if check["name"] == "root:maybe")
    assert warn["status"] == "warn"


def test_required_missing_root_fails_index_and_doctor(tmp_path: Path, capsys) -> None:
    target = tmp_path / "repo"
    vault = tmp_path / "vault"
    target.mkdir()
    vault.mkdir()
    _write_vault_config(target, vault, [("notes", "Notes", False)])
    assert cli.main(["memory", "vault-index", "--target", str(target)]) == 2
    assert "required root missing" in capsys.readouterr().err

    assert cli.main(["memory", "vault-doctor", "--target", str(target), "--json"]) == 1
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["valid"] is False
    fail = next(check for check in doctor["checks"] if check["name"] == "root:notes")
    assert fail["status"] == "fail"


def test_index_never_writes_into_the_vault(workspace, capsys) -> None:
    target, vault = workspace
    _write_note(vault, "Notes/a.md", title="Alpha", body="alpha body")
    before = sorted(path.relative_to(vault).as_posix() for path in vault.rglob("*"))
    assert cli.main(["memory", "vault-index", "--target", str(target), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["memory", "vault-search", "alpha", "--target", str(target), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["memory", "vault-show", "Notes/a.md", "--target", str(target), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["memory", "vault-doctor", "--target", str(target), "--json"]) == 0
    capsys.readouterr()
    after = sorted(path.relative_to(vault).as_posix() for path in vault.rglob("*"))
    assert after == before
    assert (target / ".brigade" / "vault-index" / "index.json").is_file()


def test_title_from_heading_when_frontmatter_has_none(workspace, capsys) -> None:
    target, vault = workspace
    _write_note(vault, "Notes/headed.md", body="# Heading Title\n\nbody about rotation")
    assert cli.main(["memory", "vault-search", "heading title", "--target", str(target), "--json"]) == 0
    hit = json.loads(capsys.readouterr().out)["hits"][0]
    assert hit["title"] == "Heading Title"
    assert hit["score"] >= 1000


def test_empty_query_is_an_error(workspace, capsys) -> None:
    target, _vault = workspace
    assert cli.main(["memory", "vault-search", "   ", "--target", str(target)]) == 2
    assert "query must not be empty" in capsys.readouterr().err


def test_human_search_output_lists_hits(workspace, capsys) -> None:
    target, vault = workspace
    _write_note(vault, "Notes/a.md", title="Alpha", body="alpha rotation")
    assert cli.main(["memory", "vault-search", "alpha", "--target", str(target)]) == 0
    out = capsys.readouterr().out
    assert "memory vault-search: alpha" in out
    assert "Notes/a.md" in out


def test_index_permissions_are_restored_for_doctor(workspace, capsys) -> None:
    target, vault = workspace
    _write_note(vault, "Notes/a.md", title="Alpha", body="alpha")
    assert cli.main(["memory", "vault-index", "--target", str(target), "--json"]) == 0
    capsys.readouterr()
    index_file = target / ".brigade" / "vault-index" / "index.json"
    os.chmod(index_file, 0o644)
    assert cli.main(["memory", "vault-doctor", "--target", str(target), "--json"]) == 1
    doctor = json.loads(capsys.readouterr().out)
    perm = next(check for check in doctor["checks"] if check["name"] == "vault_index_permissions")
    assert perm["status"] == "fail"
