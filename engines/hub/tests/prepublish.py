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
from engine_rollback import installed_binary, lost_rows, rows  # noqa: E402 - shared helpers

from brigade import fleet_hub  # noqa: E402

BINARY = Path(os.environ.get("BRIGADE_HUB_BINARY", ROOT / "engines/hub/target/debug/brigade-hub")).absolute()
FIXTURES = ROOT / "engines/hub/schema/upgrade-fixtures"
BRIGADE = [sys.executable, "-m", "brigade"]
NATIVE = "--route-mode registration=native --route-mode claims=native --route-mode messages=native"
# Tables `brigade fleet restore` changes on purpose: fences and revisions move
# forward and live claims are fenced.
RESTORE_ADVANCES = {"claims", "claim_fences", "fleet_policy_meta", "model_roster_meta"}


def engine_args() -> list[str]:
    binary = installed_binary()
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    return ["fleet", "serve", "--engine", "rust", "--hub-binary", str(binary), "--hub-sha256", digest]


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

    @staticmethod
    def parent_of(entry: Path) -> int | None:
        try:
            return int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def environ(entry: Path) -> bytes:
        try:
            return (entry / "environ").read_bytes()
        except OSError:
            return b""

    def hub_process(self, parent: int) -> int:
        """The brigade-hub child of `fleet serve` (pid `parent`)."""
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                ppid = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
                argv = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if ppid == parent and os.path.basename(argv[0]) == b"brigade-hub":
                return int(entry.name)
        self.fail("no brigade-hub process under fleet serve")

    def test_startup_drive_and_clean_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = launch(Path(directory))
            try:
                self.ready(hub)
                # It ran from the checked descriptor and re-executed through
                # its own image, keeping its name for process listings.
                pid = self.hub_process(hub.process.pid)
                image = installed_binary()
                self.assertTrue(os.path.samefile(f"/proc/{pid}/exe", image))
                # The descriptor the re-exec ran through is closed, and the
                # Python upstream never inherits its variable. Never print an
                # environment: it can hold credentials.
                held = [fd for fd in Path(f"/proc/{pid}/fd").iterdir() if fd.resolve() == image.resolve()]
                self.assertEqual(held, [], "brigade-hub still holds a descriptor of its own image")
                upstream = [
                    child
                    for child in Path("/proc").iterdir()
                    if child.name.isdigit() and self.parent_of(child) == pid
                ]
                leaked = any(b"BRIGADE_HUB_EXE_FD=" in self.environ(child) for child in upstream)
                self.assertFalse(leaked, "a child of brigade-hub inherited BRIGADE_HUB_EXE_FD")
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

    @unittest.skipIf(os.geteuid() == 0, "root reads an execute-only file, so the open cannot be made to fail")
    def test_without_its_image_descriptor_the_hub_exits_and_execs_nothing(self):
        """No pathname fallback: if /proc/self/exe cannot be opened for the
        descriptor-limit re-exec, brigade-hub exits 2 before running anything."""
        import shutil

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            marker = work / "execed"
            python = work / "python"
            python.write_text(f"#!/bin/sh\necho ran > {marker}\nexit 0\n")
            python.chmod(0o755)
            # Execute-only: exec needs only the execute bit, but opening the
            # image for reading, as /proc/self/exe does, is refused.
            hub = work / "brigade-hub"
            shutil.copyfile(BINARY, hub)
            hub.chmod(0o111)
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "BRIGADE_HUB_PYTHON": str(python)}
            argv = [str(hub), "--host", "127.0.0.1", "--port", "0", "--db", str(work / "hub.db")]
            result = subprocess.run(argv, env=env, cwd=work, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stderr[-500:])
            self.assertIn(b"cannot open its own image (/proc/self/exe)", result.stderr)
            self.assertFalse(marker.exists(), "brigade-hub executed something without its image descriptor")
            # Control: a readable image re-execs through the interpreter, which
            # the marker records, so the check above can fail.
            hub.chmod(0o755)
            subprocess.run(argv, env=env, cwd=work, capture_output=True, timeout=30)
            self.assertTrue(marker.exists(), "the control run did not exec the interpreter")

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
