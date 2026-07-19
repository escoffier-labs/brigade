"""Tests for installed component state schema v1."""

from __future__ import annotations

import json

import pytest

from brigade.component_state import (
    InstalledComponentRecord,
    InstalledState,
    load_installed_state,
    render_installed_state,
    should_rotate_previous,
    state_digest_map,
)


def _minimal_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "component_revision": "v0.6.0",
        "asset_name": "miseledger-linux-amd64",
        "byte_size": 16441315,
        "sha256": "a" * 64,
        "download_url": "https://example.invalid/miseledger",
        "executable": "/tmp/xdg-data/brigade/bin/miseledger",
    }
    record.update(overrides)
    return record


def _minimal_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "brigade_version": "0.23.0",
        "manifest_revision": "2026-07-18",
        "platform": "linux-amd64",
        "installed_at": "2026-07-19T06:00:00+00:00",
        "components": {"miseledger": _minimal_record()},
    }
    payload.update(overrides)
    return payload


def _write_installed_state(path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload))


def _sample_state(
    *,
    sha256: str = "a" * 64,
    manifest_revision: str = "2026-07-18",
    installed_at: str = "2026-07-19T06:00:00+00:00",
) -> InstalledState:
    return InstalledState(
        schema_version=1,
        brigade_version="0.23.0",
        manifest_revision=manifest_revision,
        platform="linux-amd64",
        installed_at=installed_at,
        components={
            "miseledger": InstalledComponentRecord(
                component_revision="v0.6.0",
                asset_name="miseledger-linux-amd64",
                byte_size=16441315,
                sha256=sha256,
                download_url="https://github.com/escoffier-labs/miseledger/releases/download/v0.6.0/miseledger-linux-amd64",
                executable="/tmp/xdg-data/brigade/bin/miseledger",
            )
        },
    )


def test_render_installed_state_round_trips_required_fields():
    state = InstalledState(
        schema_version=1,
        brigade_version="0.23.0",
        manifest_revision="2026-07-19",
        platform="linux-amd64",
        installed_at="2026-07-19T06:00:00+00:00",
        components={
            "miseledger": InstalledComponentRecord(
                component_revision="v0.6.0",
                asset_name="miseledger-linux-amd64",
                byte_size=16441315,
                sha256="246893c8c39318f774fc7a06338b5a8e87bf84661b1951251b2c0c971e9a7a6c",
                download_url="https://github.com/escoffier-labs/miseledger/releases/download/v0.6.0/miseledger-linux-amd64",
                executable="/tmp/xdg-data/brigade/bin/miseledger",
            )
        },
    )
    payload = render_installed_state(state)
    assert payload["schema_version"] == 1
    assert payload["brigade_version"] == "0.23.0"
    assert payload["manifest_revision"] == "2026-07-19"
    assert payload["platform"] == "linux-amd64"
    assert payload["installed_at"] == "2026-07-19T06:00:00+00:00"
    assert payload["components"]["miseledger"]["executable"].endswith("/brigade/bin/miseledger")
    assert (
        payload["components"]["miseledger"]["sha256"]
        == "246893c8c39318f774fc7a06338b5a8e87bf84661b1951251b2c0c971e9a7a6c"
    )


def test_load_installed_state_round_trips(tmp_path):
    state = InstalledState(
        schema_version=1,
        brigade_version="0.23.0",
        manifest_revision="2026-07-18",
        platform="linux-amd64",
        installed_at="2026-07-19T06:00:00+00:00",
        components={
            "miseledger": InstalledComponentRecord(
                component_revision="v0.6.0",
                asset_name="miseledger-linux-amd64",
                byte_size=16441315,
                sha256="a" * 64,
                download_url="https://example.invalid/miseledger",
                executable="/tmp/xdg-data/brigade/bin/miseledger",
            )
        },
    )
    path = tmp_path / "installed.json"
    path.write_text(
        """{
  "schema_version": 1,
  "brigade_version": "0.23.0",
  "manifest_revision": "2026-07-18",
  "platform": "linux-amd64",
  "installed_at": "2026-07-19T06:00:00+00:00",
  "components": {
    "miseledger": {
      "component_revision": "v0.6.0",
      "asset_name": "miseledger-linux-amd64",
      "byte_size": 16441315,
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "download_url": "https://example.invalid/miseledger",
      "executable": "/tmp/xdg-data/brigade/bin/miseledger"
    }
  }
}"""
    )
    loaded = load_installed_state(path)
    assert loaded is not None
    assert loaded == state


def test_load_installed_state_missing_returns_none(tmp_path):
    path = tmp_path / "installed.json"
    assert load_installed_state(path) is None


def test_load_installed_state_invalid_json_returns_none(tmp_path):
    path = tmp_path / "installed.json"
    path.write_text("not json")
    assert load_installed_state(path) is None


def test_load_installed_state_top_level_type_mismatch_returns_none(tmp_path):
    path = tmp_path / "installed.json"
    path.write_text("[]")
    assert load_installed_state(path) is None


def test_load_installed_state_missing_required_field_returns_none(tmp_path):
    path = tmp_path / "installed.json"
    path.write_text(
        """{
  "schema_version": 1,
  "brigade_version": "0.23.0",
  "platform": "linux-amd64",
  "installed_at": "2026-07-19T06:00:00+00:00",
  "components": {}
}"""
    )
    assert load_installed_state(path) is None


def test_load_installed_state_bad_component_record_returns_none(tmp_path):
    path = tmp_path / "installed.json"
    path.write_text(
        """{
  "schema_version": 1,
  "brigade_version": "0.23.0",
  "manifest_revision": "2026-07-18",
  "platform": "linux-amd64",
  "installed_at": "2026-07-19T06:00:00+00:00",
  "components": {
    "miseledger": {
      "component_revision": "v0.6.0",
      "asset_name": "miseledger-linux-amd64",
      "byte_size": "not-an-int",
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "download_url": "https://example.invalid/miseledger",
      "executable": "/tmp/xdg-data/brigade/bin/miseledger"
    }
  }
}"""
    )
    assert load_installed_state(path) is None


def test_load_installed_state_bad_schema_version_returns_none(tmp_path):
    path = tmp_path / "installed.json"
    path.write_text(
        """{
  "schema_version": 2,
  "brigade_version": "0.23.0",
  "manifest_revision": "2026-07-18",
  "platform": "linux-amd64",
  "installed_at": "2026-07-19T06:00:00+00:00",
  "components": {}
}"""
    )
    assert load_installed_state(path) is None


def test_load_installed_state_components_not_dict_returns_none(tmp_path):
    path = tmp_path / "installed.json"
    path.write_text(
        """{
  "schema_version": 1,
  "brigade_version": "0.23.0",
  "manifest_revision": "2026-07-18",
  "platform": "linux-amd64",
  "installed_at": "2026-07-19T06:00:00+00:00",
  "components": []
}"""
    )
    assert load_installed_state(path) is None


def test_load_installed_state_empty_components_ok(tmp_path):
    path = tmp_path / "installed.json"
    _write_installed_state(path, _minimal_payload(components={}))
    loaded = load_installed_state(path)
    assert loaded is not None
    assert loaded.components == {}


def test_load_installed_state_rejects_unexpected_top_level_key(tmp_path):
    path = tmp_path / "installed.json"
    payload = _minimal_payload()
    payload["extra"] = "nope"
    _write_installed_state(path, payload)
    assert load_installed_state(path) is None


def test_load_installed_state_rejects_unexpected_record_key(tmp_path):
    path = tmp_path / "installed.json"
    record = _minimal_record()
    record["extra"] = "nope"
    _write_installed_state(path, _minimal_payload(components={"miseledger": record}))
    assert load_installed_state(path) is None


@pytest.mark.parametrize("schema_version", [True, "1", 1.0])
def test_load_installed_state_rejects_invalid_schema_version_type(tmp_path, schema_version):
    path = tmp_path / "installed.json"
    _write_installed_state(path, _minimal_payload(schema_version=schema_version))
    assert load_installed_state(path) is None


@pytest.mark.parametrize(
    "field",
    ["brigade_version", "manifest_revision", "installed_at"],
)
def test_load_installed_state_rejects_empty_top_level_strings(tmp_path, field):
    path = tmp_path / "installed.json"
    _write_installed_state(path, _minimal_payload(**{field: "   "}))
    assert load_installed_state(path) is None


def test_load_installed_state_rejects_invalid_platform(tmp_path):
    path = tmp_path / "installed.json"
    _write_installed_state(path, _minimal_payload(platform="freebsd-amd64"))
    assert load_installed_state(path) is None


@pytest.mark.parametrize("installed_at", ["not-a-timestamp", ""])
def test_load_installed_state_rejects_invalid_installed_at(tmp_path, installed_at):
    path = tmp_path / "installed.json"
    _write_installed_state(path, _minimal_payload(installed_at=installed_at))
    assert load_installed_state(path) is None


def test_load_installed_state_accepts_zulu_installed_at(tmp_path):
    path = tmp_path / "installed.json"
    _write_installed_state(path, _minimal_payload(installed_at="2026-07-19T06:00:00.000000Z"))
    loaded = load_installed_state(path)
    assert loaded is not None
    assert loaded.installed_at == "2026-07-19T06:00:00.000000Z"


def test_load_installed_state_rejects_unknown_component_id(tmp_path):
    path = tmp_path / "installed.json"
    _write_installed_state(path, _minimal_payload(components={"unknown-tool": _minimal_record()}))
    assert load_installed_state(path) is None


def test_load_installed_state_rejects_empty_component_id(tmp_path):
    path = tmp_path / "installed.json"
    _write_installed_state(path, _minimal_payload(components={"": _minimal_record()}))
    assert load_installed_state(path) is None


@pytest.mark.parametrize("byte_size", [True, 0, -1])
def test_load_installed_state_rejects_invalid_byte_size(tmp_path, byte_size):
    path = tmp_path / "installed.json"
    record = _minimal_record(byte_size=byte_size)
    _write_installed_state(path, _minimal_payload(components={"miseledger": record}))
    assert load_installed_state(path) is None


@pytest.mark.parametrize(
    "sha256",
    ["A" * 64, "abc", "a" * 63, ""],
)
def test_load_installed_state_rejects_malformed_sha256(tmp_path, sha256):
    path = tmp_path / "installed.json"
    record = _minimal_record(sha256=sha256)
    _write_installed_state(path, _minimal_payload(components={"miseledger": record}))
    assert load_installed_state(path) is None


@pytest.mark.parametrize(
    "field",
    ["component_revision", "asset_name", "download_url", "executable"],
)
def test_load_installed_state_rejects_empty_record_strings(tmp_path, field):
    path = tmp_path / "installed.json"
    record = _minimal_record(**{field: "   "})
    _write_installed_state(path, _minimal_payload(components={"miseledger": record}))
    assert load_installed_state(path) is None


def test_load_installed_state_single_component_does_not_require_all_ids(tmp_path):
    path = tmp_path / "installed.json"
    _write_installed_state(
        path, _minimal_payload(components={"sessionfind": _minimal_record(asset_name="sessionfind-linux-amd64")})
    )
    loaded = load_installed_state(path)
    assert loaded is not None
    assert set(loaded.components) == {"sessionfind"}


def test_state_digest_map_hashes_components():
    state = _sample_state(sha256="b" * 64)
    digest_map = state_digest_map(state)
    assert digest_map == {"miseledger": "b" * 64}


def test_state_digest_map_ignores_non_digest_fields():
    state_a = _sample_state(sha256="b" * 64)
    state_b = InstalledState(
        schema_version=state_a.schema_version,
        brigade_version=state_a.brigade_version,
        manifest_revision=state_a.manifest_revision,
        platform=state_a.platform,
        installed_at=state_a.installed_at,
        components={
            "miseledger": InstalledComponentRecord(
                component_revision="different",
                asset_name=state_a.components["miseledger"].asset_name,
                byte_size=state_a.components["miseledger"].byte_size + 1,
                sha256="b" * 64,
                download_url=state_a.components["miseledger"].download_url,
                executable=state_a.components["miseledger"].executable,
            )
        },
    )
    assert state_digest_map(state_a) == state_digest_map(state_b)


def test_should_rotate_previous_when_digest_changes():
    current = _sample_state(sha256="a" * 64)
    nxt = _sample_state(sha256="b" * 64)
    assert should_rotate_previous(current, nxt) is True


def test_should_rotate_previous_when_manifest_revision_changes():
    current = _sample_state(manifest_revision="2026-07-18")
    nxt = _sample_state(manifest_revision="2026-07-19")
    assert should_rotate_previous(current, nxt) is True


def test_should_not_rotate_previous_on_identical_digest_map():
    current = _sample_state(sha256="a" * 64)
    nxt = _sample_state(sha256="a" * 64, manifest_revision=current.manifest_revision)
    assert should_rotate_previous(current, nxt) is False


def test_should_not_rotate_previous_when_no_current_state():
    nxt = _sample_state(sha256="a" * 64)
    assert should_rotate_previous(None, nxt) is False


def test_should_rotate_previous_when_component_set_changes():
    current = _sample_state(sha256="a" * 64)
    nxt = _sample_state(sha256="a" * 64)
    nxt.components["graphtrail"] = InstalledComponentRecord(
        component_revision="sha",
        asset_name="graphtrail-linux-amd64",
        byte_size=1,
        sha256="c" * 64,
        download_url="https://example.invalid/graphtrail",
        executable="/tmp/xdg-data/brigade/bin/graphtrail",
    )
    assert should_rotate_previous(current, nxt) is True


def test_should_rotate_previous_empty_next_different_from_current():
    current = _sample_state(sha256="a" * 64)
    nxt = InstalledState(
        schema_version=current.schema_version,
        brigade_version=current.brigade_version,
        manifest_revision=current.manifest_revision,
        platform=current.platform,
        installed_at=current.installed_at,
        components={},
    )
    assert should_rotate_previous(current, nxt) is True


def test_installed_state_records_are_frozen():
    record = InstalledComponentRecord(
        component_revision="v0.6.0",
        asset_name="miseledger-linux-amd64",
        byte_size=1,
        sha256="a" * 64,
        download_url="https://example.invalid",
        executable="/tmp/xdg-data/brigade/bin/miseledger",
    )
    with pytest.raises(AttributeError):
        record.sha256 = "b" * 64


def test_installed_state_is_frozen():
    state = _sample_state()
    with pytest.raises(AttributeError):
        state.manifest_revision = "2026-07-20"
