import json
import pytest
from brigade.config import (
    Config,
    CONFIG_REL_PATH,
    config_path,
    load_config,
    write_config,
)
from brigade.selection import Selection


def test_config_rel_path():
    assert CONFIG_REL_PATH == ".brigade/config.json"


def test_config_path_resolves_relative_to_target(tmp_path):
    assert config_path(tmp_path) == tmp_path / ".brigade" / "config.json"


def test_write_then_load_round_trip(tmp_path):
    sel = Selection(
        depth="workspace",
        harnesses=["claude", "codex", "openclaw"],
        owner="openclaw",
        includes=["publisher"],
    )
    cfg = Config(version=1, selection=sel)
    write_config(tmp_path, cfg)

    loaded = load_config(tmp_path)
    assert loaded.version == 1
    assert loaded.selection.depth == "workspace"
    assert loaded.selection.harnesses == ["claude", "codex", "openclaw"]
    assert loaded.selection.owner == "openclaw"
    assert loaded.selection.includes == ["publisher"]


def test_write_creates_parent_dir(tmp_path):
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    write_config(tmp_path, Config(version=1, selection=sel))
    assert (tmp_path / ".brigade" / "config.json").is_file()


def test_load_missing_returns_none(tmp_path):
    assert load_config(tmp_path) is None


def test_load_rejects_unknown_version(tmp_path):
    path = tmp_path / ".solo-mise" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 99, "depth": "repo", "harnesses": [], "owner": "this-repo", "includes": []}))
    with pytest.raises(ValueError, match="unsupported config version"):
        load_config(tmp_path)


def test_load_rejects_invalid_selection(tmp_path):
    path = tmp_path / ".solo-mise" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "depth": "weird", "harnesses": [], "owner": "this-repo", "includes": []}))
    with pytest.raises(ValueError, match="unknown depth"):
        load_config(tmp_path)


def test_write_produces_pretty_json(tmp_path):
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    write_config(tmp_path, Config(version=1, selection=sel))
    text = (tmp_path / ".brigade" / "config.json").read_text()
    assert "\n  " in text  # indented
    assert text.endswith("\n")


def test_load_config_reads_legacy_solo_mise_dir(tmp_path):
    from brigade.config import load_config

    legacy = tmp_path / ".solo-mise"
    legacy.mkdir()
    (legacy / "config.json").write_text(
        '{"version": 1, "depth": "repo", "harnesses": ["claude"], "owner": "claude", "includes": []}'
    )
    cfg = load_config(tmp_path)
    assert cfg is not None
    assert cfg.selection.harnesses == ["claude"]
