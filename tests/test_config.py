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


def test_write_then_load_round_trip_preserves_graphtrail_delta_timeout_seconds(tmp_path):
    sel = Selection(
        depth="workspace",
        harnesses=["claude", "codex", "openclaw"],
        owner="openclaw",
        includes=["publisher"],
    )
    cfg = Config(version=1, selection=sel, graphtrail_delta_timeout_seconds=25)
    write_config(tmp_path, cfg)

    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.graphtrail_delta_timeout_seconds == 25.0


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


def test_load_config_defaults_graphtrail_delta_timeout_seconds(tmp_path):
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    write_config(tmp_path, Config(version=1, selection=sel))
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.graphtrail_delta_timeout_seconds == 10.0


def test_load_config_reads_graphtrail_delta_timeout_seconds(tmp_path):
    path = tmp_path / ".brigade" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "depth": "repo",
                "harnesses": ["claude"],
                "owner": "claude",
                "includes": [],
                "graphtrail_delta_timeout_seconds": 25,
            }
        )
        + "\n"
    )
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.graphtrail_delta_timeout_seconds == 25.0


@pytest.mark.parametrize("value", [0, -1, "slow"])
def test_load_config_rejects_invalid_graphtrail_delta_timeout_seconds(tmp_path, value):
    path = tmp_path / ".brigade" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "depth": "repo",
                "harnesses": ["claude"],
                "owner": "claude",
                "includes": [],
                "graphtrail_delta_timeout_seconds": value,
            }
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="graphtrail_delta_timeout_seconds must be a positive number"):
        load_config(tmp_path)


def test_load_config_defaults_capture_before_retry(tmp_path):
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    write_config(tmp_path, Config(version=1, selection=sel))
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.capture_before_retry == "warn"


def test_load_config_reads_capture_before_retry(tmp_path):
    path = tmp_path / ".brigade" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "depth": "repo",
                "harnesses": ["claude"],
                "owner": "claude",
                "includes": [],
                "capture_before_retry": "block",
            }
        )
        + "\n"
    )
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.capture_before_retry == "block"


@pytest.mark.parametrize("value", ["maybe", 1, True])
def test_load_config_rejects_invalid_capture_before_retry(tmp_path, value):
    path = tmp_path / ".brigade" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "depth": "repo",
                "harnesses": ["claude"],
                "owner": "claude",
                "includes": [],
                "capture_before_retry": value,
            }
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="capture_before_retry must be one of"):
        load_config(tmp_path)


def _write_config_json(tmp_path, **overrides):
    payload = {
        "version": 1,
        "depth": "repo",
        "harnesses": ["claude"],
        "owner": "claude",
        "includes": [],
    }
    payload.update(overrides)
    path = tmp_path / ".brigade" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")


def test_load_config_defaults_verify_retention_settings(tmp_path):
    _write_config_json(tmp_path)
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.verify_runs_keep == 50
    assert loaded.verify_archive_enabled is True
    assert loaded.verify_archive_dir == ".brigade/work/verify-archive"


def test_load_config_reads_verify_retention_settings(tmp_path):
    _write_config_json(
        tmp_path,
        verify_runs_keep=10,
        verify_archive_enabled=False,
        verify_archive_dir="evidence/verify-archive",
    )
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.verify_runs_keep == 10
    assert loaded.verify_archive_enabled is False
    assert loaded.verify_archive_dir == "evidence/verify-archive"


def test_write_then_load_round_trip_preserves_verify_retention_settings(tmp_path):
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    cfg = Config(
        version=1,
        selection=sel,
        verify_runs_keep=10,
        verify_archive_enabled=False,
        verify_archive_dir="evidence/verify-archive",
    )
    write_config(tmp_path, cfg)

    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.verify_runs_keep == 10
    assert loaded.verify_archive_enabled is False
    assert loaded.verify_archive_dir == "evidence/verify-archive"


@pytest.mark.parametrize("value", [0, -1, "many", True, 2.5])
def test_load_config_rejects_invalid_verify_runs_keep(tmp_path, value):
    _write_config_json(tmp_path, verify_runs_keep=value)
    with pytest.raises(ValueError, match="verify_runs_keep must be a positive integer"):
        load_config(tmp_path)


@pytest.mark.parametrize("value", ["yes", 1, 0])
def test_load_config_rejects_invalid_verify_archive_enabled(tmp_path, value):
    _write_config_json(tmp_path, verify_archive_enabled=value)
    with pytest.raises(ValueError, match="verify_archive_enabled must be true or false"):
        load_config(tmp_path)


@pytest.mark.parametrize("value", ["", "   ", 123])
def test_load_config_rejects_invalid_verify_archive_dir(tmp_path, value):
    _write_config_json(tmp_path, verify_archive_dir=value)
    with pytest.raises(ValueError, match="verify_archive_dir must be a non-empty string"):
        load_config(tmp_path)


def test_resolve_verify_runs_keep_defaults_without_config(tmp_path):
    from brigade.config import resolve_verify_runs_keep

    assert resolve_verify_runs_keep(tmp_path) == 50


def test_resolve_verify_archive_defaults_without_config(tmp_path):
    from brigade.config import resolve_verify_archive

    enabled, root = resolve_verify_archive(tmp_path)
    assert enabled is True
    assert root == tmp_path / ".brigade" / "work" / "verify-archive"


def test_resolve_verify_archive_honors_absolute_dir(tmp_path):
    from brigade.config import resolve_verify_archive

    destination = tmp_path / "elsewhere" / "archive"
    _write_config_json(tmp_path, verify_archive_dir=str(destination))
    enabled, root = resolve_verify_archive(tmp_path)
    assert enabled is True
    assert root == destination


def test_load_config_defaults_run_lock_wait_seconds(tmp_path):
    _write_config_json(tmp_path)
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.run_lock_wait_seconds == 0.0


def test_load_config_reads_run_lock_wait_seconds(tmp_path):
    _write_config_json(tmp_path, run_lock_wait_seconds=120)
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.run_lock_wait_seconds == 120.0


@pytest.mark.parametrize("value", [-1, "slow", True])
def test_load_config_rejects_invalid_run_lock_wait_seconds(tmp_path, value):
    _write_config_json(tmp_path, run_lock_wait_seconds=value)
    with pytest.raises(ValueError, match="run_lock_wait_seconds must be a non-negative number"):
        load_config(tmp_path)


def test_resolve_run_lock_wait_seconds_defaults_without_config(tmp_path):
    from brigade.config import resolve_run_lock_wait_seconds

    assert resolve_run_lock_wait_seconds(tmp_path) == 0.0
