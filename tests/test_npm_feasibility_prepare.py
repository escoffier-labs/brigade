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


def wheel(tmp_path, *, metadata="", extra=None, version="0.30.0.dev1"):
    filename = tmp_path / f"brigade_cli-{version}-py3-none-any.whl"
    dist = f"brigade_cli-{version}.dist-info"
    files = {
        "brigade/__init__.py": b'__version__ = "0.30.0.dev1"\n',
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
        ("", {"brigade_cli-0.30.0.dev1.dist-info/WHEEL": b"Tag: cp312-none-any\n"}),
    ],
)
def test_wheel_rejects_unsafe_metadata_on_every_supported_platform(tmp_path, metadata, extra):
    with pytest.raises(ValueError):
        prepare.validate_wheel(wheel(tmp_path, metadata=metadata, extra=extra), "0.30.0.dev1")


def test_wheel_record_corruption_is_rejected(tmp_path):
    payload = wheel(tmp_path)
    with zipfile.ZipFile(payload) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    files["brigade/__init__.py"] = b"corrupt"
    with zipfile.ZipFile(payload, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    with pytest.raises(ValueError, match="RECORD"):
        prepare.validate_wheel(payload, "0.30.0.dev1")


def test_pure_wheel_extra_dependency_is_allowed(tmp_path):
    payload = wheel(tmp_path, metadata='Requires-Dist: pytest; extra == "dev"\n')
    result = prepare.validate_wheel(payload, "0.30.0.dev1")
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
    (package / "package.json").write_text(json.dumps({"name": "@example/brigade", "version": "0.30.0"}))
    artifact = prepare.pack(package, tmp_path)
    data = (tmp_path / artifact["filename"]).read_bytes()
    assert artifact["integrity"] == "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    assert artifact["shasum"] == hashlib.sha1(data).hexdigest()


def test_pack_refuses_clearly_when_npm_is_missing(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("pack ran a subprocess without a resolved npm")

    monkeypatch.setattr(prepare.shutil, "which", lambda name: None)
    monkeypatch.setattr(prepare.subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="npm executable not found on PATH"):
        prepare.pack(tmp_path, tmp_path)


@pytest.mark.parametrize("mutation", [None, "drift", "addition", "missing", "manifest", "revision"])
def test_stable_payload_audit_binds_tag_inventory_and_only_release_manifest(tmp_path, mutation):
    import json

    commit = "a" * 40
    source = tmp_path / "source"
    (source / "src/brigade").mkdir(parents=True)
    (source / "src/brigade/__init__.py").write_bytes(b'__version__ = "0.30.0.dev1"\n')
    manifest = {
        "brigade_version": "0.30.0",
        "manifest_revision": "v0.30.0+" + commit,
        "components": {"example": {"component_revision": commit}},
    }
    data = json.dumps(manifest).encode()
    release = tmp_path / "manifest.json"
    release.write_bytes(data)
    extra = {"brigade/templates/components/manifest-v1.json": data}
    reference = wheel(tmp_path, version="0.30.0", extra=extra)
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
    payload = wheel(tmp_path, version="0.30.0", extra=extra)
    if mutation:
        with pytest.raises(ValueError):
            prepare.audit_stable_payload(payload, reference, source, release, "0.30.0", commit)
    else:
        result = prepare.audit_stable_payload(payload, reference, source, release, "0.30.0", commit)
        assert result["packageEntries"] == 2
        assert result["componentManifestSha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    "state",
    [
        "absent",
        "equal",
        "mismatch",
        "uncertain",
        "lagging",
        "lagging-forever",
        "lagging-deadline",
        "deadline-after-read",
        "slow-pre-publish-read",
        "latest-mismatch",
        "latest-after-deadline",
        "unavailable",
        "no-attestation",
        "wrong-commit",
        "sha-mismatch",
        "missing-sha",
        "blank-versions-string",
        "blank-versions-array",
        "blank-versions-blank",
        "blank-versions-malformed",
        "blank-versions-error",
        "blank-versions-present",
        "blank-versions-null",
        "blank-versions-empty-array",
        "blank-versions-mixed-array",
        "blank-versions-invalid-string",
        "blank-versions-invalid-semver",
        "malformed-integrity",
    ],
)
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
    metadata = {
        "runtime": {"version": "0.30.0", "payloadVersion": "0.30.0"},
        "sourceCommit": "a" * 40,
        "artifacts": packages,
    }
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
    name=args[1] if args[2]=='versions' else args[1].rsplit('@',1)[0]
    a=next(a for a in metadata['artifacts'] if a['name']==name)
    published=(root/(a['filename']+'.published')).exists()
    if args[2]=='versions':
        values={'string':'0.0.0-bootstrap','array':['0.0.0-bootstrap','0.28.0'],
                'present':['0.0.0-bootstrap','0.30.0'],'null':None,'empty-array':[],
                'mixed-array':['0.0.0-bootstrap',None],'invalid-string':'unknown',
                'invalid-semver':'0.0.0-01'}
        kind=mode.removeprefix('blank-versions-')
        if kind=='blank': sys.exit(0)
        if kind=='malformed': print('{');sys.exit(0)
        if kind=='error': print(json.dumps(['0.0.0-bootstrap']));sys.exit(1)
        print(json.dumps(values[kind]));sys.exit(0)
    if mode.startswith('blank-versions-') and not published: print('  ');sys.exit(0)
    if mode.startswith('lagging'):
        def lag(key, misses):
            p=root/('lag-'+key.replace('/','_').replace('@','_'))
            n=int(p.read_text())+1 if p.exists() else 1
            p.write_text(str(n))
            if mode in ('lagging-forever','lagging-deadline') or n<=misses: print(json.dumps({'error':{'code':'E404'}}));sys.exit(1)
        if not published and args[2]!='dist-tags.latest': print(json.dumps({'error':{'code':'E404'}}));sys.exit(1)
        lag(name+args[2], 5)
    if mode=='deadline-after-read' and args[2]=='dist.attestations' and name==metadata['artifacts'][0]['name']:
        import time; time.sleep(3.5)
    if mode=='slow-pre-publish-read' and args[2]=='dist.integrity' and name==metadata['artifacts'][1]['name']:
        import time; time.sleep(3.5)
    if mode=='latest-after-deadline' and args[2]=='dist-tags.latest':
        import time; time.sleep(10.5)
    if mode=='malformed-integrity': print('{');sys.exit(0)
    if mode=='unavailable': print(json.dumps({'error':{'code':'E503'}}));sys.exit(1)
    if mode in ['absent','uncertain','no-attestation','deadline-after-read','slow-pre-publish-read'] and not published:
        print(json.dumps({'error':{'code':'E404'}}));sys.exit(1)
    if args[2]=='dist.integrity': print(json.dumps('sha512-wrong' if mode=='mismatch' else a['integrity']))
    elif args[2]=='dist.attestations': print(json.dumps(None if mode=='no-attestation' else {'url':'https://registry.npmjs.org/-/npm/v1/attestations/'+name+'@0.30.0'}))
    elif args[2]=='dist-tags.latest': print(json.dumps('0.29.0' if mode=='latest-mismatch' else '0.30.0'))
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
    import time

    started = time.monotonic() if state == "lagging-deadline" else None
    result = subprocess.run(
        ["node", str(SOURCE.parent / "publish.cjs"), "--artifacts", str(tmp_path)]
        + (
            ["--readback-delays-ms", "[0,10000]", "--readback-budget-ms", "3000"]
            if state == "lagging-deadline"
            else ["--readback-delays-ms", "[0,1]", "--readback-budget-ms", "3000"]
            if state in {"deadline-after-read", "slow-pre-publish-read"}
            else ["--readback-delays-ms", "[0,30000]", "--readback-budget-ms", "30000"]
            if state == "latest-mismatch"
            else ["--readback-delays-ms", "[0]", "--readback-budget-ms", "10000"]
            if state == "latest-after-deadline"
            else ["--readback-delays-ms", "[0,1,1,1,1,1]"]
        ),
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "PUBLISH_FIXTURE": str(tmp_path),
            "PUBLISH_STATE": state,
            "npm_execpath": str(cli),
            "NODE_OPTIONS": "--require=" + str(SOURCE.parent / "fixtures/publisher-provenance-preload.cjs"),
            "GITHUB_SHA": "" if state == "missing-sha" else "b" * 40 if state == "sha-mismatch" else "a" * 40,
        },
        capture_output=True,
        text=True,
        timeout=90,
    )
    elapsed = time.monotonic() - started if started is not None else None
    successful = {
        "absent",
        "equal",
        "uncertain",
        "lagging",
        "blank-versions-string",
        "blank-versions-array",
        "latest-after-deadline",
    }
    assert (result.returncode == 0) == (state in successful), result.stderr
    calls = (
        [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
        if (tmp_path / "calls.jsonl").exists()
        else []
    )
    publications = [call for call in calls if call[0] == "publish"]
    if state in {"mismatch", "unavailable", "equal", "malformed-integrity"} or (
        state.startswith("blank-versions-") and state not in successful
    ):
        assert not publications
    if state == "lagging-forever":
        assert "Registry reconciliation incomplete" in result.stderr
        assert len([call for call in calls if call[0] == "view" and call[2] == "dist.integrity"]) == 6 + 1
    if state == "lagging-deadline":
        assert "Registry reconciliation incomplete" in result.stderr
        assert len([call for call in calls if call[0] == "view" and call[2] == "dist.integrity"]) == 2, (
            "the run deadline must stop the wait before the 10s delay elapses"
        )
        assert elapsed is not None and elapsed < 8, "the run should stop before the 10s retry delay"
    if state == "deadline-after-read":
        assert "Registry reconciliation incomplete" in result.stderr
        assert [Path(call[1]).name for call in publications] == [packages[0]["filename"]]
    if state == "slow-pre-publish-read":
        assert "Registry reconciliation incomplete" in result.stderr
        assert [Path(call[1]).name for call in publications] == [packages[0]["filename"]]
    if state == "latest-mismatch":
        assert "Registry reconciliation incomplete" in result.stderr
    if state == "latest-after-deadline":
        assert result.returncode == 0, result.stderr
    if state in {"absent", "uncertain", "lagging", "blank-versions-string", "blank-versions-array"}:
        assert [Path(call[1]).name for call in publications] == [a["filename"] for a in packages]
        assert len([call for call in calls if call[0] == "view" and call[2] == "dist.attestations"]) >= 6
    if state == "no-attestation":
        assert all(Path(call[1]).name != packages[-1]["filename"] for call in publications)
    verifications = [call for call in calls if call[0] == "verify"]
    if state in successful:
        assert [call[1] for call in verifications] == [a["name"] for a in packages]
        if publications:
            launcher_index = calls.index(publications[-1])
            assert all(calls.index(call) < launcher_index for call in verifications[:-1])
    if state.startswith("blank-versions-"):
        lookups = [call for call in calls if call[0] == "view" and call[2] == "versions"]
        assert len(lookups) == (6 if state in successful else 1)
        if state not in successful:
            assert "Cannot establish registry state before publish" in result.stderr
    if state == "malformed-integrity":
        assert len(calls) == 1, "malformed integrity must not use absence fallback"
    if state == "wrong-commit":
        assert not publications and len(verifications) == 1
        assert len([call for call in calls if call[0] == "view" and call[2] == "dist.attestations"]) == 1
        assert packages[0]["name"] in result.stderr
    if state in {"sha-mismatch", "missing-sha"}:
        assert not calls, "source SHA must fail before any registry read"


@pytest.mark.parametrize(
    "state,field,attempts,error",
    [
        ("blank-once", "dist.integrity", 2, None),
        ("blank-once", "dist.attestations", 2, None),
        ("blank-always", "dist.integrity", 5, "registry visibility deadline"),
        ("blank-always", "dist.attestations", 5, "registry provenance deadline"),
        ("mismatch", "dist.integrity", 1, "sha512-wrong"),
        ("malformed", "dist.integrity", 1, "SyntaxError"),
        ("wrong-commit", "dist.attestations", 1, "Invalid registry provenance"),
        ("explicit-commit", "dist.integrity", 1, None),
        ("default-mismatch", "dist.integrity", 0, "artifact packaging commit must equal"),
        ("explicit-mismatch", "dist.integrity", 0, "artifact packaging commit must equal"),
        ("invalid-workflow-sha", "dist.integrity", 0, "GITHUB_SHA must identify"),
        ("malformed-option", "dist.integrity", 0, "expected-packaging-sha must be 40 lowercase hex"),
        ("missing-option", "dist.integrity", 0, "Required: --expected-packaging-sha"),
        ("duplicate-option", "dist.integrity", 0, "Duplicate option: --expected-packaging-sha"),
        ("packed-option", "dist.integrity", 0, "expected-packaging-sha requires --registry-spec"),
    ],
)
def test_registry_acceptance_retries_only_missing_metadata(tmp_path, state, field, attempts, error):
    import json
    import os
    import shutil
    import tarfile

    platform = subprocess.check_output(["node", "-p", "process.platform+'-'+process.arch"], text=True).strip()
    packages = [f"@escoffier-labs/brigade-uv-test-{i}" for i in range(5)] + ["@escoffier-labs/brigade"]
    payload = b"registry-only fixture; never installed"
    runtime = {
        "version": "0.30.0",
        "payloadVersion": "0.30.0",
        "platforms": {platform: {"package": packages[0]}},
        "payload": {
            "version": "0.30.0",
            "sourceCommit": "a" * 40,
            "wheel": "fixture.whl",
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    }
    artifacts = []
    for i, name in enumerate(packages):
        manifest = {
            "name": name,
            "version": "0.30.0",
            "publishConfig": {"access": "public"},
            "repository": {
                "type": "git",
                "url": "git+https://github.com/escoffier-labs/brigade.git",
                "directory": "npm/feasibility",
            },
        }
        files = {}
        if i == 5:
            manifest["optionalDependencies"] = {packages[0]: "0.30.0"}
            files = {"runtime.json": json.dumps(runtime).encode(), "wheel/fixture.whl": payload}
        files["package.json"] = json.dumps(manifest).encode()
        archive = tmp_path / f"package-{i}.tgz"
        with tarfile.open(archive, "w:gz", format=tarfile.USTAR_FORMAT) as tar:
            for filename, data in files.items():
                info = tarfile.TarInfo("package/" + filename)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        data = archive.read_bytes()
        artifacts.append(
            {
                "name": name,
                "filename": archive.name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "shasum": hashlib.sha1(data).hexdigest(),
                "integrity": "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode(),
                **({"platform": platform if i == 0 else f"test-{i}"} if i < 5 else {}),
            }
        )
    (tmp_path / "artifacts.json").write_text(
        json.dumps({"runtime": runtime, "artifacts": artifacts, "sourceCommit": "a" * 40, "sourceSha256": "b" * 64})
    )
    cli = tmp_path / "npm-cli.js"
    cli.write_text(
        r"""const fs=require('node:fs'),path=require('node:path');
const args=process.argv.slice(2),root=process.env.PUBLISH_FIXTURE;
if(args[0]==='--version') {console.log('11.19.1');process.exit(0);}
const log=path.join(root,'calls.jsonl');
const calls=fs.existsSync(log)?fs.readFileSync(log,'utf8').trim().split('\n').map(JSON.parse):[];
fs.appendFileSync(log,JSON.stringify(args)+'\n');
if(args[0]!=='view') throw new Error('unexpected registry write or install');
const metadata=JSON.parse(fs.readFileSync(path.join(root,'artifacts.json')));
if(args[2]==='dist-tags.latest') {console.log(JSON.stringify('0.30.0'));process.exit(0);}
const name=args[1].slice(0,args[1].lastIndexOf('@'));
const artifact=metadata.artifacts.find(a=>a.name===name);
const count=calls.filter(c=>c[0]==='view' && c[1]===args[1] && c[2]===args[2]).length;
if(args[2]===process.env.LOOKUP_FIELD) {
  if(process.env.PUBLISH_STATE==='blank-always' || (process.env.PUBLISH_STATE==='blank-once' && count===0)) {
    process.stdout.write(' \n');process.exit(0);
  }
  if(process.env.PUBLISH_STATE==='mismatch') {console.log(JSON.stringify('sha512-wrong'));process.exit(0);}
  if(process.env.PUBLISH_STATE==='malformed') {console.log('{');process.exit(0);}
}
console.log(JSON.stringify(args[2]==='dist.integrity'?artifact.integrity:
  {url:'https://registry.npmjs.org/-/npm/v1/attestations/'+name+'@0.30.0'}));
"""
    )
    (tmp_path / "npx-cli.js").write_text("console.error('registry metadata complete');process.exit(79);")
    out = tmp_path / "receipt"
    argv = [
        "node",
        str(SOURCE.parent / "acceptance.cjs"),
        "--artifacts",
        str(tmp_path),
        "--out",
        str(out),
        "--registry-spec",
        "@escoffier-labs/brigade@0.30.0",
    ]
    if state.startswith("explicit"):
        argv += ["--expected-packaging-sha", ("c" if state == "explicit-mismatch" else "a") * 40]
    elif state in {"malformed-option", "missing-option", "duplicate-option", "packed-option"}:
        argv += ["--expected-packaging-sha"]
        if state != "missing-option":
            argv += ["invalid" if state == "malformed-option" else "a" * 40]
        if state == "duplicate-option":
            argv += ["--expected-packaging-sha", "a" * 40]
        if state == "packed-option":
            del argv[argv.index("--registry-spec") : argv.index("--registry-spec") + 2]
    physical_temp = tmp_path / "physical-temp"
    physical_temp.mkdir()
    alias_temp = tmp_path / "alias-temp"
    if os.name == "nt":
        alias_temp = physical_temp
    else:
        alias_temp.symlink_to(physical_temp, target_is_directory=True)
    result = subprocess.run(
        argv,
        env={
            **os.environ,
            "npm_execpath": str(cli),
            "PUBLISH_FIXTURE": str(tmp_path),
            "PUBLISH_STATE": state,
            "LOOKUP_FIELD": field,
            "GITHUB_SHA": "invalid"
            if state == "invalid-workflow-sha"
            else ("b" if state in {"explicit-commit", "default-mismatch"} else "a") * 40,
            "TMPDIR": str(alias_temp),
            "TEMP": str(alias_temp),
            "TMP": str(alias_temp),
            "NODE_OPTIONS": "--require=" + str(SOURCE.parent / "fixtures/publisher-provenance-preload.cjs"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    receipt = json.loads((out / "acceptance.json").read_text())
    try:
        assert result.returncode == 1 and receipt["status"] == "failed"
        assert Path(receipt["root"]) == Path(receipt["root"]).resolve(), (
            "harness must canonicalize temp root before deriving npm prefixes"
        )
        calls_file = tmp_path / "calls.jsonl"
        calls = [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
        first = f"{packages[0]}@0.30.0"
        lookups = [c for c in calls if c[:3] == ["view", first, field]]
        assert len(lookups) == attempts, receipt["error"]
        assert all(c[0] in {"view", "verify"} for c in calls), "metadata tests must never install or publish"
        verifications = [c for c in calls if c[0] == "verify"]
        if error:
            assert error in receipt["error"]
            assert not receipt["checks"], "fail before npx or runtime and signal fixtures"
            assert len(verifications) == (1 if state == "wrong-commit" else 0)
        else:
            assert receipt["validationSha"] == ("b" if state == "explicit-commit" else "a") * 40
            assert receipt["packagingSha"] == "a" * 40
            assert [c[1] for c in verifications] == packages
            assert "registry metadata complete" in receipt["error"]
            assert [c["name"] for c in receipt["checks"]] == ["npx-cold-version"]
            assert receipt["checks"][0]["exit"] == 79, "stop before runtime and signal fixtures"
    finally:
        shutil.rmtree(receipt["root"])


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
        "version": "0.30.0",
        "payloadVersion": "0.30.0",
        "platforms": {
            platform: {
                "package": "@escoffier-labs/brigade-uv-" + platform,
                "binarySha256": hashlib.sha256(platform.encode()).hexdigest(),
            }
            for platform in platforms
        },
        "payloadWheel": {
            "sha256": hashlib.sha256(b"fixture release wheel").hexdigest(),
            "releaseTag": "v0.30.0",
            "sourceCommit": "a" * 40,
            "componentManifest": {"sha256": hashlib.sha256(b"fixture release manifest").hexdigest()},
        },
    }
    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime.json").write_text(json.dumps(runtime))
    runtime["payload"] = {
        "wheel": "brigade_cli-0.30.0-py3-none-any.whl",
        "sha256": runtime["payloadWheel"]["sha256"],
        "componentManifestSha256": runtime["payloadWheel"]["componentManifest"]["sha256"],
        "sourceCommit": "a" * 40,
        "releaseTag": "v0.30.0",
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
            content = json.dumps({"name": name, "version": "0.30.0"}).encode()
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


@pytest.mark.parametrize(
    "selection,expected_version,error",
    [
        ("path-first", "91.92.93", None),
        ("path-first-unpaired", "81.82.83", None),
        ("explicit", "71.72.73", None),
        ("explicit-missing", None, "npm_execpath"),
        ("explicit-unpaired", None, "npm_execpath"),
        ("explicit-failed-version", None, "npm CLI version probe failed"),
        ("explicit-invalid-version", None, "npm CLI version probe failed"),
    ],
)
def test_acceptance_uses_first_usable_npm_client_before_artifact_validation(
    tmp_path, selection, expected_version, error
):
    import json
    import os
    import shutil

    node = shutil.which("node")
    assert node, "acceptance regression requires Node"
    node = subprocess.check_output([node, "-p", "process.execPath"], text=True).strip()
    log = tmp_path / "clients.jsonl"

    def client(name, version):
        directory = tmp_path / name
        cli = directory / "node_modules/npm/bin/npm-cli.js"
        cli.parent.mkdir(parents=True)
        script = (
            f"#!{node}\n"
            "const fs=require('node:fs');fs.appendFileSync("
            + json.dumps(str(log))
            + ",JSON.stringify({client:"
            + json.dumps(name)
            + ",args:process.argv.slice(2)})+'\\n');"
            + "if(process.argv[2]!=='--version') process.exit(79);"
            + "console.log("
            + json.dumps(version)
            + ");"
        )
        cli.write_text(script)
        cli.chmod(0o755)
        (cli.parent / "npx-cli.js").write_text(script)
        command = directory / ("npm.cmd" if os.name == "nt" else "npm")
        if os.name == "nt":
            command.write_text('@echo off\r\n"%~dp0node_modules\\npm\\bin\\npm-cli.js" %*\r\n')
        else:
            command.symlink_to(cli)
        return directory, cli

    first, first_cli = client("first", "91.92.93")
    last, last_cli = client("last", "81.82.83")
    override, override_cli = client("override", "71.72.73")
    utilities = tmp_path / "utilities"
    utilities.mkdir()
    if os.name == "nt":
        git = shutil.which("git")
        assert git, "acceptance setup requires Git"
        utility_paths = [str(Path(os.environ["SystemRoot"]) / "System32"), str(Path(git).parent)]
    else:
        for utility in (
            "sh",
            "dirname",
            "realpath",
            "git",
            "uname",
            *(("sysctl",) if sys.platform == "darwin" else ()),
        ):
            executable = shutil.which(utility)
            assert executable, "acceptance setup requires " + utility
            (utilities / utility).symlink_to(Path(executable).resolve())
        utility_paths = [str(utilities)]
    env = {key: value for key, value in os.environ.items() if key.lower() != "npm_execpath"}
    env["PATH"] = os.pathsep.join([str(first), *utility_paths, str(last)])
    if selection == "path-first-unpaired":
        (first_cli.parent / "npx-cli.js").unlink()
    elif selection.startswith("explicit"):
        env["npm_execpath"] = str(override_cli)
        if selection == "explicit-missing":
            env["npm_execpath"] = str(override / "missing.js")
        elif selection == "explicit-unpaired":
            (override_cli.parent / "npx-cli.js").unlink()
        elif selection == "explicit-failed-version":
            override_cli.write_text("process.exit(9);")
        elif selection == "explicit-invalid-version":
            override_cli.write_text("console.log('untrusted-version-output');")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "artifacts.json").write_text("{}")
    out = tmp_path / "receipt"
    result = subprocess.run(
        [node, str(SOURCE.parent / "acceptance.cjs"), "--artifacts", str(artifacts), "--out", str(out)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    receipt = json.loads((out / "acceptance.json").read_text())
    try:
        assert result.returncode == 1 and receipt["status"] == "failed"
        assert receipt["npmVersion"] == expected_version
        if error:
            assert error in receipt["error"]
        else:
            assert "packed artifacts must identify final source" in receipt["error"]
            expected_cli = (
                override_cli
                if selection == "explicit"
                else last_cli
                if selection == "path-first-unpaired"
                else first_cli
            )
            assert Path(receipt["npxCli"]) == expected_cli.parent / "npx-cli.js"
        assert "untrusted-version-output" not in receipt["error"]
        assert not receipt["checks"], "stop before npx, npm install, runtime bootstrap or signal fixtures"
        if log.exists():
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            assert all(call["args"] == ["--version"] for call in calls)
    finally:
        shutil.rmtree(receipt["root"])


@pytest.mark.parametrize("workflow,guard_count", [("npm-feasibility.yml", 1), ("npm-publish.yml", 2)])
@pytest.mark.parametrize("wrong_version", [None, "nodeVersion", "npmVersion"])
def test_native_acceptance_receipt_guards_require_pinned_clients(tmp_path, workflow, guard_count, wrong_version):
    import json
    import os
    import re
    import textwrap

    source = (SOURCE.parents[2] / ".github/workflows" / workflow).read_text()
    blocks = re.findall(r"python - <<'PYCODE'\n(.*?)^\s*PYCODE$", source, flags=re.MULTILINE | re.DOTALL)
    guards = [textwrap.dedent(block) for block in blocks if "result['acceptanceMode']" in block]
    assert len(guards) == guard_count, "packed and registry receipt guards must both be exercised"
    receipt = {
        "status": "passed",
        "cancellationGate": "passed",
        "platform": "win32-x64",
        "expectedPlatform": "win32-x64",
        "acceptanceMode": "native-default",
        "nodeVersion": "v24.22.0",
        "npmVersion": "11.19.1",
        "distributionMode": "registry",
        "checks": [{"name": "npm-audit-signatures"}],
        "pathExcludesPythonAndUv": True,
        "pathEntries": [],
    }
    if wrong_version:
        receipt[wrong_version] = "v22.0.0" if wrong_version == "nodeVersion" else "10.9.9"
    output = tmp_path / "npm-receipts"
    output.mkdir()
    (output / "acceptance.json").write_text(json.dumps(receipt))
    for guard in guards:
        result = subprocess.run(
            [sys.executable, "-c", guard],
            env={**os.environ, "RUNNER_TEMP": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert (result.returncode == 0) == (wrong_version is None), (
            "receipt guard must reject a different client version"
        )


def test_readback_schedule_spans_about_ten_minutes_with_capped_backoff():
    root = SOURCE.parent
    out = subprocess.check_output(
        ["node", "-p", "JSON.stringify(require(process.argv[1]).readbackDelays())", str(root / "readback.cjs")],
        text=True,
    )
    import json

    delays = json.loads(out)
    assert delays[0] == 0
    assert 540_000 <= sum(delays) <= 660_000
    assert max(delays) == 60_000
    assert delays[1] == 15_000
    assert all(b >= a for a, b in zip(delays[1:-1], delays[2:-1], strict=False))


def test_source_provenance_crypto_gate_cannot_be_skipped_or_env_configured():
    root = SOURCE.parents[2]
    for filename, job in (("npm-feasibility.yml", "package"), ("npm-publish.yml", "build")):
        text = (root / ".github/workflows" / filename).read_text()
        build = text.split("  " + job + ":", 1)[1].split("\n  acceptance:", 1)[0]
        gate = build.index("npm/feasibility/provenance.test.cjs")
        assert build.index("python-version:") < gate < build.index("Build six stable payload archives")
        assert "NODE_OPTIONS" not in text
    import re

    for filename, allowed in (("publish.cjs", {"GITHUB_SHA"}), ("provenance.cjs", {"npm_execpath", "PATH"})):
        text = (SOURCE.parent / filename).read_text()
        assert set(re.findall(r"process\.env\.([A-Za-z_][A-Za-z_0-9]*)", text)) <= allowed
