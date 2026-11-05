"""Recapture historical hub layouts using each revision's real init_db.

Run through Brigade from the repository root, then lockstep.py --generate.
"""

import io
import os
import pathlib
import re
import sqlite3
import subprocess
import tarfile
import tempfile

root = pathlib.Path(__file__).resolve().parents[3]
out = root / "engines/hub/schema/upgrade-fixtures"
seen = set()
for commit in subprocess.check_output(
    ["git", "log", "--format=%H", "--", "src/brigade/fleet_hub.py"], encoding="utf-8"
).splitlines():
    source = subprocess.run(
        ["git", "show", f"{commit}:src/brigade/fleet_hub.py"], capture_output=True, encoding="utf-8"
    )
    match = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)", source.stdout, re.M)
    if not match:
        continue
    version = int(match[1])
    if version in seen:
        continue
    seen.add(version)
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        data = subprocess.check_output(["git", "archive", commit, "src/brigade"])
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(base, filter="data")
        (base / "src/brigade/fleet_hub.py").write_text(source.stdout, encoding="utf-8")
        db = base / "hub.db"
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
        (out / f"schema-{version:02}.sql").write_text(
            f"-- Captured from git show {commit}:src/brigade/fleet_hub.py via init_db.\n"
            + ddl
            + f"PRAGMA user_version={version};\n",
            encoding="utf-8",
        )
        c.close()
        print(version, commit, flush=True)
