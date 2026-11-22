"""Fleet Hub source pins cover the files, executable bits and symlink targets."""

import hashlib
import importlib.util
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops/fleet-hub/bin/source-tree-digest.py"
SPEC = importlib.util.spec_from_file_location("source_tree_digest", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def archive(entries, *, mode="w"):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode=mode) as tar:
        for name, kind, permissions, value in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.mode = permissions
            if member.isfile():
                member.size = len(value)
                tar.addfile(member, io.BytesIO(value))
            else:
                member.linkname = value.decode()
                tar.addfile(member)
    data.seek(0)
    return data


def tree(prefix, *, executable=False, target=b"file", mode="w"):
    return archive(
        [
            (prefix, tarfile.DIRTYPE, 0o755, b""),
            (f"{prefix}/link", tarfile.SYMTYPE, 0o777, target),
            (f"{prefix}/file", tarfile.REGTYPE, 0o755 if executable else 0o644, b"hello\n"),
        ],
        mode=mode,
    )


def test_source_tree_digest_known_answer():
    rows = b"file\x00f-5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03\nlink\x00lfile\n"
    assert MODULE.source_tree_digest(tree("example")) == hashlib.sha256(rows).hexdigest()


def test_source_tree_digest_ignores_prefix_and_compression():
    assert MODULE.source_tree_digest(tree("old")) == MODULE.source_tree_digest(tree("new", mode="w:gz"))


def test_source_tree_digest_tracks_executable_bit_and_symlink_target():
    digest = MODULE.source_tree_digest(tree("example"))
    assert MODULE.source_tree_digest(tree("example", executable=True)) != digest
    assert MODULE.source_tree_digest(tree("example", target=b"other")) != digest


@pytest.mark.parametrize(
    ("name", "kind", "message"),
    [
        ("root/a/../b", tarfile.REGTYPE, "Unsafe archive path"),
        ("root//absolute", tarfile.REGTYPE, "Unsafe archive path"),
        ("/absolute", tarfile.REGTYPE, "Unsafe archive path"),
        ("root/file", tarfile.REGTYPE, "Duplicate archive path"),
        ("root/./b", tarfile.REGTYPE, "Unsafe archive path"),
        ("root/a\\b", tarfile.REGTYPE, "Unsafe archive path"),
        ("../evil", tarfile.REGTYPE, "Unsafe archive path"),
        ("root/hardlink", tarfile.LNKTYPE, "Unsupported archive member"),
        ("root/device", tarfile.CHRTYPE, "Unsupported archive member"),
        # pip builds from a top-level pyproject.toml once top folders differ, so a member
        # outside the single top folder must fail rather than be skipped.
        ("pyproject.toml", tarfile.REGTYPE, "outside the top folder"),
        ("other/setup.py", tarfile.REGTYPE, "outside the top folder"),
        ("other", tarfile.DIRTYPE, "outside the top folder"),
    ],
)
def test_source_tree_digest_rejects_invalid_entries(name, kind, message):
    data = archive([("root/file", tarfile.REGTYPE, 0o644, b"one"), (name, kind, 0o644, b"two")])
    with pytest.raises(ValueError, match=message):
        MODULE.source_tree_digest(data)


def test_source_tree_digest_rejects_a_top_level_file():
    with pytest.raises(ValueError, match="top level must be one folder"):
        MODULE.source_tree_digest(archive([("pyproject.toml", tarfile.REGTYPE, 0o644, b"x")]))


def test_digest_ref_matches_the_archive_of_the_same_commit(tmp_path):
    def git(*args, data=None):
        return (
            subprocess.run(["git", *args], cwd=tmp_path, input=data, check=True, capture_output=True)
            .stdout.decode()
            .strip()
        )

    # Plumbing writes the exact blob and symlink entries; a working-tree symlink and
    # `git add` store something else on Windows (core.symlinks, autocrlf).
    git("init", "-q")
    file_blob = git("hash-object", "-w", "--stdin", data=b"hello\n")
    link_blob = git("hash-object", "-w", "--stdin", data=b"file")
    git("update-index", "--add", "--cacheinfo", f"100644,{file_blob},file")
    git("update-index", "--add", "--cacheinfo", f"120000,{link_blob},link")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "t")
    expected = MODULE.source_tree_digest(tree("other"))
    assert MODULE.digest_ref("HEAD", str(tmp_path)) == expected
