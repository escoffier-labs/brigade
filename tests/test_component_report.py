"""Tests for managed component reporting."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import brigade
from brigade import (
    cli,
    component_install,
    component_manifest,
    component_paths,
    component_report,
    component_state,
    doctor as doctor_mod,
    update_cmd,
)
from brigade.component_install import resolve_roots, setup_native_components
from brigade.install import install_selection
from brigade.selection import Selection

from tests.component_install_helpers import (
    FakeOpener,
    all_fixture_payloads,
    fixture_payload,
    linux_env,
    test_component_revision as fixture_component_revision,
    write_test_manifest,
)


def _install_fixture_manifest(tmp_path: Path, monkeypatch) -> dict[str, str]:
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    monkeypatch.setattr(component_manifest, "platform_key", lambda **_kwargs: "linux-amd64")
    isolated_bin = tmp_path / "isolated-bin"
    isolated_bin.mkdir()
    env["PATH"] = str(isolated_bin)
    for key in ("HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "PATH"):
        monkeypatch.setenv(key, env[key])
    localappdata = tmp_path / "localappdata"
    localappdata.mkdir(parents=True, exist_ok=True)
    env["LOCALAPPDATA"] = str(localappdata)
    monkeypatch.setenv("LOCALAPPDATA", env["LOCALAPPDATA"])
    return env


def _managed_paths(env: dict[str, str]) -> dict[str, Path]:
    roots = resolve_roots(env=env, system="linux")
    return {
        component_id: Path(component_paths.managed_executable_path(roots.data_root, component_id))
        for component_id in component_manifest.KNOWN_COMPONENT_IDS
    }


def _prepare_unified_release_setup(tmp_path: Path, monkeypatch) -> tuple[dict[str, str], update_cmd.ResolvedRelease]:
    """Route setup through a cached exact-release manifest fixture."""
    env = linux_env(tmp_path)
    bundled = tmp_path / "templates" / "components" / "manifest-v1.json"
    bundled.parent.mkdir(parents=True)
    write_test_manifest(bundled, brigade_version=brigade.__version__)
    manifest_bytes = bundled.read_bytes()
    release = update_cmd.ResolvedRelease(
        42,
        f"v{brigade.__version__}",
        brigade.__version__,
        "a" * 40,
        "https://github.com/escoffier-labs/brigade/releases/download/"
        f"v{brigade.__version__}/component-manifest-v1.json",
        len(manifest_bytes),
        hashlib.sha256(manifest_bytes).hexdigest(),
        manifest_bytes,
    )
    monkeypatch.setattr(component_install.templates, "template_root", lambda: bundled.parents[1])
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: bundled)
    monkeypatch.setattr(update_cmd, "resolve_release", lambda *_args, **_kwargs: release)
    monkeypatch.setattr(
        update_cmd,
        "validate_release_manifest_bytes",
        lambda resolved: component_manifest.load_bytes(resolved.manifest_bytes, source=Path(resolved.manifest_url)),
    )
    monkeypatch.setattr(component_manifest, "platform_key", lambda **_kwargs: "linux-amd64")
    return env, release


def test_default_component_report_loads_bundled_compatibility_manifest(tmp_path):
    report = component_report.inspect_components(env=linux_env(tmp_path), system="linux")

    assert report.platform_error is None
    assert report.manifest_revision == "2026-07-19"


def test_component_report_uses_verified_exact_release_manifest_after_unified_setup(tmp_path, monkeypatch):
    env, release = _prepare_unified_release_setup(tmp_path, monkeypatch)

    assert setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads())) == 0

    roots = resolve_roots(env=env, system="linux")
    cached = Path(component_paths.verified_manifest_path(roots.cache_root, release.manifest_sha256))
    report = component_report.inspect_components(env=env, system="linux")

    assert report.manifest_path == str(cached)
    assert report.state_file_status == "valid"
    assert all(component.status == "healthy" for component in report.components)


def test_component_report_ignores_stale_release_cache_after_explicit_setup(tmp_path, monkeypatch):
    env, _release = _prepare_unified_release_setup(tmp_path, monkeypatch)
    assert setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads())) == 0

    roots = resolve_roots(env=env, system="linux")
    update_state_path = Path(component_paths.update_state_path(roots.data_root))
    update_state_before = update_state_path.read_bytes()

    explicit_manifest = tmp_path / "explicit-manifest.json"
    write_test_manifest(explicit_manifest, brigade_version=brigade.__version__)
    explicit_data = json.loads(explicit_manifest.read_text())
    explicit_data["manifest_revision"] = "explicit-revision"
    explicit_data["components"]["graphtrail"]["component_revision"] = "b" * 40
    explicit_manifest.write_text(json.dumps(explicit_data))
    assert (
        setup_native_components(
            env=env,
            manifest_path=explicit_manifest,
            opener=FakeOpener(all_fixture_payloads()),
        )
        == 0
    )

    report = component_report.inspect_components(env=env, system="linux")

    assert update_state_path.read_bytes() == update_state_before
    assert report.manifest_path == str(component_manifest.manifest_path())
    assert report.manifest_revision == "fixture"
    assert report.installed_manifest_revision == "explicit-revision"


def test_component_report_ignores_stale_release_cache_after_explicit_setup_with_only_manifest_revision_changed(
    tmp_path, monkeypatch
):
    env, _release = _prepare_unified_release_setup(tmp_path, monkeypatch)
    assert setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads())) == 0

    roots = resolve_roots(env=env, system="linux")
    update_state_path = Path(component_paths.update_state_path(roots.data_root))
    update_state_before = update_state_path.read_bytes()

    explicit_manifest = tmp_path / "explicit-manifest.json"
    write_test_manifest(explicit_manifest, brigade_version=brigade.__version__)
    explicit_data = json.loads(explicit_manifest.read_text())
    explicit_data["manifest_revision"] = "explicit-revision"
    explicit_manifest.write_text(json.dumps(explicit_data))
    assert (
        setup_native_components(
            env=env,
            manifest_path=explicit_manifest,
            opener=FakeOpener(all_fixture_payloads()),
        )
        == 0
    )

    report = component_report.inspect_components(env=env, system="linux")

    assert update_state_path.read_bytes() == update_state_before
    assert report.manifest_path == str(component_manifest.manifest_path())
    assert report.manifest_revision == "fixture"
    assert report.installed_manifest_revision == "explicit-revision"


def test_component_report_without_matching_update_state_uses_standalone_manifest(tmp_path):
    env = linux_env(tmp_path)

    report = component_report.inspect_components(env=env, system="linux")

    assert report.manifest_path == str(component_manifest.manifest_path())
    assert report.manifest_revision == "2026-07-19"


def test_component_report_reports_tampered_matching_release_manifest_cache(tmp_path, monkeypatch):
    env, release = _prepare_unified_release_setup(tmp_path, monkeypatch)
    assert setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads())) == 0
    roots = resolve_roots(env=env, system="linux")
    cached = Path(component_paths.verified_manifest_path(roots.cache_root, release.manifest_sha256))
    cached.write_bytes(cached.read_bytes() + b"\n")

    report = component_report.inspect_components(env=env, system="linux")

    assert report.manifest_path == str(cached)
    assert report.manifest_schema_version is None
    assert report.platform_error == "cached exact-release manifest digest does not match update state"
    assert report.state_file_status == "valid"
    assert report.installed_manifest_revision == "fixture"
    assert report.installed_brigade_version == brigade.__version__
    assert report.installed_platform == "linux-amd64"
    assert all(component.installed_component_revision is not None for component in report.components)
    checks = component_report.doctor_checks(env=env, system="linux")
    assert checks == [
        (doctor_mod.FAIL, "components: manifest", "cached exact-release manifest digest does not match update state")
    ]


def test_component_report_matching_update_state_missing_cache(tmp_path, monkeypatch):
    env, release = _prepare_unified_release_setup(tmp_path, monkeypatch)
    assert setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads())) == 0
    roots = resolve_roots(env=env, system="linux")
    cached = Path(component_paths.verified_manifest_path(roots.cache_root, release.manifest_sha256))
    assert cached.is_file()
    cached.unlink()

    report = component_report.inspect_components(env=env, system="linux")

    assert report.manifest_path == str(cached)
    assert report.manifest_schema_version is None
    assert report.platform_error == "cached exact-release manifest is missing"
    assert report.state_file_status == "valid"
    assert report.installed_manifest_revision == "fixture"
    assert report.installed_brigade_version == brigade.__version__
    assert report.installed_platform == "linux-amd64"
    assert all(component.installed_component_revision is not None for component in report.components)
    checks = component_report.doctor_checks(env=env, system="linux")
    assert checks == [(doctor_mod.FAIL, "components: manifest", "cached exact-release manifest is missing")]


def test_component_report_mismatched_update_tag_falls_back_to_bundled(tmp_path, monkeypatch):
    env, release = _prepare_unified_release_setup(tmp_path, monkeypatch)
    assert setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads())) == 0

    roots = resolve_roots(env=env, system="linux")
    state_path = Path(component_paths.update_state_path(roots.data_root))
    assert state_path.is_file()

    state = update_cmd.load_update_state(state_path)
    assert state is not None

    mismatched_state = update_cmd.UpdateState(
        schema_version=state.schema_version,
        channel=state.channel,
        owner=state.owner,
        cli_coordinate=state.cli_coordinate,
        component_release_id=state.component_release_id,
        component_tag="v99.99.99",
        component_target_commit=state.component_target_commit,
        component_manifest_url="https://github.com/escoffier-labs/brigade/releases/download/v99.99.99/component-manifest-v1.json",
        component_manifest_sha256=state.component_manifest_sha256,
        updated_at=state.updated_at,
    )
    update_cmd.write_update_state(state_path, mismatched_state)

    report = component_report.inspect_components(env=env, system="linux")

    assert report.manifest_path == str(component_manifest.manifest_path())
    assert report.manifest_revision == "fixture"


def test_component_report_invalid_utf8_update_state_falls_back_to_bundled(tmp_path, monkeypatch):
    env, _release = _prepare_unified_release_setup(tmp_path, monkeypatch)
    assert setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads())) == 0

    roots = resolve_roots(env=env, system="linux")
    state_path = Path(component_paths.update_state_path(roots.data_root))
    state_path.write_bytes(b'{"component_tag":"\xff"}')

    report = component_report.inspect_components(env=env, system="linux")

    assert report.manifest_path == str(component_manifest.manifest_path())
    assert report.manifest_revision == "fixture"


def test_component_report_explicit_manifest_path_overrides_matching_state(tmp_path, monkeypatch):
    env, release = _prepare_unified_release_setup(tmp_path, monkeypatch)
    assert setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads())) == 0

    explicit_manifest_file = tmp_path / "explicit-manifest.json"
    write_test_manifest(explicit_manifest_file, brigade_version=brigade.__version__)

    data = json.loads(explicit_manifest_file.read_text())
    data["manifest_revision"] = "explicit-rev-123"
    explicit_manifest_file.write_text(json.dumps(data))

    report = component_report.inspect_components(env=env, system="linux", manifest_path=explicit_manifest_file)

    assert report.manifest_path == str(explicit_manifest_file)
    assert report.manifest_revision == "explicit-rev-123"


def _write_healthy_install(env: dict[str, str], manifest_path: Path) -> None:
    manifest = component_manifest.load(manifest_path)
    roots = resolve_roots(env=env, system="linux")
    components: dict[str, component_state.InstalledComponentRecord] = {}
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        asset = component_manifest.resolve_asset(manifest, component_id, "linux-amd64")
        payload, _, _ = fixture_payload(component_id)
        managed_path = Path(component_paths.managed_executable_path(roots.data_root, component_id))
        managed_path.parent.mkdir(parents=True, exist_ok=True)
        managed_path.write_bytes(payload)
        managed_path.chmod(0o755)
        components[component_id] = component_state.InstalledComponentRecord(
            component_revision=fixture_component_revision(component_id),
            asset_name=asset.asset_name,
            byte_size=asset.byte_size,
            sha256=asset.sha256,
            download_url=asset.download_url,
            executable=str(managed_path),
        )
    state = component_state.InstalledState(
        schema_version=component_state.SCHEMA_VERSION,
        brigade_version=brigade.__version__,
        manifest_revision=manifest.manifest_revision,
        platform="linux-amd64",
        installed_at="2026-07-19T12:00:00+00:00",
        components=components,
    )
    component_state.write_installed_state(
        Path(component_paths.installed_state_path(roots.data_root)),
        state,
    )


def test_status_precedence_is_documented_and_stable():
    assert component_report.STATUS_PRECEDENCE == (
        "unsupported",
        "corrupt",
        "stale",
        "missing",
        "healthy",
    )


def test_plain_version_matches_global_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    global_out = capsys.readouterr().out
    capsys.readouterr()
    assert cli.main(["version"]) == 0
    command_out = capsys.readouterr().out
    assert command_out == global_out == f"brigade {brigade.__version__}\n"


def test_version_json_requires_components():
    with pytest.raises(SystemExit) as exc:
        cli.main(["version", "--json"])
    assert exc.value.code == 2


def test_version_components_missing_state(tmp_path, monkeypatch, capsys):
    _install_fixture_manifest(tmp_path, monkeypatch)
    capsys.readouterr()
    assert cli.main(["version", "--components"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"brigade {brigade.__version__}\n")
    assert "platform: linux-amd64" in out
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        assert f"{component_id}: missing" in out


def test_version_components_json_is_single_document(tmp_path, monkeypatch, capsys):
    _install_fixture_manifest(tmp_path, monkeypatch)
    capsys.readouterr()
    assert cli.main(["version", "--components", "--json"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["brigade_version"] == brigade.__version__
    assert payload["manifest"]["schema_version"] == 1
    assert payload["platform"] == "linux-amd64"
    assert payload["status_precedence"] == list(component_report.STATUS_PRECEDENCE)
    assert len(payload["components"]) == len(component_manifest.KNOWN_COMPONENT_IDS)
    assert all(item["status"] == "missing" for item in payload["components"])
    assert all("expected_asset_name" in item for item in payload["components"])
    assert all("asset_name" not in item for item in payload["components"])


def test_healthy_install_reports_all_components(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    manifest_path = component_manifest.manifest_path()
    _write_healthy_install(env, manifest_path)
    report = component_report.inspect_components(env=env, system="linux")
    assert report.state_file_status == "valid"
    assert all(component.status == "healthy" for component in report.components)
    assert all(component.actual_byte_size is not None for component in report.components)
    assert all(component.actual_sha256 is not None for component in report.components)


def test_setup_native_then_inspect_reports_healthy(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    monkeypatch.setattr(component_manifest, "platform_key", lambda **_kwargs: "linux-amd64")
    for key in ("HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, env[key])
    assert setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads())) == 0
    report = component_report.inspect_components(env=env, system="linux")
    assert report.state_file_status == "valid"
    assert all(component.status == "healthy" for component in report.components)
    assert all(component.expected_asset_name is not None for component in report.components)


def test_healthy_install_recorded_executable_matches_setup_native_components(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    manifest_path = component_manifest.manifest_path()
    _write_healthy_install(env, manifest_path)
    roots = resolve_roots(env=env, system="linux")
    report = component_report.inspect_components(env=env, system="linux")
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        managed_path = component_paths.managed_executable_path(roots.data_root, component_id)
        component = next(item for item in report.components if item.component_id == component_id)
        assert component.recorded_executable == str(managed_path)
        assert component.status == "healthy"


def test_stale_manifest_revision(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    manifest_path = component_manifest.manifest_path()
    _write_healthy_install(env, manifest_path)
    roots = resolve_roots(env=env, system="linux")
    state_path = Path(component_paths.installed_state_path(roots.data_root))
    state = component_state.load_installed_state(state_path)
    assert state is not None
    stale = component_state.InstalledState(
        schema_version=state.schema_version,
        brigade_version=state.brigade_version,
        manifest_revision="stale-revision",
        platform=state.platform,
        installed_at=state.installed_at,
        components=state.components,
    )
    component_state.write_installed_state(state_path, stale)
    report = component_report.inspect_components(env=env, system="linux")
    assert all(component.status == "stale" for component in report.components)


def test_corrupt_invalid_installed_json(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    roots = resolve_roots(env=env, system="linux")
    state_path = Path(component_paths.installed_state_path(roots.data_root))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not-json", encoding="utf-8")
    report = component_report.inspect_components(env=env, system="linux")
    assert report.state_file_status == "corrupt"
    assert all(component.status == "corrupt" for component in report.components)


def test_corrupt_managed_executable_hash(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    manifest_path = component_manifest.manifest_path()
    _write_healthy_install(env, manifest_path)
    paths = _managed_paths(env)
    paths["graphtrail"].write_bytes(b"wrong-bytes")
    report = component_report.inspect_components(env=env, system="linux")
    by_id = {component.component_id: component for component in report.components}
    assert by_id["graphtrail"].status == "corrupt"
    assert by_id["miseledger"].status == "healthy"


def test_corrupt_managed_executable_read_error(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    manifest_path = component_manifest.manifest_path()
    _write_healthy_install(env, manifest_path)

    def raise_os_error(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(component_report.localio, "file_sha256", raise_os_error)
    report = component_report.inspect_components(env=env, system="linux")
    assert all(component.status == "corrupt" for component in report.components)
    assert all("permission denied" in component.detail for component in report.components)


def test_unsupported_platform(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    monkeypatch.setattr(
        component_manifest,
        "platform_key",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("unsupported platform solaris-sparc")),
    )
    report = component_report.inspect_components(env=env, system="linux")
    assert report.platform is None
    assert all(component.status == "unsupported" for component in report.components)


def test_inspect_components_unsupported_system(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    report = component_report.inspect_components(env=env, system="solaris")
    assert report.platform is None
    assert "unsupported component path platform system" in (report.platform_error or "")
    assert len(report.components) == len(component_manifest.KNOWN_COMPONENT_IDS)
    assert all(component.status == "unsupported" for component in report.components)


def test_inspect_components_missing_data_root(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    env = {key: value for key, value in env.items() if key != "HOME"}
    report = component_report.inspect_components(env=env, system="linux")
    assert report.platform is None
    assert report.platform_error == "component data root requires HOME"
    assert report.state_file_status == "missing"
    assert len(report.components) == len(component_manifest.KNOWN_COMPONENT_IDS)
    assert all(component.status == "unsupported" for component in report.components)


def test_unsupported_asset_for_platform(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    original_resolve = component_manifest.resolve_asset

    def fake_resolve(manifest, component_id, platform):
        if component_id == "graphtrail":
            raise ValueError("unsupported-component-platform: component 'graphtrail' has no pinned native asset")
        return original_resolve(manifest, component_id, platform)

    monkeypatch.setattr(component_manifest, "resolve_asset", fake_resolve)
    report = component_report.inspect_components(env=env, system="linux")
    by_id = {component.component_id: component for component in report.components}
    assert by_id["graphtrail"].status == "unsupported"
    assert by_id["miseledger"].status == "missing"


def test_path_binary_is_reported_without_satisfying_install(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    path_bin = tmp_path / "bin"
    path_bin.mkdir()
    payload, _, _ = fixture_payload("graphtrail")
    shim = path_bin / "graphtrail"
    shim.write_bytes(payload)
    shim.chmod(0o755)
    env = dict(env)
    env["PATH"] = f"{path_bin}{os.pathsep}{env.get('PATH', '')}"
    report = component_report.inspect_components(env=env, system="linux")
    graphtrail = next(item for item in report.components if item.component_id == "graphtrail")
    assert graphtrail.status == "missing"
    assert graphtrail.path_binary == str(shim.resolve())


def test_path_binary_oserror_returns_none(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)

    def raise_os_error(*_args, **_kwargs):
        raise OSError("bad path resolution")

    monkeypatch.setattr(component_report.shutil, "which", raise_os_error)
    report = component_report.inspect_components(env=env, system="linux")
    assert all(component.path_binary is None for component in report.components)


def test_precedence_corrupt_before_stale(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    manifest_path = component_manifest.manifest_path()
    _write_healthy_install(env, manifest_path)
    paths = _managed_paths(env)
    paths["graphtrail"].write_bytes(b"tampered")
    roots = resolve_roots(env=env, system="linux")
    state_path = Path(component_paths.installed_state_path(roots.data_root))
    state = component_state.load_installed_state(state_path)
    assert state is not None
    stale = component_state.InstalledState(
        schema_version=state.schema_version,
        brigade_version=state.brigade_version,
        manifest_revision="stale-revision",
        platform=state.platform,
        installed_at=state.installed_at,
        components=state.components,
    )
    component_state.write_installed_state(state_path, stale)
    report = component_report.inspect_components(env=env, system="linux")
    graphtrail = next(item for item in report.components if item.component_id == "graphtrail")
    assert graphtrail.status == "corrupt"


def test_stale_asset_name(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    manifest_path = component_manifest.manifest_path()
    _write_healthy_install(env, manifest_path)
    roots = resolve_roots(env=env, system="linux")
    state_path = Path(component_paths.installed_state_path(roots.data_root))
    state = component_state.load_installed_state(state_path)
    assert state is not None
    graphtrail = state.components["graphtrail"]
    stale_components = dict(state.components)
    stale_components["graphtrail"] = component_state.InstalledComponentRecord(
        component_revision=graphtrail.component_revision,
        asset_name="graphtrail-linux-amd64-stale",
        byte_size=graphtrail.byte_size,
        sha256=graphtrail.sha256,
        download_url=graphtrail.download_url,
        executable=graphtrail.executable,
    )
    stale = component_state.InstalledState(
        schema_version=state.schema_version,
        brigade_version=state.brigade_version,
        manifest_revision=state.manifest_revision,
        platform=state.platform,
        installed_at=state.installed_at,
        components=stale_components,
    )
    component_state.write_installed_state(state_path, stale)
    report = component_report.inspect_components(env=env, system="linux")
    graphtrail_report = next(item for item in report.components if item.component_id == "graphtrail")
    assert graphtrail_report.status == "stale"
    assert graphtrail_report.installed_asset_name == "graphtrail-linux-amd64-stale"
    assert "installed asset name" in graphtrail_report.detail


def test_stale_recorded_executable(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    manifest_path = component_manifest.manifest_path()
    _write_healthy_install(env, manifest_path)
    roots = resolve_roots(env=env, system="linux")
    state_path = Path(component_paths.installed_state_path(roots.data_root))
    state = component_state.load_installed_state(state_path)
    assert state is not None
    graphtrail = state.components["graphtrail"]
    stale_components = dict(state.components)
    stale_components["graphtrail"] = component_state.InstalledComponentRecord(
        component_revision=graphtrail.component_revision,
        asset_name=graphtrail.asset_name,
        byte_size=graphtrail.byte_size,
        sha256=graphtrail.sha256,
        download_url=graphtrail.download_url,
        executable="graphtrail-stale",
    )
    stale = component_state.InstalledState(
        schema_version=state.schema_version,
        brigade_version=state.brigade_version,
        manifest_revision=state.manifest_revision,
        platform=state.platform,
        installed_at=state.installed_at,
        components=stale_components,
    )
    component_state.write_installed_state(state_path, stale)
    report = component_report.inspect_components(env=env, system="linux")
    graphtrail_report = next(item for item in report.components if item.component_id == "graphtrail")
    assert graphtrail_report.status == "stale"
    assert graphtrail_report.recorded_executable == "graphtrail-stale"
    assert "recorded executable" in graphtrail_report.detail


def test_doctor_component_checks_human_and_json(tmp_target: Path, tmp_path, monkeypatch, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    manifest_path = component_manifest.manifest_path()
    _write_healthy_install(env, manifest_path)
    checks = component_report.doctor_checks(env=env, system="linux")
    assert all(status == doctor_mod.OK for status, _name, _detail in checks)
    capsys.readouterr()
    assert doctor_mod.run(target=tmp_target, harness="generic", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    component_checks = [item for item in payload["checks"] if item["name"].startswith("components:")]
    assert component_checks
    assert all(item["status"] == doctor_mod.OK for item in component_checks)
    assert all(item["scope"] == "machine" for item in component_checks)


def test_doctor_missing_is_manual_and_stale_is_warn(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    checks = component_report.doctor_checks(env=env, system="linux")
    assert all(status == doctor_mod.MANUAL for status, _name, _detail in checks)

    manifest_path = component_manifest.manifest_path()
    _write_healthy_install(env, manifest_path)
    roots = resolve_roots(env=env, system="linux")
    state_path = Path(component_paths.installed_state_path(roots.data_root))
    state = component_state.load_installed_state(state_path)
    assert state is not None
    stale = component_state.InstalledState(
        schema_version=state.schema_version,
        brigade_version=state.brigade_version,
        manifest_revision="stale-revision",
        platform=state.platform,
        installed_at=state.installed_at,
        components=state.components,
    )
    component_state.write_installed_state(state_path, stale)
    checks = component_report.doctor_checks(env=env, system="linux")
    assert all(status == doctor_mod.WARN for status, _name, _detail in checks)


def test_doctor_corrupt_is_fail(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    roots = resolve_roots(env=env, system="linux")
    state_path = Path(component_paths.installed_state_path(roots.data_root))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text('{"schema_version": "bad"}', encoding="utf-8")
    checks = component_report.doctor_checks(env=env, system="linux")
    assert all(status == doctor_mod.FAIL for status, _name, _detail in checks)


def test_doctor_unsupported_platform_is_advisory(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    monkeypatch.setattr(
        component_manifest,
        "platform_key",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("unsupported platform solaris-sparc")),
    )
    checks = component_report.doctor_checks(env=env, system="linux")
    platform_checks = [item for item in checks if item[1] == "components: platform"]
    component_checks = [
        item for item in checks if item[1].startswith("components:") and item[1] != "components: platform"
    ]
    assert platform_checks and platform_checks[0][0] == doctor_mod.INFO
    assert all(status == doctor_mod.INFO for status, _name, _detail in component_checks)


def test_doctor_missing_data_root_is_advisory(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    env = {key: value for key, value in env.items() if key != "HOME"}
    checks = component_report.doctor_checks(env=env, system="linux")
    component_checks = [item for item in checks if item[1].startswith("components:")]
    assert component_checks
    assert all(status == doctor_mod.INFO for status, _name, _detail in component_checks)


def test_doctor_invalid_managed_platform_state_fails(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    manifest_path = component_manifest.manifest_path()
    _write_healthy_install(env, manifest_path)
    roots = resolve_roots(env=env, system="linux")
    state_path = Path(component_paths.installed_state_path(roots.data_root))
    state = component_state.load_installed_state(state_path)
    assert state is not None
    wrong_platform = component_state.InstalledState(
        schema_version=state.schema_version,
        brigade_version=state.brigade_version,
        manifest_revision=state.manifest_revision,
        platform="darwin-amd64",
        installed_at=state.installed_at,
        components=state.components,
    )
    component_state.write_installed_state(state_path, wrong_platform)
    checks = component_report.doctor_checks(env=env, system="linux")
    assert all(status == doctor_mod.FAIL for status, _name, _detail in checks if _name.startswith("components:"))


def test_doctor_detail_includes_expected_and_installed_revisions(tmp_path, monkeypatch):
    env = _install_fixture_manifest(tmp_path, monkeypatch)
    checks = component_report.doctor_checks(env=env, system="linux")
    assert checks
    _status, _name, detail = checks[0]
    assert "expected revision" in detail
    assert "installed revision none" in detail
