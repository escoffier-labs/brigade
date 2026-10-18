#!/usr/bin/env python3
"""Build private npm tarballs from pinned, verified official uv assets."""

from __future__ import annotations

import argparse
import base64
import csv
import email.parser
import io
import os
import re
import sys
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
        data = destination.read_bytes()
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
    result = subprocess.run(
        ["npm", "pack", "--ignore-scripts", "--json", "--pack-destination", str(output)],
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


def build_payload(output: Path, runtime: dict, identity: dict, paths: list[str]) -> tuple[Path, dict]:
    build = output / "build"
    build.mkdir(exist_ok=False)
    source = build / "source"
    for name in paths:
        origin = REPO / name
        if not origin.is_file() or digest(origin.read_bytes()) != identity["sourceSha256"][name]:
            raise ValueError(f"source changed during build: {name}")
        dest = source / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, dest)
    project = (source / "pyproject.toml").read_text().split("[project.optional-dependencies]")[0]
    if not re.search(r"(?m)^dependencies = \[\]$", project):
        raise ValueError("source project must declare dependencies = []")
    version = runtime["payloadVersion"]
    subprocess.run(
        [sys.executable, str(source / "scripts/prepare_dev_wheel.py"), version, "--root", str(source)], check=True
    )
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
    wheel_dir = build / "dist"
    env = {**os.environ, "SOURCE_DATE_EPOCH": str(identity["sourceDateEpoch"])}
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
        env=env,
        check=True,
    )
    wheels = list(wheel_dir.iterdir())
    if len(wheels) != 1:
        raise ValueError("build must produce exactly one wheel")
    payload = validate_wheel(wheels[0], version)
    payload.update({key: identity[key] for key in ("sourceCommit", "sourceTree", "sourceIdentity")})
    runtime["payload"] = payload
    return wheels[0], {
        "versions": tools,
        "requirementsSha256": digest(requirements.read_bytes()),
        "sourceDateEpoch": identity["sourceDateEpoch"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allow-dirty", action="store_true", help="local evidence only, records dirty provenance")
    args = parser.parse_args()
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
    wheel, build_tools = build_payload(output, runtime, identity, paths)
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
            "private": True,
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
