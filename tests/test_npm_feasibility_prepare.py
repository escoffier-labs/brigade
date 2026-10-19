"""Private payload boundaries, independent of downloads and build tooling."""

import base64
import csv
import hashlib
import importlib.util
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "npm/feasibility/prepare.py"
spec = importlib.util.spec_from_file_location("npm_prepare", SOURCE)
assert spec and spec.loader
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def wheel(tmp_path, *, metadata="", extra=None, version="0.29.0.dev1"):
    filename = tmp_path / f"brigade_cli-{version}-py3-none-any.whl"
    dist = f"brigade_cli-{version}.dist-info"
    files = {
        "brigade/__init__.py": b'__version__ = "0.29.0.dev1"\n',
        f"{dist}/METADATA": (
            f"Metadata-Version: 2.4\nName: brigade-cli\nVersion: {version}\n"
            "License-Expression: MIT\nProvides-Extra: dev\n" + metadata + "\n"
        ).encode(),
        f"{dist}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist}/licenses/LICENSE": b"MIT License\nPermission is hereby granted, free of charge\n",
    }
    files.update(extra or {})
    rows = []
    for name, data in files.items():
        checksum = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows.append([name, "sha256=" + checksum, str(len(data))])
    stream = io.StringIO()
    csv.writer(stream).writerows([*rows, [f"{dist}/RECORD", "", ""]])
    files[f"{dist}/RECORD"] = stream.getvalue().encode()
    with zipfile.ZipFile(filename, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return filename


@pytest.mark.parametrize(
    "metadata,extra",
    [
        ('Requires-Dist: forbidden; sys_platform == "win32"\n', None),
        ('Requires-Dist: forbidden; python_version < "3.11"\n', None),
        ('Requires-Dist: forbidden; extra == "dev" or sys_platform == "win32"\n', None),
        ("Version: 9.0\n", None),
        ("", {"../escape": b"unsafe"}),
        ("", {"brigade_cli-0.29.0.dev1.dist-info/WHEEL": b"Tag: cp312-none-any\n"}),
    ],
)
def test_wheel_rejects_unsafe_metadata_on_every_supported_platform(tmp_path, metadata, extra):
    with pytest.raises(ValueError):
        prepare.validate_wheel(wheel(tmp_path, metadata=metadata, extra=extra), "0.29.0.dev1")


def test_wheel_record_corruption_is_rejected(tmp_path):
    payload = wheel(tmp_path)
    with zipfile.ZipFile(payload) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    files["brigade/__init__.py"] = b"corrupt"
    with zipfile.ZipFile(payload, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    with pytest.raises(ValueError, match="RECORD"):
        prepare.validate_wheel(payload, "0.29.0.dev1")


def test_pure_wheel_extra_dependency_is_allowed(tmp_path):
    payload = wheel(tmp_path, metadata='Requires-Dist: pytest; extra == "dev"\n')
    result = prepare.validate_wheel(payload, "0.29.0.dev1")
    assert result["sha256"] == hashlib.sha256(payload.read_bytes()).hexdigest()
    assert result["bytes"] == payload.stat().st_size


@pytest.mark.parametrize("untracked", [False, True])
def test_dirty_preparation_refuses_before_download(tmp_path, monkeypatch, untracked):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    tracked = repo / "source.txt"
    tracked.write_text("initial\n")
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            repo,
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    (repo / "untracked.txt" if untracked else tracked).write_text("dirty\n")
    monkeypatch.setattr(prepare, "REPO", repo)
    monkeypatch.setattr(sys, "argv", ["prepare", "--out", str(tmp_path / "output")])

    def forbidden(*args, **kwargs):
        pytest.fail("dirty preparation attempted a download")

    monkeypatch.setattr(prepare, "download", forbidden)
    with pytest.raises(ValueError, match="dirty"):
        prepare.main()


def test_pack_preserves_registry_integrity_of_original_archive(tmp_path):
    import json

    package = tmp_path / "package"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"name": "@example/brigade", "version": "0.29.0"}))
    artifact = prepare.pack(package, tmp_path)
    data = (tmp_path / artifact["filename"]).read_bytes()
    assert artifact["integrity"] == "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    assert artifact["shasum"] == hashlib.sha1(data).hexdigest()


@pytest.mark.parametrize("mutation", [None, "drift", "addition", "missing", "manifest", "revision"])
def test_stable_payload_audit_binds_tag_inventory_and_only_release_manifest(tmp_path, mutation):
    import json

    commit = "a" * 40
    source = tmp_path / "source"
    (source / "src/brigade").mkdir(parents=True)
    (source / "src/brigade/__init__.py").write_bytes(b'__version__ = "0.29.0.dev1"\n')
    manifest = {
        "brigade_version": "0.29.0",
        "manifest_revision": "v0.29.0+" + commit,
        "components": {"example": {"component_revision": commit}},
    }
    data = json.dumps(manifest).encode()
    release = tmp_path / "manifest.json"
    release.write_bytes(data)
    extra = {"brigade/templates/components/manifest-v1.json": data}
    reference = wheel(tmp_path, version="0.29.0", extra=extra)
    reference = reference.rename(tmp_path / "reference.whl")
    if mutation == "drift":
        extra["brigade/__init__.py"] = b"unexpected"
    elif mutation == "addition":
        extra["brigade/extra.py"] = b"unexpected"
    elif mutation == "missing":
        extra = {}
    elif mutation == "manifest":
        extra["brigade/templates/components/manifest-v1.json"] = b"{}"
    elif mutation == "revision":
        manifest["components"]["example"]["component_revision"] = "b" * 40
        release.write_text(json.dumps(manifest))
        extra["brigade/templates/components/manifest-v1.json"] = release.read_bytes()
    payload = wheel(tmp_path, version="0.29.0", extra=extra)
    if mutation:
        with pytest.raises(ValueError):
            prepare.audit_stable_payload(payload, reference, source, release, "0.29.0", commit)
    else:
        result = prepare.audit_stable_payload(payload, reference, source, release, "0.29.0", commit)
        assert result["packageEntries"] == 2
        assert result["componentManifestSha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("state", ["absent", "equal", "mismatch", "uncertain", "unavailable", "no-attestation"])
def test_registry_publisher_reconciles_immutable_archives_before_launcher(tmp_path, state):
    import json
    import os

    packages = []
    for i in range(6):
        data = f"original-{i}".encode()
        filename = f"package-{i}.tgz"
        (tmp_path / filename).write_bytes(data)
        artifact = {
            "name": f"@escoffier-labs/brigade-uv-test-{i}" if i < 5 else "@escoffier-labs/brigade",
            "filename": filename,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "shasum": hashlib.sha1(data).hexdigest(),
            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode(),
        }
        if i < 5:
            artifact["platform"] = f"test-{i}"
        packages.append(artifact)
    metadata = {"runtime": {"version": "0.29.0", "payloadVersion": "0.29.0"}, "artifacts": packages}
    (tmp_path / "artifacts.json").write_text(json.dumps(metadata))
    npm = tmp_path / "npm-fixture.py"
    npm.write_text(
        "#!"
        + sys.executable
        + "\n"
        + """import json,sys,os
from pathlib import Path
root=Path(os.environ['PUBLISH_FIXTURE']); mode=os.environ['PUBLISH_STATE']
args=sys.argv[1:]
with (root/'calls.jsonl').open('a') as f: f.write(json.dumps(args)+'\\n')
metadata=json.loads((root/'artifacts.json').read_text())
if args[0]=='view':
    name=args[1].rsplit('@',1)[0]; a=next(a for a in metadata['artifacts'] if a['name']==name)
    published=(root/(a['filename']+'.published')).exists()
    if mode=='unavailable': print(json.dumps({'error':{'code':'E503'}}));sys.exit(1)
    if mode in ['absent','uncertain','no-attestation'] and not published:
        print(json.dumps({'error':{'code':'E404'}}));sys.exit(1)
    if args[2]=='dist.integrity': print(json.dumps('sha512-wrong' if mode=='mismatch' else a['integrity']))
    elif args[2]=='dist.attestations': print(json.dumps(None if mode=='no-attestation' else {'url':'https://registry.npmjs.org/attestation'}))
    elif args[2]=='dist-tags.latest': print(json.dumps('0.29.0'))
else:
    assert args[0]=='publish' and '--provenance' in args and '--access' in args and 'public' in args
    archive=Path(args[1]); assert archive.read_bytes().startswith(b'original-')
    (root/(archive.name+'.published')).touch()
    sys.exit(1 if mode=='uncertain' else 0)
"""
    )
    cli = tmp_path / "npm-cli.cjs"
    cli.write_text(
        "const r=require('node:child_process').spawnSync("
        + json.dumps(sys.executable)
        + ", ["
        + json.dumps(str(npm))
        + ",...process.argv.slice(2)],{stdio:'inherit'});process.exit(r.status ?? 1);"
    )
    result = subprocess.run(
        ["node", str(SOURCE.parent / "publish.cjs"), "--artifacts", str(tmp_path)],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "PUBLISH_FIXTURE": str(tmp_path),
            "PUBLISH_STATE": state,
            "npm_execpath": str(cli),
        },
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert (result.returncode == 0) == (state in {"absent", "equal", "uncertain"}), result.stderr
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    publications = [call for call in calls if call[0] == "publish"]
    if state in {"mismatch", "unavailable", "equal"}:
        assert not publications
    if state in {"absent", "uncertain"}:
        assert [Path(call[1]).name for call in publications] == [a["filename"] for a in packages]
        assert len([call for call in calls if call[0] == "view" and call[2] == "dist.attestations"]) >= 6
    if state == "no-attestation":
        assert all(Path(call[1]).name != packages[-1]["filename"] for call in publications)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (None, None),
        ("packaging-source", "packaging source"),
        ("archive-bytes", "original archive integrity"),
        ("archive-size", "original archive integrity"),
        ("archive-sha256", "original archive integrity"),
        ("archive-integrity", "original archive integrity"),
        ("archive-shasum", "original archive integrity"),
        ("reviewed-binary", "runtime differs from reviewed"),
        ("reviewed-pin", "runtime differs from reviewed"),
        ("payload-wheel-sha", "stable payload pin identity"),
        ("payload-wheel-name", "stable payload pin identity"),
        ("payload-manifest-sha", "stable payload pin identity"),
        ("extra-tgz", "six-archive inventory"),
        ("missing-tgz", "six-archive inventory"),
        ("wrong-names", "six-archive inventory"),
        ("duplicate-filename", "six-archive inventory"),
        ("runtime-receipt", "runtime receipt mismatch"),
    ],
)
def test_artifact_verification_binds_six_original_archives_to_reviewed_source(tmp_path, monkeypatch, mutation, reason):
    import copy
    import json
    import tarfile

    platforms = ("linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win32-x64")
    runtime = {
        "version": "0.29.0",
        "payloadVersion": "0.29.0",
        "platforms": {
            platform: {
                "package": "@escoffier-labs/brigade-uv-" + platform,
                "binarySha256": hashlib.sha256(platform.encode()).hexdigest(),
            }
            for platform in platforms
        },
        "payloadWheel": {
            "sha256": hashlib.sha256(b"fixture release wheel").hexdigest(),
            "releaseTag": "v0.29.0",
            "sourceCommit": "a" * 40,
            "componentManifest": {"sha256": hashlib.sha256(b"fixture release manifest").hexdigest()},
        },
    }
    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime.json").write_text(json.dumps(runtime))
    runtime["payload"] = {
        "wheel": "brigade_cli-0.29.0-py3-none-any.whl",
        "sha256": runtime["payloadWheel"]["sha256"],
        "componentManifestSha256": runtime["payloadWheel"]["componentManifest"]["sha256"],
        "sourceCommit": "a" * 40,
        "releaseTag": "v0.29.0",
    }
    identity = {
        "sourceCommit": "b" * 40,
        "sourceTree": "clean",
        "sourceIdentity": "c" * 64,
        "sourceSha256": {},
        "sourceDateEpoch": 1,
    }
    monkeypatch.setattr(prepare, "SOURCE", source)
    monkeypatch.setattr(prepare, "source_snapshot", lambda _: (identity, []))
    artifacts = []
    for i, platform in enumerate((*platforms, None)):
        name = runtime["platforms"][platform]["package"] if platform else "@escoffier-labs/brigade"
        filename = f"package-{i}.tgz"
        with tarfile.open(tmp_path / filename, "w:gz") as archive:
            content = json.dumps({"name": name, "version": "0.29.0"}).encode()
            entry = tarfile.TarInfo("package/package.json")
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
        data = (tmp_path / filename).read_bytes()
        artifact = {
            "name": name,
            "filename": filename,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "shasum": hashlib.sha1(data).hexdigest(),
            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode(),
        }
        if platform:
            artifact["platform"] = platform
        artifacts.append(artifact)
    metadata = {**identity, "runtime": runtime, "artifacts": artifacts}
    if mutation == "packaging-source":
        metadata["sourceCommit"] = "d" * 40
    elif mutation == "archive-bytes":
        (tmp_path / artifacts[0]["filename"]).write_bytes(b"changed original archive")
    elif mutation == "archive-size":
        artifacts[0]["bytes"] += 1
    elif mutation == "archive-sha256":
        artifacts[0]["sha256"] = "0" * 64
    elif mutation == "archive-integrity":
        artifacts[0]["integrity"] = "sha512-wrong"
    elif mutation == "archive-shasum":
        artifacts[0]["shasum"] = "0" * 40
    elif mutation == "reviewed-binary":
        runtime["platforms"][platforms[0]]["binarySha256"] = "0" * 64
    elif mutation == "reviewed-pin":
        runtime["payloadWheel"]["sha256"] = "0" * 64
    elif mutation == "payload-wheel-sha":
        runtime["payload"]["sha256"] = "0" * 64
    elif mutation == "payload-wheel-name":
        runtime["payload"]["wheel"] = "brigade_cli-0.29.1-py3-none-any.whl"
    elif mutation == "payload-manifest-sha":
        runtime["payload"]["componentManifestSha256"] = "0" * 64
    elif mutation == "extra-tgz":
        (tmp_path / "extra.tgz").write_bytes(b"unexpected")
    elif mutation == "missing-tgz":
        (tmp_path / artifacts[0]["filename"]).unlink()
    elif mutation == "wrong-names":
        artifacts[0]["name"] = "@example/other"
    elif mutation == "duplicate-filename":
        artifacts[1]["filename"] = artifacts[0]["filename"]
    runtime_receipt = copy.deepcopy(runtime)
    if mutation == "runtime-receipt":
        runtime_receipt["version"] = "0.29.1"
    (tmp_path / "artifacts.json").write_text(json.dumps(metadata))
    (tmp_path / "runtime.json").write_text(json.dumps(runtime_receipt))
    if mutation:
        with pytest.raises(ValueError, match=reason):
            prepare.verify_artifacts(tmp_path)
    else:
        verified = prepare.verify_artifacts(tmp_path)
        assert len(verified["artifacts"]) == 6
        assert verified["runtime"]["payload"]["sourceCommit"] != verified["sourceCommit"]


@pytest.mark.parametrize("cached", [False, True])
def test_download_bound_applies_before_use_including_cached_bytes(tmp_path, monkeypatch, cached):
    source = tmp_path / "download"
    source.write_bytes(b"a" * 9)
    destination = tmp_path / "cache"
    if cached:
        destination.write_bytes(source.read_bytes())
    monkeypatch.setattr(prepare, "MAX_DOWNLOAD", 8)
    with pytest.raises(ValueError, match="bound"):
        prepare.download(source.as_uri(), destination, hashlib.sha256(source.read_bytes()).hexdigest())
