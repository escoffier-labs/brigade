"""Linux installer acceptance with current engines and checksum-pinned v0.30 assets.

CI supplies BRIGADE_R5_NATIVE_BIN and BRIGADE_R5_V030_ASSETS. Every process gets
an isolated home and repository. Published release bytes are never rewritten.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from brigade import __version__, component_install, component_manifest, component_paths, component_state, engine_paths

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = component_manifest.COMPONENT_FAMILIES


@pytest.fixture
def acceptance(tmp_path, monkeypatch):
    native = os.environ.get("BRIGADE_R5_NATIVE_BIN")
    old = os.environ.get("BRIGADE_R5_V030_ASSETS")
    if sys.platform != "linux" or not native or not old:
        pytest.skip("Linux native installer fixtures supplied by the installer CI job")
    old = Path(old)
    checksums = dict(line.split()[::-1] for line in (old / "checksums.txt").read_text().splitlines())
    for path in old.iterdir():
        if path.name != "checksums.txt":
            assert hashlib.sha256(path.read_bytes()).hexdigest() == checksums[path.name]
            if path.suffix != ".json":
                path.chmod(0o755)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home")}
    for kind in ("data", "config", "cache"):
        env[f"XDG_{kind.upper()}_HOME"] = str(tmp_path / kind)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    Path(env["HOME"]).mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / "sample.py").write_text("def example():\n    return 42\n")
    archive = subprocess.check_output(["git", "archive", "v0.30.0", "src/brigade"], cwd=ROOT)
    old_python = tmp_path / "old-python"
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(old_python, filter="data")
    return tmp_path, Path(native), old, env, repo, old_python / "src"


def run(argv, env, repo, *, input=None):
    result = subprocess.run(
        [str(a) for a in argv], env=env, cwd=repo, text=True, input=input, capture_output=True, timeout=90
    )
    assert result.returncode == 0, (argv, result.stdout, result.stderr)
    return result


def current_manifest(acceptance):
    root, native, old, env, repo, _ = acceptance
    payload = json.loads((old / "component-manifest-v1.json").read_bytes())
    payload["brigade_version"] = __version__
    payload["manifest_revision"] = "installer-current"
    bodies = {}
    for key in list(payload["components"]):
        canonical = component_manifest.family_id(key)
        record = payload["components"].pop(key)
        record["executable"] = canonical
        source = native / canonical if key in FAMILIES else old / f"{key}-linux-amd64"
        body = source.read_bytes()
        for platform, asset in record["assets"].items():
            asset["asset_name"] = asset["asset_name"].replace(key, canonical, 1)
            asset["download_url"] = "https://example.invalid/" + asset["asset_name"]
            if platform == "linux-amd64":
                asset.update(byte_size=len(body), sha256=hashlib.sha256(body).hexdigest())
                bodies[asset["download_url"]] = body
        payload["components"][canonical] = record
    path = root / "current.json"
    path.write_text(json.dumps(payload))
    return path, bodies


def setup(acceptance):
    _, _, _, env, repo, _ = acceptance
    path, bodies = current_manifest(acceptance)

    def runner(argv, **kwargs):
        return subprocess.run(argv, env=env, cwd=repo, **kwargs)

    assert (
        component_install.setup_native_components(
            manifest_path=path, env=env, opener=lambda url: io.BytesIO(bodies[url]), runner=runner
        )
        == 0
    )
    data_root = component_paths.data_root(env=env)
    installed = component_state.load_installed_state(Path(component_paths.installed_state_path(data_root)))
    assert set(installed.components) == set(component_manifest.DEFAULT_COMPONENT_IDS)
    return {key: Path(record.executable) for key, record in installed.components.items()}


def initialize(binary, env, repo, *args):
    result = run(
        [binary, *args],
        env,
        repo,
        input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n",
    )
    name = Path(binary).name
    expected = f"{name} is deprecated; use {FAMILIES[name]}; removed in v0.33.0\n" if name in FAMILIES else ""
    assert result.stderr == expected
    return json.loads(result.stdout.splitlines()[0])["result"]["serverInfo"]["name"]


def test_fresh_install_and_canonical_storage(acceptance):
    _, _, _, env, repo, _ = acceptance
    paths = setup(acceptance)
    run([paths["brigade-code-graph"], "sync"], env, repo)
    assert (repo / engine_paths.CODE_GRAPH_DB).is_file()
    assert not (repo / engine_paths.LEGACY_CODE_GRAPH_DB.parent).exists()
    assert ".brigade/code-graph" in (repo / ".gitignore").read_text()
    run([paths["brigade-evidence"], "init"], env, repo)
    fixture = ROOT / "engines/evidence-ledger/testdata/adapters/discrawl.fixture.jsonl"
    run([paths["brigade-evidence"], "import", "adapter", fixture, "--source", "discrawl", "--json"], env, repo)
    run([paths["brigade-evidence"], "evidence", "fixture", "--json"], env, repo)
    dirs = engine_paths.evidence_dirs(env=env)
    assert dirs.mac_key_path.is_file()
    from brigade import component_report

    manifest_path, _ = current_manifest(acceptance)
    assert all(
        row[0] not in {"WARN", "FAIL"} for row in component_report.doctor_checks(env=env, manifest_path=manifest_path)
    )
    assert dirs.db_path.is_file()
    assert not (Path(env["XDG_DATA_HOME"]) / engine_paths.LEGACY_EVIDENCE_LEAF).exists()
    assert initialize(paths["brigade-code-graph-mcp"], env, repo) == "brigade-code-graph"
    assert initialize(paths["brigade-evidence"], env, repo, "mcp") == "brigade-evidence"
    for legacy, canonical in FAMILIES.items():
        shim = paths[canonical].with_name(legacy)
        assert os.path.samefile(paths[canonical], shim)
        if canonical.endswith("-mcp"):
            assert initialize(shim, env, repo) == legacy.removesuffix("-mcp")
        else:
            flag = "version" if canonical == "brigade-evidence" else "--version"
            result = run([shim, flag], env, repo)
            assert result.stdout.startswith(legacy + " ")
            assert result.stderr == f"{legacy} is deprecated; use {canonical}; removed in v0.33.0\n"


def test_upgrade_legacy_data_downgrade_and_rollback(acceptance, monkeypatch):
    root, native, old, env, repo, old_python = acceptance
    old_env = {**env, "PYTHONPATH": str(old_python)}
    # The old CLI installs exactly its published manifest from an isolated cache.
    payload = json.loads((old / "component-manifest-v1.json").read_bytes())
    cache_root = component_paths.cache_root(env=env)
    for record in payload["components"].values():
        asset = record["assets"]["linux-amd64"]
        cache = Path(component_paths.cached_asset_path(cache_root, asset["sha256"], asset["asset_name"]))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes((old / asset["asset_name"]).read_bytes())
    old_cli = [sys.executable, "-m", "brigade"]
    run([*old_cli, "setup", "--manifest", old / "component-manifest-v1.json", "--offline"], old_env, repo)
    legacy_graph, legacy_evidence = tuple(FAMILIES)[0], tuple(FAMILIES)[2]
    run([old / f"{legacy_graph}-linux-amd64", "sync"], env, repo)
    evidence = old / f"{legacy_evidence}-linux-amd64"
    run([evidence, "init"], env, repo)
    fixture = ROOT / "engines/evidence-ledger/testdata/adapters/discrawl.fixture.jsonl"
    run([evidence, "import", "adapter", fixture, "--source", "discrawl", "--json"], env, repo)
    bundle = json.loads(run([evidence, "evidence", "fixture", "--json"], env, repo).stdout)
    bundle_id = bundle["id"]
    legacy_dirs = engine_paths.evidence_dirs(env=env)
    protected = {path: path.read_bytes() for path in (legacy_dirs.mac_key_path,)}
    from brigade import authority_broker, cli, localio, mcp_cmd, receipt_signing, receipts_cmd
    from tests.test_receipts_cmd import _write_verify_export_receipt

    receipt_signing.generate_key(repo)
    key, key_id = receipt_signing.load_key(repo)
    for index, field in enumerate(("code_graph_delta", next(iter(FAMILIES)) + "_delta")):
        receipt = _write_verify_export_receipt(
            repo,
            f"fixture-{index}",
            started_at="2026-01-01T00:00:00Z",
            code_graph_delta={"status": "skipped", "reason": "fixture"},
        )
        data = json.loads(receipt.read_text())
        data[field] = data.pop("code_graph_delta")
        digest = localio.canonical_json_digest(data, exclude_keys={"digests"})
        data["digests"].update(receipt_sha256=digest, signature=receipt_signing.sign(digest, key), key_id=key_id)
        localio.write_json(receipt, data)
        protected[receipt] = receipt.read_bytes()
    export = root / "receipts.jsonl"
    assert cli.main(["receipts", "export", "evidence", "--target", str(repo), "--out", str(export), "--new-only"]) == 0
    cursor = next((repo / ".brigade" / "work").glob("*-export-cursor.json"))
    protected[cursor] = cursor.read_bytes()
    assert mcp_cmd.init(target=repo, json_output=True) == 0
    for legacy, canonical in FAMILIES.items():
        if canonical.endswith("-mcp") or canonical == "brigade-evidence":
            name = legacy.removesuffix("-mcp")
            assert mcp_cmd.add(target=repo, name=name, command=legacy, json_output=True) == 0
    assert mcp_cmd.sync(target=repo, harness="claude", write=True, json_output=True) == 0
    rows = json.loads(
        run([evidence, "sql", "select id, content_hash from items order by id", "--json"], env, repo).stdout
    )["rows"]
    item = rows[0]

    def review(binary):
        secret = authority_broker.new_capability_secret()
        cap = authority_broker.mint_trust_capability(
            secret,
            item_id=item["id"],
            from_digest=item["content_hash"].removeprefix("sha256:"),
            to_label="reviewed",
            mark_injection_clean=True,
        )
        return run(
            [
                binary,
                "trust",
                "review",
                "--item",
                item["id"],
                "--content-hash",
                item["content_hash"],
                "--to-label",
                "reviewed",
                "--mark-injection-clean",
                "--json",
            ],
            env,
            repo,
            input=authority_broker.encode_handoff(secret, cap).decode("ascii"),
        )

    review(evidence)
    old_show = json.loads(run([evidence, "evidence", "show", bundle_id, "--json"], env, repo).stdout)
    paths = setup(acceptance)
    monkeypatch.setenv("BRIGADE_EVIDENCE_BIN", str(paths["brigade-evidence"]))
    assert receipts_cmd.verify(target=repo, json_output=True) == 0
    assert cli.main(["receipts", "export", "evidence", "--target", str(repo), "--out", str(export), "--new-only"]) == 0
    assert export.read_bytes() == b""
    assert mcp_cmd.sync(target=repo, harness="claude", write=True, json_output=True) == 0
    projected = json.loads((repo / ".mcp.json").read_text())["mcpServers"]
    for legacy, canonical in FAMILIES.items():
        if canonical.endswith("-mcp") or canonical == "brigade-evidence":
            assert projected[legacy.removesuffix("-mcp")]["command"] == str(paths[canonical])
    assert (
        json.loads(
            run(
                [paths["brigade-evidence"], "sql", "select id, content_hash from items order by id", "--json"],
                env,
                repo,
            ).stdout
        )["rows"]
        == rows
    )
    review(paths["brigade-evidence"])
    assert not (repo / engine_paths.CODE_GRAPH_DB).exists()
    assert engine_paths.evidence_dirs(env=env).source == "legacy"
    run([paths["brigade-code-graph"], "stats", "--json"], env, repo)
    shown = json.loads(run([paths["brigade-evidence"], "evidence", "show", bundle_id, "--json"], env, repo).stdout)
    assert shown["id"] == bundle_id
    # Show regenerates observation times, while all signed and identity fields stay stable.
    volatile = {"generated_at", "regenerated_at"}
    assert {k: v for k, v in shown.items() if k not in volatile} == {
        k: v for k, v in old_show.items() if k not in volatile
    }
    for path, body in protected.items():
        assert path.read_bytes() == body
    # The actual old reader treats canonical state as missing without crashing.
    code = "from pathlib import Path; from brigade.component_state import load_installed_state; import sys; assert load_installed_state(Path(sys.argv[1])) is None"
    state_path = Path(component_paths.installed_state_path(component_paths.data_root(env=env)))
    run([sys.executable, "-c", code, state_path], old_env, repo)
    assert (
        component_install.setup_native_components(
            env=env,
            rollback=True,
            offline=True,
            runner=lambda argv, **kw: subprocess.run(argv, env=env, cwd=repo, **kw),
        )
        == 0
    )
    restored = component_state.load_installed_state(state_path)
    assert set(restored.components) == set(payload["components"])
    for canonical in FAMILIES.values():
        assert not paths[canonical].exists()
    # Forward again, then let the old installer reinstall its legacy identities.
    paths = setup(acceptance)
    failed = subprocess.run(
        [str(a) for a in [*old_cli, "setup", "--manifest", old / "component-manifest-v1.json", "--offline"]],
        env=old_env,
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert failed.returncode == 1
    assert "invalid installed state" in failed.stderr
    assert (
        component_install.setup_native_components(
            env=env,
            rollback=True,
            offline=True,
            runner=lambda argv, **kw: subprocess.run(argv, env=env, cwd=repo, **kw),
        )
        == 0
    )
    run([*old_cli, "setup", "--manifest", old / "component-manifest-v1.json", "--offline"], old_env, repo)
    run([evidence, "evidence", "show", bundle_id, "--json"], env, repo)
    assert set(component_state.load_installed_state(state_path).components) == set(payload["components"])


def test_fresh_install_downgrade_requires_moving_state_aside(acceptance):
    _, _, old, env, repo, old_python = acceptance
    paths = setup(acceptance)
    old_env = {**env, "PYTHONPATH": str(old_python)}
    state_path = Path(component_paths.installed_state_path(component_paths.data_root(env=env)))
    argv = [sys.executable, "-m", "brigade", "setup", "--manifest", str(old / "component-manifest-v1.json")]
    failed = subprocess.run(argv, env=old_env, cwd=repo, capture_output=True, text=True, timeout=90)
    assert failed.returncode == 1 and "invalid installed state" in failed.stderr
    state_path.rename(state_path.with_suffix(".saved"))
    # Seed the verified old assets so recovery stays offline.
    payload = json.loads((old / "component-manifest-v1.json").read_bytes())
    for record in payload["components"].values():
        asset = record["assets"]["linux-amd64"]
        cache = Path(
            component_paths.cached_asset_path(component_paths.cache_root(env=env), asset["sha256"], asset["asset_name"])
        )
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes((old / asset["asset_name"]).read_bytes())
    run([*argv, "--offline"], old_env, repo)
    assert set(component_state.load_installed_state(state_path).components) == set(payload["components"])
    assert all(path.exists() for path in paths.values())
    # A downgrade can create a second layout; forward resolution reports both.
    legacy_evidence = next(key for key, value in FAMILIES.items() if value == "brigade-evidence")
    run([paths["brigade-evidence"], "init"], env, repo)
    run([old / f"{legacy_evidence}-linux-amd64", "init"], env, repo)
    assert any(row[0] == "WARN" and "evidence" in row[1] for row in engine_paths.legacy_findings(repo, env=env))
