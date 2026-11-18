import pytest

from brigade import memory_proposal_io as io


@pytest.mark.parametrize("value", ["memory/cards/a.md", "a", ".brigade/lesson-review/x.json"])
def test_posix_relative_paths_are_canonical_on_every_platform(value):
    assert io.relative(value) == value


@pytest.mark.parametrize("value", ["/etc/passwd", "C:/x", "C:x", "a/../b", "a//b", "./a", "a/", ".", "a\\b", ""])
def test_absolute_drive_traversal_and_noncanonical_paths_are_refused(value):
    with pytest.raises(ValueError):
        io.relative(value)
