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


def wheel(tmp_path, *, metadata="", extra=None):
    filename = tmp_path / "brigade_cli-0.29.0.dev1-py3-none-any.whl"
    dist = "brigade_cli-0.29.0.dev1.dist-info"
    files = {
        "brigade/__init__.py": b'__version__ = "0.29.0.dev1"\n',
        f"{dist}/METADATA": (
            "Metadata-Version: 2.4\nName: brigade-cli\nVersion: 0.29.0.dev1\n"
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
