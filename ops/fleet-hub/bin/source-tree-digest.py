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
    top: str | None = None
    with tarfile.open(fileobj=archive, mode="r|*") as tar:
        for member in tar:
            if not (member.isdir() or member.isfile() or member.issym()):
                raise ValueError(f"Unsupported archive member: {member.name!r}")
            # Every member sits under one top folder, which tar --strip-components=1 and pip
            # both strip. A member outside it would be skipped here but still extracted or
            # built (pip stops stripping when top folders differ), so it fails the digest.
            name = member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name
            parts = name.split("/")
            if "\\" in name or "\0" in name or any(part in ("", ".", "..") for part in parts):
                raise ValueError(f"Unsafe archive path: {member.name!r}")
            if top is None:
                top = parts[0]
            if parts[0] != top:
                raise ValueError(f"Archive member outside the top folder {top!r}: {member.name!r}")
            if len(parts) == 1:
                if not member.isdir():
                    raise ValueError(f"Archive top level must be one folder: {member.name!r}")
                continue
            rel = "/".join(parts[1:])
            if member.isdir():
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
    # git archive applies the local line-ending settings, so a Windows checkout with
    # core.autocrlf=true would write CRLF files and give a different pin. GitHub builds
    # its archives without them, so turn them off to match.
    command = [
        "git",
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.eol=lf",
        "archive",
        "--format=tar",
        "--prefix=source/",
        commit,
    ]
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
