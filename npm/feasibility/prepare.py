#!/usr/bin/env python3
"""Build public npm tarballs from pinned official wheel and uv release bytes."""

from __future__ import annotations

import argparse
import base64
import csv
import email.parser
import io
import os
import re
import venv
import hashlib
import json
import shutil
import stat
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

SOURCE = Path(__file__).resolve().parent
REPO = SOURCE.parent.parent
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_EXPANDED = 160 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(url: str, destination: Path, expected: str) -> bytes:
    if destination.exists():
        with destination.open("rb") as cached:
            data = cached.read(MAX_DOWNLOAD + 1)
    else:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError(f"download exceeds bound: {url}")
    if digest(data) != expected:
        raise ValueError(f"SHA-256 mismatch: {url}; no substitute permitted")
    destination.write_bytes(data)
    return data


def executable_from_archive(archive: Path, executable: str) -> bytes:
    """Inspect every entry before reading just the expected regular executable."""
    zipped = archive.suffix == ".zip"
    root = archive.name.removesuffix(".tar.gz").removesuffix(".zip")
    expected = executable if zipped else f"{root}/{executable}"
    allowed = {executable, "uvx.exe", "uvw.exe"} if zipped else {root, expected, f"{root}/uvx"}
    total = 0
    seen: set[str] = set()
    payload = None
    if zipped:
        with zipfile.ZipFile(archive) as z:
            entries = z.infolist()
            if len(entries) > 16:
                raise ValueError("too many archive entries")
            for entry in entries:
                name = entry.filename
                validate_path(name, allowed, seen)
                total += entry.file_size
                mode = entry.external_attr >> 16
                if entry.is_dir() or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not stat.S_ISREG(mode)):
                    raise ValueError(f"unexpected archive entry type: {name}")
                if total > MAX_EXPANDED:
                    raise ValueError("archive expansion exceeds bound")
                if name == expected:
                    payload = z.read(entry)
    else:
        with tarfile.open(archive, "r:gz") as t:
            for index, member in enumerate(t):
                if index >= 16:
                    raise ValueError("too many archive entries")
                validate_path(member.name, allowed, seen)
                total += member.size
                if not member.isfile() and not (member.isdir() and member.name == root):
                    raise ValueError(f"unexpected archive entry type: {member.name}")
                if total > MAX_EXPANDED:
                    raise ValueError("archive expansion exceeds bound")
                if member.name == expected:
                    stream = t.extractfile(member)
                    if stream is None:
                        raise ValueError("executable is unreadable")
                    payload = stream.read(MAX_EXPANDED + 1)
    if payload is None or not payload:
        raise ValueError(f"missing executable: {expected}")
    return payload


def validate_path(name: str, allowed: set[str], seen: set[str]) -> None:
    parts = PurePosixPath(name).parts
    if name not in allowed or name in seen or "\\" in name or ":" in name or name.startswith("/") or ".." in parts:
        raise ValueError(f"unexpected or unsafe archive path: {name}")
    seen.add(name)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def pack(directory: Path, output: Path) -> dict[str, object]:
    # Windows installs npm as npm.cmd, which CreateProcess does not resolve from a bare "npm".
    npm = shutil.which("npm")
    if npm is None:
        raise ValueError("npm executable not found on PATH")
    result = subprocess.run(
        [npm, "pack", "--ignore-scripts", "--json", "--pack-destination", str(output)],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = json.loads(result.stdout)[0]
    artifact = output / metadata["filename"]
    return {
        "name": metadata["name"],
        "filename": artifact.name,
        "sha256": digest(artifact.read_bytes()),
        "bytes": artifact.stat().st_size,
        "integrity": metadata["integrity"],
        "shasum": metadata["shasum"],
        "unpackedBytes": metadata["unpackedSize"],
    }


def source_snapshot(allow_dirty: bool) -> tuple[dict, list[str]]:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(REPO), *args]).decode().strip()

    dirty = bool(git("status", "--porcelain", "--untracked-files=all"))
    if dirty and not allow_dirty:
        raise ValueError(
            "dirty source refused. Commit reviewed source. See npm/feasibility/README.md for local iteration options."
        )
    paths = sorted(set(git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")) - {""})
    hashes = {}
    for name in paths:
        source = REPO / name
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"source must be a regular file: {name}")
        hashes[name] = digest(source.read_bytes())
    identity = {
        "sourceCommit": git("rev-parse", "HEAD"),
        "sourceTree": "dirty" if dirty else "clean",
        "sourceDateEpoch": int(git("show", "-s", "--format=%ct", "HEAD")),
        "sourceSha256": hashes,
        "sourceIdentity": digest(json.dumps(hashes, sort_keys=True).encode()),
    }
    return identity, list(hashes)


def validate_wheel(wheel: Path, version: str) -> dict:
    """Fail closed on unsafe archives, metadata, dependencies, and RECORD bytes."""
    expected = f"brigade_cli-{version}-py3-none-any.whl"
    if wheel.name != expected or not wheel.is_file() or wheel.is_symlink():
        raise ValueError("expected exactly one brigade-cli pure py3-none-any wheel")
    dist = f"brigade_cli-{version}.dist-info"
    try:
        with zipfile.ZipFile(wheel) as archive:
            files: dict[str, bytes] = {}
            expanded = 0
            for entry in archive.infolist():
                name = entry.filename
                parts = PurePosixPath(name).parts
                mode = entry.external_attr >> 16
                expanded += entry.file_size
                if (
                    name in files
                    or name.startswith("/")
                    or ".." in parts
                    or "\\" in name
                    or ":" in name
                    or "/".join(parts) != name
                    or not parts
                    or parts[0] not in {"brigade", dist}
                    or entry.is_dir()
                    or (stat.S_IFMT(mode) and not stat.S_ISREG(mode))
                    or expanded > MAX_EXPANDED
                    or len(files) >= 10000
                ):
                    raise ValueError(f"unsafe wheel entry: {name}")
                files[name] = archive.read(entry)
            metadata = email.parser.BytesParser().parsebytes(files[f"{dist}/METADATA"])
            for key, expected_value in (("Name", "brigade-cli"), ("Version", version)):
                if metadata.get_all(key) != [expected_value]:
                    raise ValueError(f"wheel metadata mismatch: {key}")
            if metadata.get("License-Expression", metadata.get("License")) != "MIT":
                raise ValueError("wheel must use MIT license")
            if not any(
                name.startswith(dist + "/") and name.endswith("/LICENSE") and b"MIT License" in data
                for name, data in files.items()
            ):
                raise ValueError("wheel MIT LICENSE missing")
            wheel_meta = email.parser.BytesParser().parsebytes(files[f"{dist}/WHEEL"])
            if wheel_meta.get_all("Tag") != ["py3-none-any"] or wheel_meta.get("Root-Is-Purelib") != "true":
                raise ValueError("wheel must be pure py3-none-any")
            extras = set(metadata.get_all("Provides-Extra", []))
            for dependency in metadata.get_all("Requires-Dist", []):
                # Accept only explicit extra-only markers. Never evaluate a marker
                # on the builder platform, which could miss Windows/Python deps.
                requirement, separator, marker = dependency.partition(";")
                match = re.fullmatch(r"\s*extra\s*==\s*['\"]([a-zA-Z0-9_-]+)['\"]\s*", marker)
                if not requirement or not separator or not match or match[1] not in extras:
                    raise ValueError(f"non-extra or unsupported wheel dependency: {dependency}")
            record_path = f"{dist}/RECORD"
            rows = list(csv.reader(io.StringIO(files[record_path].decode())))
            if len(rows) != len(files) or {row[0] for row in rows} != set(files):
                raise ValueError("wheel RECORD file set mismatch")
            for row in rows:
                if len(row) != 3:
                    raise ValueError("wheel RECORD invalid row")
                name, checksum, size = row
                if name == record_path and checksum == size == "":
                    continue
                data = files[name]
                expected_hash = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                if checksum != "sha256=" + expected_hash or size != str(len(data)):
                    raise ValueError(f"wheel RECORD mismatch: {name}")
    except (KeyError, zipfile.BadZipFile, UnicodeError, csv.Error) as exc:
        raise ValueError(f"invalid wheel metadata/RECORD: {exc}") from exc
    return {
        "wheel": wheel.name,
        "version": version,
        "distribution": "brigade-cli",
        "sha256": digest(wheel.read_bytes()),
        "bytes": wheel.stat().st_size,
    }


def audit_stable_payload(wheel: Path, reference: Path, source: Path, manifest: Path, version: str, commit: str) -> dict:
    """Audit all package bytes and reverse coverage against a tag reference build."""

    def members(path: Path) -> dict[str, bytes]:
        with zipfile.ZipFile(path) as archive:
            return {name: archive.read(name) for name in archive.namelist() if name.startswith("brigade/")}

    actual, expected = members(wheel), members(reference)
    if set(actual) != set(expected):
        raise ValueError("tag reference package inventory mismatch")
    generated = "brigade/templates/components/manifest-v1.json"
    data = manifest.read_bytes()
    release = json.loads(data)
    if (
        release["brigade_version"] != version
        or release["manifest_revision"] != f"v{version}+{commit}"
        or not release["components"]
        or any(item["component_revision"] != commit for item in release["components"].values())
    ):
        raise ValueError("release component manifest revision mismatch")
    if actual.get(generated) != data:
        raise ValueError("wheel release component manifest mismatch")
    for name, content in actual.items():
        if name == generated:
            continue
        tag_file = source / "src" / name
        if not tag_file.is_file() or tag_file.is_symlink() or tag_file.read_bytes() != content:
            raise ValueError(f"wheel differs from tag source: {name}")
    return {"packageEntries": len(actual), "componentManifestSha256": digest(data)}


def build_payload(output: Path, runtime: dict, identity: dict, paths: list[str]) -> tuple[Path, dict]:
    # The packaging snapshot never substitutes for the stable payload's tag identity.
    pin = runtime.get("payloadWheel")
    if not pin:
        raise ValueError("verified published stable wheel pin required")
    version = runtime["payloadVersion"]
    tag = f"v{version}"
    if pin["releaseTag"] != tag or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("stable release tag/version mismatch")

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(REPO), *args])

    commit = git("rev-parse", f"{tag}^{{commit}}").decode().strip()
    if commit != pin["sourceCommit"] or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("stable release tag commit mismatch")
    build = output / "build"
    build.mkdir(exist_ok=False)
    source = build / "source"
    hashes = {}
    for entry in git("ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        header, raw_name = entry.split(b"\t", 1)
        mode, kind, oid = header.split()
        name = raw_name.decode()
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise ValueError(f"tag source must be regular files: {name}")
        content = git("cat-file", "blob", oid.decode())
        hashes[name] = digest(content)
        dest = source / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    wheel = build / f"brigade_cli-{version}-py3-none-any.whl"
    if not pin["url"].startswith("https://files.pythonhosted.org/") or not pin["url"].endswith("/" + wheel.name):
        raise ValueError("official published wheel URL required")
    download(pin["url"], wheel, pin["sha256"])
    payload = validate_wheel(wheel, version)
    component = pin["componentManifest"]
    base = f"https://github.com/escoffier-labs/brigade/releases/download/{tag}/"
    manifest = build / "component-manifest-v1.json"
    if component["url"] != base + manifest.name or component["checksumsUrl"] != base + "checksums.txt":
        raise ValueError("exact release manifest/checksums URLs required")
    download(component["url"], manifest, component["sha256"])
    checksums = download(component["checksumsUrl"], build / "checksums.txt", component["checksumsSha256"])
    matches = [line.split() for line in checksums.decode().splitlines() if line.split()[-1:] == [manifest.name]]
    if matches != [[component["sha256"], manifest.name]]:
        raise ValueError("release manifest checksum mismatch")
    (source / "src/brigade/templates/components/manifest-v1.json").write_bytes(manifest.read_bytes())
    builder = build / "venv"
    venv.EnvBuilder(with_pip=True).create(builder)
    python = builder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    requirements = SOURCE / "build-requirements.txt"
    subprocess.run(
        [str(python), "-m", "pip", "--isolated", "install", "--require-hashes", "-r", str(requirements)], check=True
    )
    tools = json.loads(
        subprocess.check_output(
            [
                str(python),
                "-I",
                "-c",
                "import importlib.metadata as m,json;print(json.dumps({n:m.version(n) for n in ('pip','packaging','setuptools','wheel')}))",
            ]
        )
    )
    wheel_dir = build / "reference"
    epoch = int(git("show", "-s", "--format=%ct", commit))
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "--isolated",
            "wheel",
            "--no-build-isolation",
            "--no-deps",
            "--no-index",
            "--wheel-dir",
            str(wheel_dir),
            str(source),
        ],
        env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch)},
        check=True,
    )
    references = list(wheel_dir.glob("*.whl"))
    if len(references) != 1:
        raise ValueError("tag reference must produce exactly one wheel")
    validate_wheel(references[0], version)
    audit = audit_stable_payload(wheel, references[0], source, manifest, version, commit)
    payload.update(
        sourceCommit=commit,
        sourceTree="clean",
        sourceTreeSha=git("rev-parse", f"{commit}^{{tree}}").decode().strip(),
        sourceIdentity=digest(json.dumps(hashes, sort_keys=True).encode()),
        releaseTag=tag,
        **audit,
    )
    runtime["payload"] = payload
    return wheel, {"versions": tools, "requirementsSha256": digest(requirements.read_bytes()), "sourceDateEpoch": epoch}


def dev_helper():
    import importlib.util

    spec = importlib.util.spec_from_file_location("dev_wheel", REPO / "scripts/prepare_dev_wheel.py")
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def candidate_version(identity: dict) -> str:
    from datetime import datetime, timezone

    helper = dev_helper()
    date = datetime.fromtimestamp(identity["sourceDateEpoch"], timezone.utc).strftime("%Y%m%d")
    return helper.derive_dev_version(helper.read_stable_version(REPO), date)


def candidate_runtime(reviewed: dict, identity: dict) -> dict:
    runtime = json.loads(json.dumps(reviewed))
    runtime["payloadVersion"] = candidate_version(identity)
    runtime["version"] = runtime["payloadVersion"].replace(".dev", "-candidate.") + "." + identity["sourceCommit"][:12]
    runtime.pop("payloadWheel")
    return runtime


def audit_candidate_wheel(wheel: Path, version: str, paths: list[str]) -> None:
    """Every package member and every source package file must agree."""
    expected = {}
    for name in paths:
        if not name.startswith("src/brigade/"):
            continue
        data = (REPO / name).read_bytes()
        if name == "src/brigade/__init__.py":
            data = re.sub(rb'(?m)^__version__ = "[^" ]+"$', f'__version__ = "{version}"'.encode(), data)
        expected[name.removeprefix("src/")] = data
    with zipfile.ZipFile(wheel) as archive:
        actual = {n: archive.read(n) for n in archive.namelist() if n.startswith("brigade/")}
    if set(actual) != set(expected):
        raise ValueError("candidate source package inventory mismatch")
    for name, data in expected.items():
        if actual[name] != data:
            raise ValueError(f"candidate wheel differs from source: {name}")


def build_candidate_payload(output: Path, runtime: dict, identity: dict, paths: list[str]) -> tuple[Path, dict]:
    if identity["sourceTree"] != "clean":
        raise ValueError("candidate requires clean source")
    runtime.clear()
    runtime.update(candidate_runtime(json.loads((SOURCE / "runtime.json").read_text()), identity))
    build = output / "build"
    source = build / "source"
    source.mkdir(parents=True, exist_ok=False)
    for name in paths:
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((REPO / name).read_bytes())
    version = runtime["payloadVersion"]
    dev_helper().prepare_dev_wheel(source, version)
    builder = build / "venv"
    venv.EnvBuilder(with_pip=True).create(builder)
    python = builder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    requirements = SOURCE / "build-requirements.txt"
    subprocess.run(
        [str(python), "-m", "pip", "--isolated", "install", "--require-hashes", "-r", str(requirements)], check=True
    )
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "--isolated",
            "wheel",
            "--no-build-isolation",
            "--no-deps",
            "--no-index",
            "--wheel-dir",
            str(build),
            str(source),
        ],
        env={**os.environ, "SOURCE_DATE_EPOCH": str(identity["sourceDateEpoch"])},
        check=True,
    )
    wheels = list(build.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("candidate must produce exactly one wheel")
    wheel = wheels[0]
    payload = validate_wheel(wheel, version)
    audit_candidate_wheel(wheel, version, paths)
    payload.update(sourceCommit=identity["sourceCommit"], sourceTree="clean", sourceIdentity=identity["sourceIdentity"])
    runtime["payload"] = payload
    # Refuse source races during construction, including changes outside the wheel.
    if source_snapshot(False)[0] != identity:
        raise ValueError("candidate source changed during build")
    tools = json.loads(
        subprocess.check_output(
            [
                str(python),
                "-I",
                "-c",
                "import importlib.metadata as m,json;print(json.dumps({n:m.version(n) for n in ('pip','packaging','setuptools','wheel')}))",
            ]
        )
    )
    return wheel, {
        "versions": tools,
        "requirementsSha256": digest(requirements.read_bytes()),
        "sourceDateEpoch": identity["sourceDateEpoch"],
    }


def verify_artifacts(output: Path, *, candidate: bool = False) -> dict:
    """Bind the original archives to clean packaging source and the stable pin."""
    metadata = json.loads((output / "artifacts.json").read_text())
    source, paths = source_snapshot(False)
    for key in ("sourceCommit", "sourceTree", "sourceIdentity", "sourceSha256", "sourceDateEpoch"):
        if metadata.get(key) != source[key]:
            raise ValueError(f"packaging source mismatch: {key}")
    runtime = metadata["runtime"]
    reviewed = json.loads((SOURCE / "runtime.json").read_text())
    expected_runtime = candidate_runtime(reviewed, source) if candidate else reviewed
    if {key: value for key, value in runtime.items() if key != "payload"} != expected_runtime:
        raise ValueError("runtime differs from reviewed source")
    if json.loads((output / "runtime.json").read_text()) != runtime:
        raise ValueError("runtime receipt mismatch")
    pin, payload = reviewed["payloadWheel"], runtime["payload"]
    if candidate:
        if any(payload.get(key) != source[key] for key in ("sourceCommit", "sourceTree", "sourceIdentity")):
            raise ValueError("candidate payload source identity mismatch")
        if (
            payload.get("version") != runtime["payloadVersion"]
            or payload.get("wheel") != f"brigade_cli-{runtime['payloadVersion']}-py3-none-any.whl"
        ):
            raise ValueError("candidate payload version mismatch")
    elif (
        runtime["version"] != "0.30.0"
        or runtime["payloadVersion"] != "0.30.0"
        or payload["sourceCommit"] != pin["sourceCommit"]
        or payload["releaseTag"] != pin["releaseTag"]
        or payload["sha256"] != pin["sha256"]
        or payload["componentManifestSha256"] != pin["componentManifest"]["sha256"]
        or payload["wheel"] != f"brigade_cli-{runtime['payloadVersion']}-py3-none-any.whl"
    ):
        raise ValueError("stable payload pin identity mismatch")
    artifacts = metadata["artifacts"]
    names = [item["filename"] for item in artifacts]
    platforms = runtime["platforms"]
    if (
        len(names) != len(set(names))
        or len(names) != 6
        or {a["platform"] for a in artifacts if "platform" in a} != set(platforms)
        or sum("platform" not in a for a in artifacts) != 1
        or {a["name"] for a in artifacts} != {"@escoffier-labs/brigade", *(p["package"] for p in platforms.values())}
        or {p.name for p in output.glob("*.tgz")} != set(names)
    ):
        raise ValueError("exact six-archive inventory required")
    for item in artifacts:
        name = item["filename"]
        if Path(name).name != name or "/" in name or "\\" in name or not name.endswith(".tgz"):
            raise ValueError("unsafe artifact filename")
        archive = output / name
        if not archive.is_file() or archive.is_symlink():
            raise ValueError("artifact must be regular file")
        data = archive.read_bytes()
        if (
            len(data) != item["bytes"]
            or digest(data) != item["sha256"]
            or hashlib.sha1(data).hexdigest() != item["shasum"]
            or "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode() != item["integrity"]
        ):
            raise ValueError(f"original archive integrity mismatch: {name}")
    if candidate:
        # Hash receipts alone are not derivation evidence. Inspect original package
        # bytes without extracting paths or trusting a producer-supplied inventory.
        for item in artifacts:
            with tarfile.open(output / item["filename"], "r:gz") as archive:
                files = {}
                total = 0
                for member in archive:
                    total += member.size
                    if not member.isfile() or member.name in files or total > MAX_EXPANDED:
                        raise ValueError("unsafe candidate archive")
                    stream = archive.extractfile(member)
                    assert stream is not None
                    files[member.name] = stream.read(MAX_EXPANDED + 1)
            manifest = json.loads(files["package/package.json"])
            if manifest["name"] != item["name"] or manifest["version"] != runtime["version"]:
                raise ValueError("candidate package identity mismatch")
            if "platform" in item:
                pin = runtime["platforms"][item["platform"]]
                expected_names = {
                    "package/package.json",
                    "package/manifest.json",
                    "package/" + pin["executable"],
                    *("package/" + n for n in runtime["licenses"]),
                }
                if set(files) != expected_names or digest(files["package/" + pin["executable"]]) != pin["binarySha256"]:
                    raise ValueError("candidate companion identity mismatch")
                expected_manifest = {
                    "version": runtime["version"],
                    "uvVersion": runtime["uvVersion"],
                    "sha256": pin["binarySha256"],
                    "executable": pin["executable"],
                }
                if json.loads(files["package/manifest.json"]) != expected_manifest:
                    raise ValueError("candidate companion manifest mismatch")
                if any(digest(files["package/" + name]) != sha for name, sha in runtime["licenses"].items()):
                    raise ValueError("candidate license mismatch")
            else:
                expected_names = {
                    "package/package.json",
                    "package/runtime.json",
                    "package/launcher.cjs",
                    "package/README.md",
                    "package/LICENSE",
                    "package/wheel/" + payload["wheel"],
                }
                if set(files) != expected_names:
                    raise ValueError("candidate launcher inventory mismatch")
                expected_manifest = json.loads((SOURCE / "package.json").read_text())
                expected_manifest["version"] = runtime["version"]
                expected_manifest["optionalDependencies"] = {
                    p["package"]: runtime["version"] for p in runtime["platforms"].values()
                }
                if manifest != expected_manifest:
                    raise ValueError("candidate launcher manifest mismatch")
                for name, source_file in (
                    ("launcher.cjs", SOURCE / "launcher.cjs"),
                    ("README.md", SOURCE / "README.md"),
                    ("LICENSE", REPO / "LICENSE"),
                ):
                    if files["package/" + name] != source_file.read_bytes():
                        raise ValueError("candidate launcher source mismatch")
        launcher = next(item for item in artifacts if "platform" not in item)
        with tarfile.open(output / launcher["filename"], "r:gz") as archive:
            members = list(archive)
            names = [m.name for m in members]
            wheel_name = "package/wheel/" + payload["wheel"]
            if len(names) != len(set(names)) or names.count(wheel_name) != 1:
                raise ValueError("candidate archive inventory mismatch")
            for name, expected in (("package/runtime.json", runtime),):
                stream = archive.extractfile(name)
                if stream is None or json.loads(stream.read()) != expected:
                    raise ValueError("candidate packed runtime mismatch")
            member = archive.getmember(wheel_name)
            if not member.isfile() or member.size > MAX_DOWNLOAD:
                raise ValueError("candidate wheel must be bounded regular file")
            stream = archive.extractfile(member)
            assert stream is not None
            data = stream.read(MAX_DOWNLOAD + 1)
        if digest(data) != payload["sha256"] or len(data) != payload["bytes"]:
            raise ValueError("candidate packed wheel identity mismatch")
        import tempfile

        with tempfile.TemporaryDirectory() as scratch:
            wheel = Path(scratch) / payload["wheel"]
            wheel.write_bytes(data)
            validate_wheel(wheel, runtime["payloadVersion"])
            audit_candidate_wheel(wheel, runtime["payloadVersion"], paths)
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", action="store_true", help="unreleased clean current-source candidate only")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--verify-artifacts", type=Path)
    parser.add_argument("--allow-dirty", action="store_true", help="local evidence only, records dirty provenance")
    args = parser.parse_args()
    if args.verify_artifacts:
        metadata = verify_artifacts(args.verify_artifacts.resolve(), candidate=args.candidate)
        print(json.dumps({"status": "verified", "sourceCommit": metadata["sourceCommit"], "archives": 6}))
        return
    if args.out is None:
        parser.error("--out or --verify-artifacts required")
    output = args.out.resolve()
    if output == REPO or REPO in output.parents:
        parser.error("generated artifacts must be outside the source checkout")
    identity, paths = source_snapshot(args.allow_dirty)
    output.mkdir(parents=True, exist_ok=True)
    downloads = output / "downloads"
    downloads.mkdir(exist_ok=True)
    packages = output / "packages"
    packages.mkdir(exist_ok=True)
    runtime = json.loads((SOURCE / "runtime.json").read_text())
    for platform, pin in runtime["platforms"].items():
        if not isinstance(pin.get("binarySha256"), str) or len(pin["binarySha256"]) != 64:
            raise ValueError(f"missing executable SHA-256 pin: {platform}")
    builder = build_candidate_payload if args.candidate else build_payload
    wheel, build_tools = builder(output, runtime, identity, paths)
    tag = runtime["uvVersion"]
    licenses = {
        name: download(
            f"https://raw.githubusercontent.com/astral-sh/uv/{tag}/{name}",
            downloads / name,
            checksum,
        )
        for name, checksum in runtime["licenses"].items()
    }
    artifacts = []
    for platform, pin in runtime["platforms"].items():
        archive = downloads / pin["asset"]
        download(
            f"https://github.com/astral-sh/uv/releases/download/{tag}/{pin['asset']}",
            archive,
            pin["archiveSha256"],
        )
        binary = executable_from_archive(archive, pin["executable"])
        binary_hash = digest(binary)
        if pin["binarySha256"] != binary_hash:
            raise ValueError(f"executable SHA-256 mismatch: {platform}")
        pin["binarySha256"] = binary_hash
        package = packages / platform
        package.mkdir(exist_ok=True)
        executable = package / pin["executable"]
        executable.write_bytes(binary)
        executable.chmod(0o755)
        for name, data in licenses.items():
            (package / name).write_bytes(data)
        system, cpu = platform.split("-")
        manifest = {
            "name": pin["package"],
            "version": runtime["version"],
            "publishConfig": {"access": "public"},
            "repository": {
                "type": "git",
                "url": "git+https://github.com/escoffier-labs/brigade.git",
                "directory": "npm/feasibility",
            },
            "license": "MIT OR Apache-2.0",
            "os": [system],
            "cpu": [cpu],
            "main": "manifest.json",
            "files": [pin["executable"], "manifest.json", *licenses],
        }
        if system == "linux":
            manifest["libc"] = ["glibc"]
        write_json(package / "package.json", manifest)
        write_json(
            package / "manifest.json",
            {
                "version": runtime["version"],
                "uvVersion": tag,
                "sha256": binary_hash,
                "executable": pin["executable"],
            },
        )
        artifact = pack(package, output)
        artifact.update(
            platform=platform,
            archiveSha256=pin["archiveSha256"],
            archiveBytes=archive.stat().st_size,
            binarySha256=binary_hash,
            binaryBytes=len(binary),
        )
        artifacts.append(artifact)
    launcher = packages / "launcher"
    launcher.mkdir(exist_ok=True)
    for name in ("package.json", "launcher.cjs", "README.md"):
        shutil.copyfile(SOURCE / name, launcher / name)
    shutil.copyfile(REPO / "LICENSE", launcher / "LICENSE")
    (launcher / "launcher.cjs").chmod(0o755)
    (launcher / "wheel").mkdir()
    shutil.copyfile(wheel, launcher / "wheel" / wheel.name)
    if args.candidate:
        manifest = json.loads((launcher / "package.json").read_text())
        manifest["version"] = runtime["version"]
        manifest["optionalDependencies"] = {p["package"]: runtime["version"] for p in runtime["platforms"].values()}
        write_json(launcher / "package.json", manifest)
    write_json(launcher / "runtime.json", runtime)
    artifacts.append(pack(launcher, output))
    write_json(output / "runtime.json", runtime)
    write_json(
        output / "artifacts.json",
        {
            "runtime": runtime,
            "artifacts": artifacts,
            "licenses": runtime["licenses"],
            **identity,
            "buildTools": build_tools,
        },
    )
    print(json.dumps({"output": str(output), "artifacts": artifacts}, indent=2))


if __name__ == "__main__":
    main()
