"""S10 pre-publication acceptance for one built `brigade-hub` artifact.

The release job runs this on each target's native runner before anything is
published, with BRIGADE_HUB_BINARY naming the artifact it built. Every case
starts the hub the way an operator does, through
`brigade fleet serve --engine rust`, which checks the binary's SHA-256 first:

- the binary reports its version without starting anything;
- startup, `/ready`, and a clean stop;
- the frozen contract drive with registration, claims and messages native;
- an upgrade from the last stable release's schema fixture;
- a start killed while it migrates an old schema, then a normal start;
- an online backup while serving, restored with `brigade fleet restore`,
  served again with every row readable;
- refusal of a database with an unknown newer `user_version`.

Run with a Python whose isolated mode (`-I`) imports this checkout's
`brigade`, since brigade-hub starts its upstream that way.
"""

import hashlib
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "registry/skills/verify-brigade"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hub_contract as hc  # noqa: E402 - repository-local drive helper
from engine_rollback import lost_rows, rows  # noqa: E402 - shared row comparison

from brigade import fleet_hub  # noqa: E402

BINARY = Path(os.environ.get("BRIGADE_HUB_BINARY", ROOT / "engines/hub/target/debug/brigade-hub")).absolute()
FIXTURES = ROOT / "engines/hub/schema/upgrade-fixtures"
BRIGADE = [sys.executable, "-m", "brigade"]
NATIVE = "--route-mode registration=native --route-mode claims=native --route-mode messages=native"
# Tables `brigade fleet restore` changes on purpose: fences and revisions move
# forward and live claims are fenced.
RESTORE_ADVANCES = {"claims", "claim_fences", "fleet_policy_meta", "model_roster_meta"}


def engine_args() -> list[str]:
    digest = hashlib.sha256(BINARY.read_bytes()).hexdigest()
    return ["fleet", "serve", "--engine", "rust", "--hub-binary", str(BINARY), "--hub-sha256", digest]


def template() -> str:
    return (
        "{brigade} " + " ".join(engine_args()) + " --host {host} --port {port} --db {db} --token-file {token_file} "
    ) + NATIVE


def launch(state: Path):
    return hc.launch_hub(template(), brigade=BRIGADE, state_dir=state, startup_timeout=60)


def stop(hub) -> tuple[int, str]:
    hc.stop_hub(hub)
    return hub.process.returncode, hub.log.read_text(errors="replace")


def from_fixture(db: Path, name: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.executescript((FIXTURES / name).read_text(encoding="utf-8"))


def user_version(db: Path) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


class Prepublish(unittest.TestCase):
    def ready(self, hub):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if hc.request(hub.url, "GET", "/ready").status == 200:
                return
            time.sleep(0.2)
        self.fail("/ready never answered 200")

    def stop_clean(self, hub):
        code, log = stop(hub)
        self.assertEqual(code, 0, log[-2000:])
        self.assertIn("brigade fleet hub stopped cleanly", log)

    def test_version_starts_nothing(self):
        result = subprocess.run([str(BINARY), "--version"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("brigade-hub "), result.stdout)

    def test_startup_drive_and_clean_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = launch(Path(directory))
            try:
                self.ready(hub)
                result = hc.run_contract(hub)
                self.assertFalse(result["mismatches"], result["mismatches"])
            finally:
                self.stop_clean(hub)

    def test_upgrade_from_the_last_stable_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            from_fixture(state / "hub.db", "v0.30.0.sql")
            self.assertLess(user_version(state / "hub.db"), fleet_hub.SCHEMA_VERSION)
            hub = launch(state)
            try:
                self.ready(hub)
            finally:
                self.stop_clean(hub)
            self.assertEqual(user_version(state / "hub.db"), fleet_hub.SCHEMA_VERSION)

    def test_start_killed_while_migrating_then_a_normal_start(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            db = state / "hub.db"
            from_fixture(db, "schema-01.sql")
            (state / "home").mkdir()
            env = hc.hub_env(state, "prepublish-admin-token", hc.new_operator())
            argv = [*BRIGADE, *engine_args(), "--host", "127.0.0.1", "--port", "0", "--db", str(db)]
            # Kill the whole start at several points inside the migration window.
            for delay in (0.2, 0.5, 1.0):
                process = subprocess.Popen(
                    argv, env=env, cwd=state, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                time.sleep(delay)
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            hub = launch(state)
            try:
                self.ready(hub)
            finally:
                self.stop_clean(hub)
            self.assertEqual(user_version(db), fleet_hub.SCHEMA_VERSION)
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_online_backup_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            served = work / "served"
            served.mkdir()
            hub = launch(served)
            try:
                result = hc.run_contract(hub)
                self.assertFalse(result["mismatches"], result["mismatches"])
                backup = subprocess.run(
                    [*BRIGADE, "fleet", "backup", "--db", str(served / "hub.db"), "--dest", str(work / "backups")]
                    + ["--min-free-gib", "0.001", "--json"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(backup.returncode, 0, backup.stdout + backup.stderr)
            finally:
                self.stop_clean(hub)
            copies = list((work / "backups").rglob("hub.db"))
            self.assertEqual(len(copies), 1, backup.stdout)
            copied = rows(copies[0])
            self.assertTrue(copied["claims"], "the backup holds no claims")
            restored = work / "restored"
            restored.mkdir()
            restore = subprocess.run(
                [*BRIGADE, "fleet", "restore", "--copy", str(copies[0]), "--db", str(restored / "hub.db")]
                + ["--ledger", str(work / "ledger.json"), "--assert-no-prior-restore", "--json"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(restore.returncode, 0, restore.stdout + restore.stderr)
            hub = launch(restored)
            try:
                self.ready(hub)
                after = rows(restored / "hub.db")
                # The restore tool advances fences and revisions and fences
                # live claims, so a lossy restore cannot rewind them. Every
                # other row reads unchanged, and every fenced target remains.
                kept = {table: counted for table, counted in copied.items() if table not in RESTORE_ADVANCES}
                self.assertEqual(lost_rows(kept, after), [])
                with sqlite3.connect(copies[0]) as before, sqlite3.connect(restored / "hub.db") as now:
                    fences = "SELECT target, generation FROM claim_fences"
                    old = dict(before.execute(fences).fetchall())
                    new = dict(now.execute(fences).fetchall())
                self.assertEqual(set(old), set(new))
                self.assertTrue(all(new[target] >= generation for target, generation in old.items()))
            finally:
                self.stop_clean(hub)

    def test_unknown_newer_user_version_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            db = state / "hub.db"
            fleet_hub.init_db(db).close()
            with sqlite3.connect(db) as conn:
                conn.execute(f"PRAGMA user_version={fleet_hub.SCHEMA_VERSION + 1}")
            (state / "home").mkdir()
            env = hc.hub_env(state, "prepublish-admin-token", hc.new_operator())
            argv = [*BRIGADE, *engine_args(), "--host", "127.0.0.1", "--port", "0", "--db", str(db)]
            result = subprocess.run(argv, env=env, cwd=state, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(f"has schema version {fleet_hub.SCHEMA_VERSION + 1}", result.stderr)
            self.assertNotIn("listening on", result.stdout + result.stderr)
            self.assertEqual(user_version(db), fleet_hub.SCHEMA_VERSION + 1)

    def test_negative_an_altered_binary_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "home").mkdir()
            env = hc.hub_env(state, "prepublish-admin-token", hc.new_operator())
            argv = [*BRIGADE, *engine_args(), "--host", "127.0.0.1", "--port", "0", "--db", str(state / "hub.db")]
            argv[argv.index("--hub-sha256") + 1] = "0" * 64
            result = subprocess.run(argv, env=env, cwd=state, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("does not match the checksum", result.stderr)
            self.assertFalse((state / "hub.db").exists(), "the refused start touched the database")


if __name__ == "__main__":
    unittest.main()
