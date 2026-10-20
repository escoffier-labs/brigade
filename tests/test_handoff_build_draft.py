"""build_draft() writes one handoff and returns its payload without printing."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from brigade.handoff_cmd import drafts


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
    assert Path(first["path"]).stat().st_mode & 0o777 == 0o600
    Path(first["path"]).unlink()
    victim = tmp_path / "victim.md"
    Path(first["path"]).symlink_to(victim)
    with pytest.raises(ValueError, match="already exists"):
        drafts.build_draft(**kwargs)
    assert not victim.exists()
    payload = drafts.build_draft(**kwargs, force=True)
    assert payload["valid"] is True
