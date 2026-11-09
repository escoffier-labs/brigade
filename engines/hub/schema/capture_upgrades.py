"""Recapture historical hub layouts using each revision's real init_db.

Run through Brigade from the repository root, then lockstep.py --generate.
With --chains, only the multi-hop upgrade paths in CHAINS are recaptured.
"""

import io
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

root = pathlib.Path(__file__).resolve().parents[3]
out = root / "engines/hub/schema/upgrade-fixtures"

# A hub upgraded across releases keeps each release's ADD COLUMN order, which a
# single replay of an old capture through today's init_db cannot reproduce.
# Each chain opens one database with every listed revision's init_db in turn.
CHAINS = {
    # v2 claims (no lease columns) -> v3 appends lock_* -> v14 creates the
    # seat-keyed model_policy -> v15 appends reasoning, brigade_cli,
    # t3_instance_id, t3_service_tier -> v21 appends brigade_model. Today's
    # init_db then appends t3_model and claims' harness/role/job/session. This
    # is the column order observed on the production hub at schema v50.
    "chain-v02-v03-v14-v15-v21": [
        "11b3009d0c4fa9f572b7c18e5691adcc9ec1cf59",
        "b6fd4a07779dd1f545c86d0c2428d18ea1ee3fa9",
        "6bb785becae9fda8c2470115beb656d01c29d5db",
        "8d0372ec944e47bda502d8c337e5f4edd2242e7b",
        "e35f5e20c7ce1cfeec16ff86618aebf90c3f8935",
    ],
}


def init_at(commit: str, db: pathlib.Path) -> int:
    """Open db with the init_db of commit; return that revision's SCHEMA_VERSION."""
    source = subprocess.run(
        ["git", "show", f"{commit}:src/brigade/fleet_hub.py"], capture_output=True, encoding="utf-8", check=True
    ).stdout
    version = int(re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)", source, re.M)[1])
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        data = subprocess.check_output(["git", "archive", commit, "src/brigade"])
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(base, filter="data")
        r = subprocess.run(
            [
                "python3",
                "-c",
                "from brigade import fleet_hub; from pathlib import Path; import sys; fleet_hub.init_db(Path(sys.argv[1])).close()",
                str(db),
            ],
            cwd=d,
            env={**os.environ, "PYTHONPATH": str(base / "src")},
            capture_output=True,
            encoding="utf-8",
        )
        if r.returncode:
            raise RuntimeError(f"{version} {commit}: {r.stderr}")
    return version


def dump(db: pathlib.Path, name: str, header: str, version: int) -> None:
    c = sqlite3.connect(db)
    ddl = (
        ";\n".join(
            r[0]
            for r in c.execute(
                "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,name"
            )
        )
        + ";\n"
    )
    c.close()
    (out / f"{name}.sql").write_text(f"-- {header}\n" + ddl + f"PRAGMA user_version={version};\n", encoding="utf-8")


def history() -> None:
    seen = set()
    for commit in subprocess.check_output(
        ["git", "log", "--format=%H", "--", "src/brigade/fleet_hub.py"], encoding="utf-8"
    ).splitlines():
        source = subprocess.run(
            ["git", "show", f"{commit}:src/brigade/fleet_hub.py"], capture_output=True, encoding="utf-8"
        )
        match = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)", source.stdout, re.M)
        if not match or int(match[1]) in seen:
            continue
        seen.add(int(match[1]))
        with tempfile.TemporaryDirectory() as d:
            db = pathlib.Path(d) / "hub.db"
            version = init_at(commit, db)
            dump(db, f"schema-{version:02}", f"Captured from git show {commit}:src/brigade/fleet_hub.py via init_db.", version)
        print(version, commit, flush=True)


def chains() -> None:
    for name, commits in CHAINS.items():
        with tempfile.TemporaryDirectory() as d:
            db = pathlib.Path(d) / "hub.db"
            versions = [init_at(commit, db) for commit in commits]
            dump(db, name, f"Captured by chaining init_db of {' -> '.join(commits)}.", versions[-1])
        print(name, versions, flush=True)


if __name__ == "__main__":
    chains() if sys.argv[1:] == ["--chains"] else history()
