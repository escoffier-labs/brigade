"""S10 rollback: from `fleet serve --engine rust` back to the Python engine.

Two paths, both from the release gate's rollback test:

- Same files: stop the Rust engine, start the Python engine on the same
  database, and the drive passes there with every row the Rust engine left
  still readable.
- Pinned backup, the planned rollback in the doc: fence the Rust engine by
  stopping it, take the final online backup (`brigade fleet backup`), and
  start the Python engine on that copy. Every row in the copy reads and the
  drive passes. Never a scheduled backup, which misses later writes.

The negative check proves the row comparison catches a lost row, so a
rollback that dropped data could not pass. Run with a Python whose isolated
mode (`-I`) imports this checkout's `brigade`, since brigade-hub starts its
upstream that way.
"""

import atexit
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "registry/skills/verify-brigade"))
import hub_contract as hc  # noqa: E402 - repository-local drive helper

BINARY = Path(os.environ.get("BRIGADE_HUB_BINARY", ROOT / "engines/hub/target/debug/brigade-hub"))
NATIVE = "--route-mode registration=native --route-mode claims=native --route-mode messages=native"
BRIGADE = [sys.executable, "-m", "brigade"]


_INSTALLED: list[Path] = []


def installed_binary() -> Path:
    """A private 0755 copy of BINARY, as an install leaves it.

    `fleet serve --engine rust` refuses a binary its group or others can
    write, which a build under umask 002 is.
    """
    if not _INSTALLED:
        directory = Path(tempfile.mkdtemp(prefix="brigade-hub-installed-"))
        atexit.register(shutil.rmtree, directory, ignore_errors=True)
        copy = directory / "brigade-hub"
        shutil.copyfile(BINARY, copy)
        copy.chmod(0o755)
        _INSTALLED.append(copy)
    return _INSTALLED[0]


def rust_template() -> str:
    binary = installed_binary()
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    return (
        f"{{brigade}} fleet serve --engine rust --hub-binary {binary} --hub-sha256 {digest} "
        f"--host {{host}} --port {{port}} --db {{db}} --token-file {{token_file}} {NATIVE}"
    )


def launch(state: Path, template: str):
    return hc.launch_hub(template, brigade=BRIGADE, state_dir=state, startup_timeout=60)


def rows(db: Path) -> dict[str, Counter]:
    """Every row of every table, as a multiset per table."""
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        tables = [
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {table: Counter(map(repr, conn.execute(f'SELECT * FROM "{table}"'))) for table in tables}


def lost_rows(before: dict[str, Counter], after: dict[str, Counter]) -> list[str]:
    """Rows in `before` that no longer read in `after`, named by table."""
    lost = []
    for table, counted in before.items():
        missing = counted - after.get(table, Counter())
        lost += [f"{table}: {row}" for row in missing.elements()]
    return lost


def stop(hub) -> tuple[int, str]:
    hc.stop_hub(hub)
    return hub.process.returncode, hub.log.read_text(errors="replace")


class EngineRollback(unittest.TestCase):
    def drive(self, hub):
        result = hc.run_contract(hub)
        self.assertFalse(result["mismatches"], result["mismatches"])

    def stop_rust(self, hub):
        code, log = stop(hub)
        self.assertEqual(code, 0, log[-2000:])
        self.assertIn("brigade fleet hub listening on", log)
        self.assertIn("brigade fleet hub stopped cleanly", log)

    def test_rollback_to_python_on_the_same_files(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            rust = launch(state, rust_template())
            try:
                self.drive(rust)
            finally:
                self.stop_rust(rust)
            before = rows(state / "hub.db")
            self.assertTrue(before["claims"], "the Rust engine wrote no claims to roll back")
            python = launch(state, hc.DEFAULT_HUB_CMD)
            try:
                self.assertEqual(lost_rows(before, rows(state / "hub.db")), [])
                self.drive(python)
            finally:
                stop(python)

    def test_rollback_to_python_on_a_pinned_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            state = work / "rust"
            state.mkdir()
            rust = launch(state, rust_template())
            try:
                self.drive(rust)
            finally:
                # Fence: nothing writes between the stop and the final backup.
                self.stop_rust(rust)
            backup = subprocess.run(
                [*BRIGADE, "fleet", "backup", "--db", str(state / "hub.db"), "--dest", str(work / "backups")]
                + ["--min-free-gib", "0.001", "--json"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(backup.returncode, 0, backup.stdout + backup.stderr)
            copies = list((work / "backups").rglob("hub.db"))
            self.assertEqual(len(copies), 1, backup.stdout)
            pinned = rows(copies[0])
            self.assertEqual(lost_rows(rows(state / "hub.db"), pinned), [], "the final backup lost rows")
            restored = work / "python"
            restored.mkdir()
            with sqlite3.connect(copies[0]) as source, sqlite3.connect(restored / "hub.db") as target:
                source.backup(target)
            python = launch(restored, hc.DEFAULT_HUB_CMD)
            try:
                self.assertEqual(lost_rows(pinned, rows(restored / "hub.db")), [])
                self.drive(python)
            finally:
                stop(python)

    def test_negative_a_lost_row_fails_the_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            rust = launch(state, rust_template())
            try:
                self.drive(rust)
            finally:
                self.stop_rust(rust)
            before = rows(state / "hub.db")
            damaged = state / "damaged.db"
            # The backup API carries rows still in the -wal file; a file copy would not.
            with sqlite3.connect(state / "hub.db") as source, sqlite3.connect(damaged) as target:
                source.backup(target)
            with sqlite3.connect(damaged) as conn:
                conn.execute("DELETE FROM claims WHERE rowid = (SELECT MIN(rowid) FROM claims)")
            lost = lost_rows(before, rows(damaged))
            self.assertEqual(len(lost), 1, lost)
            self.assertTrue(lost[0].startswith("claims: "), lost)


if __name__ == "__main__":
    unittest.main()
