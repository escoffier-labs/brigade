"""Opt-in wire drive across rollback with live SQLite sidecars.

Run with BRIGADE_HUB_SLOW_ACCEPTANCE=1. The three full wire drives include
long-poll waits and run outside the normal PR test budget.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "registry/skills/verify-brigade"))
import hub_contract as hc

BINARY = Path(__file__).resolve().parents[1] / "target/debug/brigade-hub"


@unittest.skipUnless(
    os.environ.get("BRIGADE_HUB_SLOW_ACCEPTANCE") == "1",
    "slow acceptance flag required",
)
class Rollback(unittest.TestCase):
    def test_live_wal_rollback_and_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            keeper = None
            for template in [
                f"{BINARY} --host {{host}} --port {{port}} --db {{db}} --token-file {{token_file}}",
                hc.DEFAULT_HUB_CMD,
                f"{BINARY} --host {{host}} --port {{port}} --db {{db}} --token-file {{token_file}}",
            ]:
                hub = hc.launch_hub(
                    template,
                    brigade=[sys.executable, "-m", "brigade"],
                    state_dir=work,
                    startup_timeout=30,
                )
                try:
                    if keeper is None:
                        keeper = sqlite3.connect(work / "hub.db")
                        keeper.execute("PRAGMA wal_autocheckpoint=0")
                    result = hc.run_contract(hub)
                    self.assertFalse(result["mismatches"], result["mismatches"])
                    self.assertTrue((work / "hub.db-wal").exists())
                    self.assertTrue((work / "hub.db-shm").exists())
                    if str(BINARY) in template:
                        self.assertEqual(
                            hc.request(hub.url, "GET", "/ready").status, 200
                        )
                finally:
                    hc.stop_hub(hub)
            keeper.close()


if __name__ == "__main__":
    unittest.main()
