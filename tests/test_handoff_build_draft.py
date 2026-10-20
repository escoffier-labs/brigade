"""build_draft() writes one handoff and returns its payload without printing."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from brigade.handoff_cmd import drafts


def _assert_mode(path: Path, mode: int) -> None:
    # Windows chmod only toggles the read-only bit, so permission bits are POSIX-only.
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == mode


def _freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = time.struct_time((2026, 10, 19, 12, 0, 0, 0, 292, 0))
    monkeypatch.setattr(drafts.time, "gmtime", lambda *args: frozen)


def test_build_draft_returns_payload(tmp_path: Path, monkeypatch, capsys):
    _freeze(monkeypatch)
    payload = drafts.build_draft(
        target=tmp_path, title="Plugin memory", summary="Short summary.", content="Body.", inbox=str(tmp_path / "in")
    )
    assert payload["valid"] is True
    assert payload["action"] == "no-card"
    assert Path(payload["path"]).is_file()
    assert capsys.readouterr().out == ""


def test_build_draft_refuses_overwrite(tmp_path: Path, monkeypatch):
    _freeze(monkeypatch)
    kwargs = dict(target=tmp_path, title="Same", summary="S.", content="Body.", inbox=str(tmp_path / "in"))
    drafts.build_draft(**kwargs)
    with pytest.raises(ValueError, match="already exists"):
        drafts.build_draft(**kwargs)


def test_forced_draft_preserves_lf_bytes(tmp_path: Path, monkeypatch):
    _freeze(monkeypatch)
    kwargs = dict(target=tmp_path, title="Same", summary="S.", content="Body.\nNext line.", inbox=str(tmp_path / "in"))
    path = Path(drafts.build_draft(**kwargs)["path"])
    original = path.read_bytes()
    assert b"\n" in original and b"\r" not in original
    drafts.build_draft(**kwargs, force=True)
    assert path.read_bytes() == original


def test_build_draft_validates_inputs(tmp_path: Path):
    with pytest.raises(ValueError, match="--target-card is required"):
        drafts.build_draft(target=tmp_path, title="T", summary="S.", content="Body.", action="create-card")
    with pytest.raises(ValueError, match="--action must be one of"):
        drafts.build_draft(target=tmp_path, title="T", summary="S.", content="Body.", action="delete")


def test_build_draft_creates_private_file_without_following_symlinks(tmp_path: Path, monkeypatch):
    _freeze(monkeypatch)
    inbox = tmp_path / "in"
    inbox.mkdir()
    kwargs = dict(target=tmp_path, title="Same", summary="S.", content="Body.", inbox=str(inbox))
    first = drafts.build_draft(**kwargs)
    _assert_mode(Path(first["path"]), 0o600)
    Path(first["path"]).unlink()
    victim = tmp_path / "victim.md"
    Path(first["path"]).symlink_to(victim)
    with pytest.raises(ValueError, match="already exists"):
        drafts.build_draft(**kwargs)
    assert not victim.exists()


def test_forced_draft_stays_private_and_refuses_a_planted_symlink(tmp_path: Path, monkeypatch):
    _freeze(monkeypatch)
    inbox = tmp_path / "in"
    inbox.mkdir()
    kwargs = dict(target=tmp_path, title="Same", summary="S.", content="Body.", inbox=str(inbox), force=True)
    first = drafts.build_draft(**kwargs)
    path = Path(first["path"])
    _assert_mode(path, 0o600)
    path.chmod(0o644)
    drafts.build_draft(**kwargs)
    _assert_mode(path, 0o600)
    path.unlink()
    victim = tmp_path / "victim.md"
    victim.write_text("keep", encoding="utf-8")
    path.symlink_to(victim)
    with pytest.raises(ValueError, match="symlink"):
        drafts.build_draft(**kwargs)
    assert victim.read_text(encoding="utf-8") == "keep"


def test_forced_draft_refuses_a_symlink_where_o_nofollow_is_missing(tmp_path: Path, monkeypatch):
    _freeze(monkeypatch)
    monkeypatch.delattr(drafts.os, "O_NOFOLLOW", raising=False)
    inbox = tmp_path / "in"
    inbox.mkdir()
    kwargs = dict(target=tmp_path, title="Same", summary="S.", content="Body.", inbox=str(inbox), force=True)
    path = Path(drafts.build_draft(**kwargs)["path"])
    path.unlink()
    victim = tmp_path / "victim.md"
    victim.write_text("keep", encoding="utf-8")
    path.symlink_to(victim)
    with pytest.raises(ValueError, match="symlink"):
        drafts.build_draft(**kwargs)
    assert victim.read_text(encoding="utf-8") == "keep"


def test_forced_draft_never_writes_through_a_symlink_planted_after_the_check(tmp_path: Path, monkeypatch):
    _freeze(monkeypatch)
    monkeypatch.delattr(drafts.os, "O_NOFOLLOW", raising=False)
    inbox = tmp_path / "in"
    inbox.mkdir()
    kwargs = dict(target=tmp_path, title="Same", summary="S.", content="Body.", inbox=str(inbox), force=True)
    path = Path(drafts.build_draft(**kwargs)["path"])
    path.unlink()
    victim = tmp_path / "victim.md"
    victim.write_text("keep", encoding="utf-8")
    path.symlink_to(victim)
    # Simulate the race: the symlink check runs before the link appears.
    monkeypatch.setattr(drafts.Path, "is_symlink", lambda self: False)
    drafts.build_draft(**kwargs)
    assert victim.read_text(encoding="utf-8") == "keep"
    assert path.is_file() and not os.path.islink(path)
    _assert_mode(path, 0o600)
    assert sorted(p.name for p in inbox.iterdir()) == [path.name]


def test_draft_without_o_nofollow_refuses_a_dangling_symlink(tmp_path: Path, monkeypatch):
    _freeze(monkeypatch)
    monkeypatch.delattr(drafts.os, "O_NOFOLLOW", raising=False)
    inbox = tmp_path / "in"
    inbox.mkdir()
    kwargs = dict(target=tmp_path, title="Same", summary="S.", content="Body.", inbox=str(inbox))
    path = Path(drafts.build_draft(**kwargs)["path"])
    assert path.read_text(encoding="utf-8").startswith("# Memory Handoff")
    _assert_mode(path, 0o600)
    path.unlink()
    victim = tmp_path / "victim.md"
    path.symlink_to(victim)
    with pytest.raises(ValueError, match="already exists"):
        drafts.build_draft(**kwargs)
    assert not victim.exists()
    assert sorted(p.name for p in inbox.iterdir()) == [path.name]
