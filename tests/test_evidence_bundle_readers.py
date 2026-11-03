"""Brigade readers of evidence bundles follow the engine's resolution rule.

A fresh install of the renamed engine writes bundles under the canonical cache
directory. Each reader must see them, keep reading a legacy-only layout in
place, and prefer the canonical layout when both exist
(docs/phase-engine-rename.md, "Resolution rule"). The layout is decided by the
data and config directories, never by the cache.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brigade import engine_paths, memory_cmd, memory_proposal_v2, memory_reflect_candidates
from tests._home import set_home
from tests.test_memory_proposal_capabilities import SAFE_DIRECTORY_CAPABILITY

LAYOUTS = ("canonical", "legacy", "both")
BUNDLE_ID = "bundle-one"
# frozen identifier: see docs/phase-engine-rename.md; persisted or signed contract.
REF = f"{memory_cmd.EVIDENCE_LOCATOR_PREFIX}{BUNDLE_ID}"
bundle_path = memory_cmd._evidence_bundle_path


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    found = {kind: tmp_path / f"xdg-{kind}" for kind in ("data", "config", "cache")}
    for kind, root in found.items():
        monkeypatch.setenv(f"XDG_{kind.upper()}_HOME", str(root))
    return found


def _layout_leaf(role: str) -> Path:
    return engine_paths.EVIDENCE_LEAF if role == "canonical" else engine_paths.LEGACY_EVIDENCE_LEAF


def _seed(roots: dict[str, Path], layout: str) -> Path:
    """Create the directories and bundles for ``layout``; return the bundle a reader must use."""
    roles = ("canonical", "legacy") if layout == "both" else (layout,)
    bundles = {}
    for role in roles:
        leaf = _layout_leaf(role)
        (roots["data"] / leaf).mkdir(parents=True)
        bundle = roots["cache"] / leaf / "evidence" / f"{BUNDLE_ID}.json"
        bundle.parent.mkdir(parents=True)
        bundle.write_text(f'{{"layout": "{role}"}}\n')
        bundles[role] = bundle
    return bundles["legacy"] if layout == "legacy" else bundles["canonical"]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_memory_care_finds_the_resolved_bundle(tmp_path, roots, layout):
    expected = _seed(roots, layout)

    assert bundle_path(BUNDLE_ID) == expected
    assert memory_cmd._tracked_evidence_ref_missing(tmp_path, REF) is None


@pytest.mark.parametrize("layout", LAYOUTS)
def test_reflection_resolves_the_resolved_bundle(roots, layout):
    expected = _seed(roots, layout)

    assert memory_reflect_candidates.evidence_bundle_path(REF) == (True, expected)


@pytest.mark.skipif(not SAFE_DIRECTORY_CAPABILITY, reason="safe directory descriptors unavailable")
@pytest.mark.parametrize("layout", LAYOUTS)
def test_proposal_binds_the_resolved_bundle(tmp_path, roots, layout):
    expected = _seed(roots, layout)

    digests, _ = memory_proposal_v2._evidence(tmp_path, [{"evidence_ids": [REF]}], [])

    assert set(digests) == {REF}
    assert digests[REF] == memory_proposal_v2.io.sha256(expected.read_bytes())


def test_both_layouts_prefer_canonical_over_a_legacy_copy(roots):
    canonical = _seed(roots, "both")
    legacy = roots["cache"] / engine_paths.LEGACY_EVIDENCE_LEAF / "evidence" / f"{BUNDLE_ID}.json"

    assert canonical != legacy and legacy.is_file()
    assert bundle_path(BUNDLE_ID) == canonical


def test_fresh_home_resolves_the_canonical_cache_without_creating_it(tmp_path, roots):
    expected = roots["cache"] / engine_paths.EVIDENCE_LEAF / "evidence" / f"{BUNDLE_ID}.json"

    assert bundle_path(BUNDLE_ID) == expected
    assert "missing" in (memory_cmd._tracked_evidence_ref_missing(tmp_path, REF) or "")
    assert not roots["cache"].exists()


def test_a_legacy_cache_alone_does_not_select_the_legacy_layout(tmp_path, roots):
    legacy = roots["cache"] / engine_paths.LEGACY_EVIDENCE_LEAF / "evidence" / f"{BUNDLE_ID}.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}\n")

    assert bundle_path(BUNDLE_ID) == (roots["cache"] / engine_paths.EVIDENCE_LEAF / "evidence" / f"{BUNDLE_ID}.json")
    assert memory_cmd._tracked_evidence_ref_missing(tmp_path, REF) is not None


def test_legacy_config_directory_alone_selects_the_legacy_layout(roots):
    (roots["config"] / engine_paths.LEGACY_EVIDENCE_LEAF).mkdir(parents=True)

    expected = roots["cache"] / engine_paths.LEGACY_EVIDENCE_LEAF / "evidence" / f"{BUNDLE_ID}.json"
    assert bundle_path(BUNDLE_ID) == expected


def test_default_roots_come_from_home(tmp_path, monkeypatch):
    for kind in ("DATA", "CONFIG", "CACHE"):
        monkeypatch.delenv(f"XDG_{kind}_HOME", raising=False)
    set_home(monkeypatch, tmp_path / "home")

    expected = tmp_path / "home" / ".cache" / engine_paths.EVIDENCE_LEAF / "evidence" / f"{BUNDLE_ID}.json"
    assert bundle_path(BUNDLE_ID) == expected


def test_invalid_bundle_ids_stay_unresolved(roots):
    assert bundle_path("../escape") is None
