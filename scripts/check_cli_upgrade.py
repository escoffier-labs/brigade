#!/usr/bin/env python3
"""Pre-upload gate: an installed ``brigade-cli`` must survive an in-place upgrade to the built wheel.

``pipx upgrade brigade-cli`` runs ``pip install --upgrade brigade-cli`` inside the app's venv. A
package whose upgrade writes ``brigade/`` and then lets the old release's uninstall delete those
files leaves every command broken. This gate reproduces that upgrade before anything is uploaded:
in a fresh venv it installs the newest stable ``brigade-cli`` older than the built version from
PyPI, upgrades it to the built wheel (``--find-links`` the same ``dist/`` so a dependency on a
just-built ``brigadestack`` resolves locally), then requires ``brigade --version`` to report the
built version and ``brigade-hook --help`` to run.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
import venv
from pathlib import Path

PACKAGE = "brigade-cli"
STABLE = re.compile(r"\d+\.\d+\.\d+")


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def previous_stable(releases: dict[str, list[dict]], current: str) -> str:
    """Return the newest stable, non-yanked release strictly older than ``current``."""
    candidates = [
        version
        for version, files in releases.items()
        if STABLE.fullmatch(version)
        and version_key(version) < version_key(current)
        and files
        and not all(item.get("yanked") for item in files)
    ]
    if not candidates:
        raise ValueError(f"no stable {PACKAGE} release older than {current} is on PyPI")
    return max(candidates, key=version_key)


def fetch_releases(package: str) -> dict[str, list[dict]]:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json", timeout=30) as response:  # noqa: S310
        return json.load(response)["releases"]


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(argv), flush=True)
    result = subprocess.run(argv, capture_output=True, text=True)
    for stream in (result.stdout, result.stderr):
        if stream.strip():
            print(stream.rstrip(), flush=True)
    return result


def check_upgrade(dist: Path, version: str, previous: str) -> list[str]:
    """Run the upgrade in a fresh venv and return the failures (empty when it passes)."""
    wheels = sorted(dist.glob(f"brigade_cli-{version}-*.whl"))
    if len(wheels) != 1:
        return [f"expected exactly one brigade_cli-{version} wheel in {dist}, found {[w.name for w in wheels]}"]
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="brigade-cli-upgrade-") as tmp:
        env_dir = Path(tmp) / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
        bin_dir = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
        suffix = ".exe" if sys.platform == "win32" else ""
        python = str(bin_dir / f"python{suffix}")
        if run([python, "-m", "pip", "install", "--quiet", f"{PACKAGE}=={previous}"]).returncode != 0:
            return [f"could not install {PACKAGE}=={previous} from PyPI"]
        old = run([str(bin_dir / f"brigade{suffix}"), "--version"])
        if old.returncode != 0:
            return [f"the previous release {PACKAGE}=={previous} did not run before the upgrade"]
        upgrade = run(
            [python, "-m", "pip", "install", "--upgrade", "--find-links", str(dist.resolve()), str(wheels[0].resolve())]
        )
        if upgrade.returncode != 0:
            return [f"pip could not upgrade {PACKAGE}=={previous} to {wheels[0].name}"]
        reported = run([str(bin_dir / f"brigade{suffix}"), "--version"])
        if reported.returncode != 0 or version not in reported.stdout.split():
            failures.append(
                f"`brigade --version` after the upgrade exited {reported.returncode} and printed "
                f"{reported.stdout.strip()!r}, expected version {version}"
            )
        hook = run([str(bin_dir / f"brigade-hook{suffix}"), "--help"])
        if hook.returncode != 0:
            failures.append(f"`brigade-hook --help` after the upgrade exited {hook.returncode}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=Path("dist"), help="directory holding the built wheels")
    parser.add_argument("--version", required=True, help="version the built brigade-cli wheel must report")
    parser.add_argument("--previous", help="release to upgrade from (default: newest older stable on PyPI)")
    args = parser.parse_args()
    try:
        previous = args.previous or previous_stable(fetch_releases(PACKAGE), args.version)
    except (OSError, ValueError, KeyError) as exc:
        print(f"::error::cannot resolve the release to upgrade from: {exc}", file=sys.stderr)
        return 1
    print(f"upgrading {PACKAGE}=={previous} -> {args.version}", flush=True)
    failures = check_upgrade(args.dist, args.version, previous)
    for failure in failures:
        print(f"::error::{failure}", file=sys.stderr)
    if failures:
        return 1
    print(f"ok: {PACKAGE}=={previous} upgrades to {args.version} and still runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
