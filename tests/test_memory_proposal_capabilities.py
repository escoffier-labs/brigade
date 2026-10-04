"""Proposal syntax and fail-closed capability checks run on every platform."""

from __future__ import annotations

import os

import pytest

from brigade import cli, memory_proposals as api

SAFE_DIRECTORY_CAPABILITY = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")
PROPOSAL_ID = "proposal-" + "0" * 32
DIGEST = "0" * 64
COMMANDS = ("create", "show", "review", "reject", "apply")


@pytest.fixture
def minimal_target(tmp_path):
    cards = tmp_path / "memory/cards"
    cards.mkdir(parents=True)
    (cards / "source.md").write_text("Source bytes must remain unchanged.\n", encoding="utf-8")
    (tmp_path / ".claude/memory-handoffs").mkdir(parents=True)
    (tmp_path / ".brigade").mkdir()
    return tmp_path


def _snapshot(target):
    return {
        str(path.relative_to(target)): (path.stat().st_mode, path.read_bytes() if path.is_file() else None)
        for path in target.rglob("*")
    }


def _assert_capability_refusal(target, command, capsys):
    before = _snapshot(target)
    kwargs = {"target": target}
    args = ["memory", "proposal", command]
    if command == "create":
        kwargs.update(
            issue_id="saved-finding", survivor="memory/cards/source.md", relation="merge", reason="Inspect pair."
        )
        args += [
            "--issue",
            "saved-finding",
            "--survivor",
            "memory/cards/source.md",
            "--relation",
            "merge",
            "--reason",
            "Inspect pair.",
        ]
    else:
        kwargs["proposal_id"] = PROPOSAL_ID
        args.append(PROPOSAL_ID)
        if command != "show":
            kwargs["digest"] = DIGEST
            args += ["--digest", DIGEST]
        if command in ("review", "reject"):
            kwargs["reason"] = "Inspect pair."
            args += ["--reason", "Inspect pair."]
    exit_code = 2 if command == "create" else 1
    with pytest.raises(api.ProposalError) as result:
        getattr(api, command + "_payload")(**kwargs)
    assert result.value.exit_code == exit_code
    assert isinstance(result.value.__cause__, OSError)
    assert str(result.value.__cause__) == "safe directory descriptors unavailable"
    assert cli.main([*args, "--target", str(target)]) == exit_code
    capsys.readouterr()
    assert _snapshot(target) == before
    assert not (target / api.STATE_REL).exists()
    assert not list(target.glob(".claude/memory-handoffs/*"))


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("unavailable", ["dir-fd", "nofollow"])
def test_proposal_requires_safe_directory_capability_before_any_artifact(
    minimal_target, monkeypatch, capsys, command, unavailable
):
    if unavailable == "dir-fd":
        monkeypatch.setattr(os, "supports_dir_fd", set())
    else:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    _assert_capability_refusal(minimal_target, command, capsys)


@pytest.mark.skipif(SAFE_DIRECTORY_CAPABILITY, reason="native safe directory descriptors available")
@pytest.mark.parametrize("command", COMMANDS)
def test_native_unsupported_capability_refuses_without_writes(minimal_target, capsys, command):
    _assert_capability_refusal(minimal_target, command, capsys)


@pytest.mark.parametrize(
    "command,malformed",
    [
        ("show", "id"),
        ("review", "id"),
        ("reject", "id"),
        ("apply", "id"),
        ("review", "digest"),
        ("reject", "digest"),
        ("apply", "digest"),
        ("review", "reason"),
        ("reject", "reason"),
    ],
)
def test_cli_invalid_proposal_input_is_exit_two(tmp_path, capsys, command, malformed):
    proposal = {"id": PROPOSAL_ID, "digest": DIGEST}
    args = [
        "memory",
        "proposal",
        command,
        "invalid" if malformed == "id" else proposal["id"],
        "--target",
        str(tmp_path),
    ]
    if command != "show":
        args += ["--digest", "invalid" if malformed == "digest" else proposal["digest"]]
    if command in ("review", "reject"):
        args += ["--reason", "" if malformed == "reason" else "Review exact revision."]
    assert cli.main(args) == 2
    capsys.readouterr()


@pytest.mark.parametrize("command", ["show", "review", "reject", "apply"])
def test_cli_missing_required_proposal_input_is_exit_two(tmp_path, command):
    args = ["memory", "proposal", command, "--target", str(tmp_path)]
    with pytest.raises(SystemExit) as result:
        cli.main(args)
    assert result.value.code == 2
