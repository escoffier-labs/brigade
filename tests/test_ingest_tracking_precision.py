"""Git tracking protects indexed paths without blocking distinct handoffs."""

import os
import subprocess
from pathlib import Path

import pytest

from brigade import ingest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.mark.parametrize(
    "kind,indexed_name,handoff_name",
    [
        ("distinct", "Tracked.md", "tracked.md"),
        ("distinct", "ÄTracked.md", "ätracked.md"),
        ("distinct", "Straße.md", "STRASSE.md"),
        ("hardlink", "indexed.md", "handoff.md"),
        ("symlink", "tracked-link.md", "handoff.md"),
    ],
)
def test_distinct_untracked_handoff_lists_and_archives(tmp_path: Path, kind, indexed_name, handoff_name):
    _git(tmp_path, "init", "-q")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    handoff = inbox / handoff_name
    handoff.write_text("untracked handoff\n", encoding="utf-8")
    indexed = inbox / indexed_name
    if indexed.exists():
        pytest.skip("filesystem aliases these case variants")
    if kind == "hardlink":
        os.link(handoff, indexed)
    elif kind == "symlink":
        try:
            indexed.symlink_to(handoff.name)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
    else:
        indexed.write_text("tracked document\n", encoding="utf-8")
    _git(tmp_path, "add", "--", f"inbox/{indexed_name}")
    index_before = _git(tmp_path, "ls-files", "--stage", "-z")
    indexed_bytes = indexed.read_bytes()
    original = handoff.read_bytes()

    assert ingest._list_handoffs(inbox) == ([handoff], [indexed])
    with pytest.raises(ingest.TrackedHandoffError):
        ingest._archive(indexed, inbox / "processed")
    assert not (inbox / "processed").exists()
    ingest._archive(handoff, inbox / "processed")

    assert not handoff.exists()
    assert (inbox / "processed" / handoff.name).read_bytes() == original
    assert _git(tmp_path, "ls-files", "--stage", "-z") == index_before
    if kind == "symlink":
        assert indexed.is_symlink()
        assert os.readlink(indexed) == handoff.name
    else:
        assert indexed.read_bytes() == indexed_bytes


@pytest.mark.parametrize("indexed_name,handoff_name", [("Tracked.md", "tracked.md"), ("ÄTracked.md", "ätracked.md")])
def test_archive_refuses_case_alias_recreated_after_identity_read(
    tmp_path: Path, monkeypatch, indexed_name, handoff_name
):
    _git(tmp_path, "init", "-q")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    indexed = inbox / indexed_name
    indexed.write_text("tracked handoff\n", encoding="utf-8")
    _git(tmp_path, "add", "--", f"inbox/{indexed_name}")
    handoff = inbox / handoff_name
    indexed.rename(handoff)
    original = handoff.read_bytes()
    index_before = _git(tmp_path, "ls-files", "--stage", "-z")
    status_before = _git(tmp_path, "status", "--porcelain")
    stat = Path.stat
    injected = False

    def replace_after_read(path, *args, **kwargs):
        nonlocal injected
        if str(path) == str(indexed) and not injected:
            injected = True
            observed = stat(handoff, *args, **kwargs)
            # Keep the old inode alive so the replacement has a new identity.
            handoff.rename(tmp_path / "old-handoff")
            handoff.write_bytes(original)
            assert stat(handoff).st_ino != observed.st_ino
            return observed
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", replace_after_read)
    with pytest.raises(ingest.TrackedHandoffError):
        ingest._archive(handoff, inbox / "processed")

    assert injected
    assert handoff.read_bytes() == original
    assert not (inbox / "processed").exists()
    assert _git(tmp_path, "ls-files", "--stage", "-z") == index_before
    assert _git(tmp_path, "status", "--porcelain", "--", "inbox") == status_before
