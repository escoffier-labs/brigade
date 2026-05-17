import pytest
from solo_mise.selection import (
    Selection,
    HARNESS_PRIORITY,
    KNOWN_HARNESSES,
    KNOWN_DEPTHS,
    KNOWN_INCLUDES,
    resolve_owner,
)


def test_harness_priority_order():
    assert HARNESS_PRIORITY == ["openclaw", "hermes", "claude", "codex", "this-repo"]


def test_resolve_owner_picks_highest_priority_present():
    assert resolve_owner(["claude", "openclaw"]) == "openclaw"
    assert resolve_owner(["claude", "codex"]) == "claude"
    assert resolve_owner(["codex"]) == "codex"
    assert resolve_owner(["hermes", "claude"]) == "hermes"


def test_resolve_owner_empty_selection_returns_this_repo():
    assert resolve_owner([]) == "this-repo"


def test_resolve_owner_explicit_override_wins():
    assert resolve_owner(["claude", "openclaw"], override="claude") == "claude"
    assert resolve_owner(["claude"], override="this-repo") == "this-repo"


def test_resolve_owner_rejects_override_not_in_selection():
    with pytest.raises(ValueError, match="not in selected harnesses"):
        resolve_owner(["claude"], override="openclaw")


def test_resolve_owner_accepts_this_repo_override_always():
    assert resolve_owner(["openclaw"], override="this-repo") == "this-repo"


def test_selection_dataclass_holds_fields():
    sel = Selection(
        depth="workspace",
        harnesses=["claude", "codex"],
        owner="claude",
        includes=["publisher"],
    )
    assert sel.depth == "workspace"
    assert sel.harnesses == ["claude", "codex"]
    assert sel.owner == "claude"
    assert sel.includes == ["publisher"]


def test_selection_validate_rejects_unknown_depth():
    with pytest.raises(ValueError, match="unknown depth"):
        Selection(depth="weird", harnesses=["claude"], owner="claude", includes=[]).validate()


def test_selection_validate_rejects_unknown_harness():
    with pytest.raises(ValueError, match="unknown harness"):
        Selection(depth="repo", harnesses=["claude", "weird"], owner="claude", includes=[]).validate()


def test_selection_validate_rejects_owner_not_in_harnesses():
    with pytest.raises(ValueError, match="owner.*not in selected harnesses"):
        Selection(depth="repo", harnesses=["claude"], owner="openclaw", includes=[]).validate()


def test_selection_validate_accepts_this_repo_owner_with_empty_harnesses():
    Selection(depth="repo", harnesses=[], owner="this-repo", includes=[]).validate()


def test_known_constants():
    assert set(KNOWN_HARNESSES) == {"claude", "codex", "openclaw", "hermes"}
    assert set(KNOWN_DEPTHS) == {"repo", "workspace"}
    assert set(KNOWN_INCLUDES) == {"publisher"}
