"""`brigade handoff draft` publishes atomically: a reader never sees a partial `*.md`."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from brigade import dirfd, handoff_publish, ingest
from brigade.handoff_cmd import drafts


def _freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = time.struct_time((2026, 10, 19, 12, 0, 0, 0, 292, 0))
    monkeypatch.setattr(drafts.time, "gmtime", lambda *args: frozen)


class _SplitWriter:
    """Delegates to a real handle but pauses halfway through the first write to run a probe."""

    def __init__(self, handle: Any, probe: Callable[[], None]) -> None:
        self._handle = handle
        self._probe = probe

    def write(self, data: Any) -> int:
        half = len(data) // 2
        written = self._handle.write(data[:half])
        self._handle.flush()
        self._probe()
        return written + self._handle.write(data[half:])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)

    def __enter__(self) -> "_SplitWriter":
        self._handle.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._handle.__exit__(*exc)


def _pause_mid_write(monkeypatch: pytest.MonkeyPatch, probe: Callable[[], None]) -> None:
    real = os.fdopen

    def fake(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real(fd, mode, *args, **kwargs)
        return _SplitWriter(handle, probe) if "w" in mode else handle

    monkeypatch.setattr(os, "fdopen", fake)


def _draft(tmp_path: Path, inbox: Path, **extra: Any) -> dict[str, Any]:
    return drafts.build_draft(
        target=tmp_path, title="Atomic", summary="S.", content="Body.\n" * 200, inbox=str(inbox), **extra
    )


def test_concurrent_reader_never_sees_a_partial_file(tmp_path: Path, monkeypatch):
    _freeze(monkeypatch)
    inbox = tmp_path / "in"
    inbox.mkdir()
    seen: list[list[str]] = []

    def probe() -> None:
        # The writer is halfway through its bytes. A reader listing `*.md` must find nothing.
        seen.append(sorted(p.name for p in inbox.glob("*.md")))

    _pause_mid_write(monkeypatch, probe)
    payload = _draft(tmp_path, inbox)
    assert seen == [[]]
    published = Path(payload["path"])
    assert published.read_text(encoding="utf-8").endswith("Body.\n")
    assert sorted(p.name for p in inbox.iterdir()) == [published.name]


def test_temp_name_is_never_listed_by_ingest_or_the_draft_queue(tmp_path: Path, monkeypatch):
    _freeze(monkeypatch)
    inbox = tmp_path / "in"
    inbox.mkdir()
    observed: dict[str, Any] = {}

    def probe() -> None:
        observed["on_disk"] = sorted(p.name for p in inbox.iterdir())
        observed["ingest"] = [p.name for p in ingest._list_handoffs(inbox)[0]]
        observed["glob"] = [p.name for p in inbox.glob("*.md")]

    _pause_mid_write(monkeypatch, probe)
    _draft(tmp_path, inbox)
    assert len(observed["on_disk"]) == 1
    (temporary,) = observed["on_disk"]
    assert handoff_publish.is_temp_name(temporary)
    assert not temporary.endswith(".md")
    assert observed["ingest"] == []
    assert observed["glob"] == []


def test_an_existing_name_is_never_overwritten(tmp_path: Path, monkeypatch):
    _freeze(monkeypatch)
    inbox = tmp_path / "in"
    inbox.mkdir()
    first = Path(_draft(tmp_path, inbox)["path"])
    first_bytes = first.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        drafts.build_draft(target=tmp_path, title="Atomic", summary="S.", content="Different.", inbox=str(inbox))
    assert first.read_bytes() == first_bytes
    assert sorted(p.name for p in inbox.iterdir()) == [first.name]


def test_a_symlink_at_the_final_name_is_not_followed(tmp_path: Path, monkeypatch):
    _freeze(monkeypatch)
    inbox = tmp_path / "in"
    inbox.mkdir()
    victim = tmp_path / "victim.md"
    victim.write_text("keep", encoding="utf-8")
    first = Path(_draft(tmp_path, inbox)["path"])
    first.unlink()
    first.symlink_to(victim)
    with pytest.raises(ValueError, match="already exists"):
        _draft(tmp_path, inbox)
    assert victim.read_text(encoding="utf-8") == "keep"
    assert first.is_symlink()
    assert [p.name for p in inbox.iterdir()] == [first.name]


@pytest.mark.parametrize("stage", ["write", "fsync", "publish"])
def test_the_temp_file_is_removed_after_a_failure(tmp_path: Path, monkeypatch, stage: str):
    inbox = tmp_path / "in"
    inbox.mkdir()

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError(28, "simulated failure")

    if stage == "write":
        _pause_mid_write(monkeypatch, lambda: boom())
    elif stage == "fsync":
        monkeypatch.setattr(os, "fsync", boom)
    else:
        monkeypatch.setattr(dirfd, "replace_children", boom)
    with pytest.raises(OSError, match="simulated failure"):
        handoff_publish.publish_handoff(inbox, "20261019-120000-x.md", "# Memory Handoff\n" * 100)
    assert list(inbox.iterdir()) == []


def test_replace_swaps_a_finished_file_atomically(tmp_path: Path):
    inbox = tmp_path / "in"
    inbox.mkdir()
    handoff_publish.publish_handoff(inbox, "a.md", "one\n")
    handoff_publish.publish_handoff(inbox, "a.md", "two\n", replace=True)
    assert (inbox / "a.md").read_text(encoding="utf-8") == "two\n"
    assert [p.name for p in inbox.iterdir()] == ["a.md"]


def test_the_pathname_fallback_follows_the_same_contract(tmp_path: Path, monkeypatch):
    # Without descriptor-relative primitives (no O_NOFOLLOW), the same three steps run by pathname.
    monkeypatch.setattr(dirfd, "available", lambda: False)
    inbox = tmp_path / "in"
    inbox.mkdir()
    seen: list[list[str]] = []
    _pause_mid_write(monkeypatch, lambda: seen.append(sorted(p.name for p in inbox.glob("*.md"))))
    path = handoff_publish.publish_handoff(inbox, "a.md", "# Memory Handoff\n" * 50)
    assert seen == [[]]
    assert path.read_text(encoding="utf-8").startswith("# Memory Handoff")
    with pytest.raises(handoff_publish.HandoffNameTaken):
        handoff_publish.publish_handoff(inbox, "a.md", "other\n")
    assert [p.name for p in inbox.iterdir()] == ["a.md"]
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(5, "simulated")))
    with pytest.raises(OSError, match="simulated"):
        handoff_publish.publish_handoff(inbox, "b.md", "x\n")
    assert [p.name for p in inbox.iterdir()] == ["a.md"]
