"""Acceptance tests for the operator-vault propose write-back (#945)."""

from __future__ import annotations

import hashlib
import io
import json
import stat
from pathlib import Path

import pytest

from brigade import cli, memory_vault
from brigade.card_identity import valid_card_id
from brigade.projection import kernel


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


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "repo"
    vault = tmp_path / "vault"
    target.mkdir()
    (vault / "Notes").mkdir(parents=True)
    (vault / "Inbox").mkdir(parents=True)
    (vault / "Brigade Memory" / "Cards").mkdir(parents=True)
    _write_vault_config(
        target,
        vault,
        [
            ("notes", "Notes", False),
            ("inbox", "Inbox", False),
            ("generated", "Brigade Memory", False),
        ],
    )
    return target, vault


def _propose(
    target: Path,
    *,
    title: str,
    scope: str,
    body: str,
    dry_run: bool = False,
    json_output: bool = True,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    command = [
        "memory",
        "vault-propose",
        "--title",
        title,
        "--scope",
        scope,
        "--target",
        str(target),
    ]
    if dry_run:
        command.append("--dry-run")
    if json_output:
        command.append("--json")
    return cli.main(command)


def test_propose_writes_an_additive_inbox_note_with_stable_id(workspace, capsys, monkeypatch) -> None:
    target, vault = workspace
    body = "Rotate operator vault keys quarterly.\n"
    assert _propose(target, title="Rotation Policy", scope="inbox", body=body, monkeypatch=monkeypatch) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "brigade.vault-propose.v1"
    assert payload["dry_run"] is False
    assert payload["scope"] == "inbox"
    assert payload["relative_path"] == "Inbox/rotation-policy.md"
    assert payload["vault"] == "redacted:operator-vault"
    assert valid_card_id(payload["id"]) == payload["id"]
    assert payload["receipt"]["terminal_state"] == "committed"

    note = vault / "Inbox" / "rotation-policy.md"
    assert note.is_file()
    assert not note.is_symlink()
    text = note.read_text(encoding="utf-8")
    assert f"canonical_id: {payload['id']}" in text
    assert f"id: {payload['id']}" in text
    assert "title: Rotation Policy" in text
    assert "Rotate operator vault keys quarterly." in text
    assert payload["content_hash"] == f"sha256:{hashlib.sha256(note.read_bytes()).hexdigest()}"
    assert (vault / "Brigade Memory" / "Cards").exists()
    assert not list((vault / "Brigade Memory").rglob("*.md"))
    staging = target / ".brigade" / "vault-propose"
    assert staging.is_dir()
    assert stat.S_IMODE(staging.stat().st_mode) == 0o700
    assert list(staging.iterdir()) == []


def test_dry_run_reports_destination_and_bytes_without_touching_the_vault(workspace, capsys, monkeypatch) -> None:
    target, vault = workspace
    before = sorted(path.relative_to(vault).as_posix() for path in vault.rglob("*"))
    assert (
        _propose(
            target,
            title="Rotation Policy",
            scope="inbox",
            body="durable note body\n",
            dry_run=True,
            monkeypatch=monkeypatch,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["relative_path"] == "Inbox/rotation-policy.md"
    assert payload["receipt"] is None
    assert "durable note body" in payload["rendered"]
    assert payload["bytes"] == len(payload["rendered"].encode("utf-8"))
    after = sorted(path.relative_to(vault).as_posix() for path in vault.rglob("*"))
    assert after == before
    assert not (target / ".brigade" / "vault-propose").exists()


def test_unknown_scope_is_an_error(workspace, capsys, monkeypatch) -> None:
    target, _vault = workspace
    assert _propose(target, title="Note", scope="secrets", body="body", monkeypatch=monkeypatch) == 2
    assert "unknown scope" in capsys.readouterr().err


def test_missing_config_is_an_error(tmp_path: Path, capsys, monkeypatch) -> None:
    target = tmp_path / "repo"
    target.mkdir()
    assert _propose(target, title="Note", scope="inbox", body="body", monkeypatch=monkeypatch) == 2
    assert "not configured" in capsys.readouterr().err


def test_empty_title_is_an_error(workspace, capsys, monkeypatch) -> None:
    target, _vault = workspace
    assert _propose(target, title="   ", scope="inbox", body="body", monkeypatch=monkeypatch) == 2
    assert "title must not be empty" in capsys.readouterr().err


def test_title_cannot_be_a_path(workspace, capsys, monkeypatch) -> None:
    target, vault = workspace
    assert _propose(target, title="../escape", scope="inbox", body="body", monkeypatch=monkeypatch) == 2
    assert "not a path" in capsys.readouterr().err
    assert list((vault / "Inbox").iterdir()) == []


def test_refuses_to_overwrite_an_existing_note(workspace, capsys, monkeypatch) -> None:
    target, vault = workspace
    existing = vault / "Inbox" / "rotation-policy.md"
    existing.write_text("operator owned\n", encoding="utf-8")
    assert _propose(target, title="Rotation Policy", scope="inbox", body="new", monkeypatch=monkeypatch) == 2
    assert "overwrite" in capsys.readouterr().err
    assert existing.read_text(encoding="utf-8") == "operator owned\n"


def test_refuses_the_generated_projection_folder(workspace, capsys, monkeypatch) -> None:
    target, vault = workspace
    assert _propose(target, title="Alpha", scope="generated", body="body", monkeypatch=monkeypatch) == 2
    assert "Brigade Memory" in capsys.readouterr().err
    assert not list((vault / "Brigade Memory").rglob("*.md"))


def test_does_not_follow_a_symlinked_inbox(workspace, capsys, monkeypatch) -> None:
    target, vault = workspace
    outside = target.parent / "outside"
    outside.mkdir()
    (vault / "Escape").symlink_to(outside)
    _write_vault_config(target, vault, [("escape", "Escape", False)])
    assert _propose(target, title="Leaked", scope="escape", body="OUTSIDEONLY", monkeypatch=monkeypatch) == 2
    err = capsys.readouterr().err
    assert "symlink" in err or "escapes" in err or "missing" in err
    assert not (outside / "leaked.md").exists()


def test_rejects_a_symlinked_ancestor_on_the_destination(workspace, capsys, monkeypatch) -> None:
    target, vault = workspace
    outside = target.parent / "outside-inbox"
    outside.mkdir()
    inbox = vault / "Inbox"
    inbox.rmdir()
    inbox.symlink_to(outside)
    assert _propose(target, title="Leaked", scope="inbox", body="OUTSIDEONLY", monkeypatch=monkeypatch) == 2
    assert not (outside / "leaked.md").exists()


def test_refuses_staging_that_resolves_inside_the_vault(tmp_path: Path, capsys, monkeypatch) -> None:
    vault = tmp_path / "vault"
    target = vault / "repo"
    target.mkdir(parents=True)
    (vault / "Inbox").mkdir()
    _write_vault_config(target, vault, [("inbox", "Inbox", False)])
    assert _propose(target, title="Inside", scope="inbox", body="body", monkeypatch=monkeypatch) == 2
    assert "staging directory resolves inside the vault" in capsys.readouterr().err
    assert list((vault / "Inbox").iterdir()) == []


def test_fails_closed_when_containment_checks_are_unavailable(workspace, capsys, monkeypatch) -> None:
    target, vault = workspace
    monkeypatch.setattr(memory_vault, "_containment_primitives_available", lambda: False)
    assert _propose(target, title="Note", scope="inbox", body="body", monkeypatch=monkeypatch) == 2
    assert "unavailable on this platform" in capsys.readouterr().err
    assert list((vault / "Inbox").iterdir()) == []


def test_held_staged_bytes_are_the_bytes_that_land(workspace, capsys, monkeypatch) -> None:
    target, vault = workspace
    real_stage = memory_vault._stage_proposal

    def swap_after_stage(staging_dir: Path, data: bytes):
        descriptor, path, validated = real_stage(staging_dir, data)
        path.write_bytes(b"swapped after validation\n")
        return descriptor, path, validated

    monkeypatch.setattr(memory_vault, "_stage_proposal", swap_after_stage)
    assert (
        _propose(
            target,
            title="Held Bytes",
            scope="inbox",
            body="validated body\n",
            monkeypatch=monkeypatch,
        )
        == 0
    )
    capsys.readouterr()
    text = (vault / "Inbox" / "held-bytes.md").read_text(encoding="utf-8")
    assert "validated body" in text
    assert "swapped after validation" not in text


def test_failed_delivery_restores_rather_than_leaving_a_partial_note(workspace, monkeypatch) -> None:
    target, vault = workspace
    real_execute = kernel.execute

    def fail_during_commit(plan, *, target):
        return real_execute(plan, target=target, inject=kernel.FailureInjector(boundary="commit:0:after"))

    monkeypatch.setattr(memory_vault.kernel, "execute", fail_during_commit)
    with pytest.raises(memory_vault.VaultProposeError):
        memory_vault.propose_payload(
            target,
            title="Partial",
            scope="inbox",
            body="should not land\n",
            dry_run=False,
        )
    assert list((vault / "Inbox").iterdir()) == []


def test_output_redacts_interpolated_secrets(workspace, capsys, monkeypatch) -> None:
    target, _vault = workspace
    assert (
        _propose(
            target,
            title="Contact Rotation",
            scope="inbox",
            body="Bearer " + "super-secret-token-value-" + "123456 about rotation.",
            monkeypatch=monkeypatch,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["vault"] == "redacted:operator-vault"
    assert str(_vault) not in json.dumps(payload)
    assert "super-secret-token-value-" + "123456" not in payload["title"]


def test_human_dry_run_prints_rendered_bytes(workspace, capsys, monkeypatch) -> None:
    target, _vault = workspace
    assert (
        _propose(
            target,
            title="Human Note",
            scope="inbox",
            body="human body\n",
            dry_run=True,
            json_output=False,
            monkeypatch=monkeypatch,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "memory vault-propose dry-run: Inbox/human-note.md" in out
    assert "human body" in out


def test_command_does_not_accept_vault_flag(workspace) -> None:
    target, _vault = workspace
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "memory",
                "vault-propose",
                "--title",
                "x",
                "--scope",
                "inbox",
                "--vault",
                "/tmp/fake-vault",
                "--target",
                str(target),
            ]
        )
    assert exc.value.code == 2
