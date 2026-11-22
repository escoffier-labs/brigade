#!/usr/bin/env python3
"""Compute the Fleet Hub BRIGADE_SHA256 source tree digest."""

import argparse
import hashlib
import subprocess
import tarfile
import urllib.request
from typing import BinaryIO


def source_tree_digest(archive: BinaryIO) -> str:
    """Hash the files and symlinks in a tar stream after stripping its top folder."""
    rows: dict[str, bytes] = {}
    with tarfile.open(fileobj=archive, mode="r|*") as tar:
        for member in tar:
            _, _, rel = member.name.partition("/")
            if member.name.startswith("/") or rel.startswith("/") or ".." in rel.split("/"):
                raise ValueError(f"Unsafe archive path: {member.name!r}")
            if not (member.isdir() or member.isfile() or member.issym()):
                raise ValueError(f"Unsupported archive member: {member.name!r}")
            if member.isdir() or not rel:
                continue
            if rel in rows:
                raise ValueError(f"Duplicate archive path: {rel!r}")
            if member.issym():
                rows[rel] = b"l" + member.linkname.encode()
            else:
                content = tar.extractfile(member)
                assert content is not None
                with content:
                    file_digest = hashlib.file_digest(content, "sha256").hexdigest()
                executable = b"x" if member.mode & 0o111 else b"-"
                rows[rel] = b"f" + executable + file_digest.encode()

    digest = hashlib.sha256()
    for rel, value in sorted(rows.items()):
        digest.update(rel.encode() + b"\0" + value + b"\n")
    return digest.hexdigest()


def digest_ref(ref: str, repo_dir: str) -> str:
    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], cwd=repo_dir, text=True
    ).strip()
    command = ["git", "archive", "--format=tar", "--prefix=source/", commit]
    with subprocess.Popen(command, cwd=repo_dir, stdout=subprocess.PIPE) as process:
        assert process.stdout is not None
        with process.stdout:
            digest = source_tree_digest(process.stdout)
            # Drain the tar padding before closing the pipe and checking Git's exit status.
            while process.stdout.read(64 * 1024):
                pass
        if process.wait():
            raise subprocess.CalledProcessError(process.returncode, command)
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--ref", help="Compute the digest from a local Git commit.")
    inputs.add_argument("--url", help="Compute the digest from an archive URL.")
    inputs.add_argument("archive", nargs="?", help="Compute the digest from a local tar file.")
    parser.add_argument("--repo-dir", default=".", help="Git repository for --ref (default: current directory).")
    args = parser.parse_args()
    try:
        if args.ref is not None:
            digest = digest_ref(args.ref, args.repo_dir)
        elif args.url is not None:
            with urllib.request.urlopen(args.url, timeout=60) as archive:
                digest = source_tree_digest(archive)
        else:
            with open(args.archive, "rb") as archive:
                digest = source_tree_digest(archive)
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(digest)


if __name__ == "__main__":
    main()
