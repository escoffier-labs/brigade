import pytest

from brigade import memory_archive as archive, memory_cmd


def setup_card(target, text=b"---\ncard_id: old\n---\nunique-word\r\n"):
    path = target / "memory/cards/old.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text)
    (target / "MEMORY.md").write_text("Header\n- [Old](memory/cards/old.md)\nFooter\n")
    return path


def test_archive_restore_bytes_and_links(tmp_path):
    path = setup_card(tmp_path)
    original = path.read_bytes()
    index = (tmp_path / "MEMORY.md").read_bytes()
    d = archive.archive_card(tmp_path, "memory/cards/old.md", reason="ended", config=memory_cmd.MemoryCareConfig())
    assert not path.exists()
    assert (tmp_path / "memory/archive/cards/old.md").read_bytes() == original
    assert memory_cmd._iter_cards(tmp_path, memory_cmd.MemoryCareConfig(card_roots=("memory",))) == []
    archive.restore(tmp_path, d)
    assert path.read_bytes() == original
    assert (tmp_path / "MEMORY.md").read_bytes() == index


def test_restore_preserves_rewritten_index_and_refuses_occupied_path(tmp_path):
    path = setup_card(tmp_path)
    d = archive.archive_card(tmp_path, "memory/cards/old.md", reason="ended", config=memory_cmd.MemoryCareConfig())
    path.write_bytes(b"new card")
    with pytest.raises(archive.ArchiveRefused):
        archive.restore(tmp_path, d)
    assert path.read_bytes() == b"new card"
    path.unlink()
    (tmp_path / "MEMORY.md").write_text("Replacement\n")
    archive.restore(tmp_path, d)
    assert (tmp_path / "MEMORY.md").read_text() == "Replacement\n- [Old](memory/cards/old.md)\n"


@pytest.mark.parametrize(
    "text,config",
    [
        (b"---\narchive: never\n---\nkeep", memory_cmd.MemoryCareConfig()),
        (b"keep", memory_cmd.MemoryCareConfig(read_only_paths=("memory/cards/",))),
    ],
)
def test_exemptions(tmp_path, text, config):
    path = setup_card(tmp_path, text)
    with pytest.raises(archive.ArchiveRefused):
        archive.archive_card(tmp_path, "memory/cards/old.md", reason="ended", config=config)
    assert path.read_bytes() == text


def test_restore_refuses_changed_archive(tmp_path):
    path = setup_card(tmp_path)
    d = archive.archive_card(tmp_path, "memory/cards/old.md", reason="ended", config=memory_cmd.MemoryCareConfig())
    (tmp_path / "memory/archive/cards/old.md").write_bytes(b"edited archive")
    with pytest.raises(archive.ArchiveRefused):
        archive.restore(tmp_path, d)
    assert not path.exists()


def test_archive_rejects_unsafe_paths(tmp_path):
    for rel in ("../outside.md", "/outside.md"):
        with pytest.raises((archive.ArchiveRefused, ValueError)):
            archive.archive_card(tmp_path, rel, reason="ended", config=memory_cmd.MemoryCareConfig())
