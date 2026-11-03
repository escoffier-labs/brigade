"""Shared engine storage resolution cases (docs/phase-engine-rename.md, Storage locations)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import engine_paths

CASES = json.loads((Path(__file__).parent / "fixtures" / "engine-paths-cases.json").read_text())
CODE_GRAPH = CASES["code_graph"]
EVIDENCE = CASES["evidence"]


def test_fixture_layouts_match_module_constants():
    assert Path(CODE_GRAPH["layouts"]["canonical"]) == engine_paths.CODE_GRAPH_DB
    assert Path(CODE_GRAPH["layouts"]["legacy"]) == engine_paths.LEGACY_CODE_GRAPH_DB
    assert CODE_GRAPH["env"] == {
        "canonical": engine_paths.CODE_GRAPH_DB_ENV,
        "legacy": engine_paths.LEGACY_CODE_GRAPH_DB_ENV,
    }
    layouts = EVIDENCE["layouts"]
    assert Path(layouts["canonical"]["leaf"]) == engine_paths.EVIDENCE_LEAF
    assert layouts["canonical"]["db"] == engine_paths.EVIDENCE_DB_NAME
    assert Path(layouts["legacy"]["leaf"]) == engine_paths.LEGACY_EVIDENCE_LEAF
    assert layouts["legacy"]["db"] == engine_paths.LEGACY_EVIDENCE_DB_NAME
    assert EVIDENCE["mac_key"] == engine_paths.EVIDENCE_MAC_KEY_NAME
    assert EVIDENCE["config"] == engine_paths.EVIDENCE_CONFIG_NAME


@pytest.mark.parametrize("case", CODE_GRAPH["cases"], ids=lambda case: case["name"])
def test_code_graph_db_case(tmp_path, case):
    repo = tmp_path / "repo"
    for role in case["present"]:
        db = repo / CODE_GRAPH["layouts"][role]
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"index")
    env = {CODE_GRAPH["env"][role]: value for role, value in case.get("env", {}).items()}
    snapshot = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    expect = case["expect"]

    if "error" in expect:
        with pytest.raises(ValueError, match="conflicting"):
            engine_paths.code_graph_db(repo, explicit=case.get("explicit"), env=env)
        return
    resolved = engine_paths.code_graph_db(repo, explicit=case.get("explicit"), env=env)

    expected_path = repo / (expect["path"] if "path" in expect else CODE_GRAPH["layouts"][expect["layout"]])
    assert resolved == engine_paths.ResolvedLocation(expected_path, expect["source"], expect["legacy_present"])
    # Resolution only reads: nothing is created, copied or moved.
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == snapshot


def test_code_graph_db_keeps_absolute_explicit_path(tmp_path):
    elsewhere = tmp_path / "elsewhere" / "index.db"
    resolved = engine_paths.code_graph_db(tmp_path / "repo", explicit=elsewhere, env={})
    assert resolved == engine_paths.ResolvedLocation(elsewhere, "explicit", False)


def test_code_graph_db_ignores_empty_overrides(tmp_path):
    env = {engine_paths.CODE_GRAPH_DB_ENV: "", engine_paths.LEGACY_CODE_GRAPH_DB_ENV: ""}
    resolved = engine_paths.code_graph_db(tmp_path, explicit="", env=env)
    assert resolved.source == "new"


def _evidence_env(tmp_path: Path, xdg: bool) -> tuple[dict[str, str], dict[str, Path]]:
    home = tmp_path / "home"
    env = {"HOME": str(home)}
    roots = {}
    for kind, spec in EVIDENCE["roots"].items():
        if xdg:
            roots[kind] = tmp_path / f"xdg-{kind}"
            env[spec["env"]] = str(roots[kind])
        else:
            roots[kind] = home / spec["home_fallback"]
    return env, roots


@pytest.mark.parametrize("case", EVIDENCE["cases"], ids=lambda case: case["name"])
def test_evidence_dirs_case(tmp_path, case):
    env, roots = _evidence_env(tmp_path, case.get("xdg", False))
    for kind, role in case["present"]:
        (roots[kind] / EVIDENCE["layouts"][role]["leaf"]).mkdir(parents=True)
    snapshot = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    expect = case["expect"]

    resolved = engine_paths.evidence_dirs(env=env)

    layout = EVIDENCE["layouts"][expect["layout"]]
    data_dir = roots["data"] / layout["leaf"]
    assert resolved == engine_paths.EvidenceDirs(
        data_dir=data_dir,
        db_path=data_dir / layout["db"],
        mac_key_path=data_dir / EVIDENCE["mac_key"],
        config_path=roots["config"] / layout["leaf"] / EVIDENCE["config"],
        cache_dir=roots["cache"] / layout["leaf"],
        source=expect["source"],
        legacy_present=expect["legacy_present"],
    )
    assert resolved.location == engine_paths.ResolvedLocation(data_dir, expect["source"], expect["legacy_present"])
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == snapshot


def test_evidence_dirs_requires_a_root():
    with pytest.raises(ValueError, match="HOME"):
        engine_paths.evidence_dirs(env={})
