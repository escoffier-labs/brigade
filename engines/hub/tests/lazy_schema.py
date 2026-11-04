"""Managed-front lazy DDL must preserve readiness, node writes and restart."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "registry/skills/verify-brigade"))
import hub_contract as hc

BINARY = ROOT / "engines/hub/target/debug/brigade-hub"


class LazySchema(unittest.TestCase):
    def test_mcp_report_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            template = f"{BINARY} --host {{host}} --port {{port}} --db {{db}} --token-file {{token_file}}"
            for phase in range(2):
                hub = hc.launch_hub(template, brigade=[sys.executable, "-m", "brigade"], state_dir=work, startup_timeout=30)
                try:
                    if phase == 0:
                        added = hc.request(hub.url, "POST", "/nodes", token=hub.admin_token,
                                           body={"action": "add", "node_id": "schema-probe"})
                        self.assertEqual(added.status, 200, added.body)
                        token = added.json["token"]
                        report = hc.request(hub.url, "POST", "/mcp/report", token=token, body={
                            "schema_version": 1, "brigade_version": "0.0.0",
                            "observed_at": "2026-01-01T00:00:00+00:00", "catalog_present": False,
                            "servers": [], "harnesses": [],
                        })
                        self.assertEqual(report.status, 200, report.body)
                    # Cross at least one maintenance revalidation tick.
                    time.sleep(1.5)
                    self.assertEqual(hc.request(hub.url, "GET", "/ready").status, 200)
                    written = hc.request(hub.url, "POST", "/events", token=token, body=[{
                        "node_id": "schema-probe", "run_id": "schema-probe-run", "sequence": phase,
                        "state": "run.started", "ts": "2026-01-01T00:00:00+00:00",
                        "digest": f"schema-probe-{phase}", "repo": "example-repo", "seat": "coder", "harness": "codex",
                    }])
                    self.assertEqual(written.status, 200, written.body)
                    self.assertEqual(hc.request(hub.url, "GET", "/ready").status, 200)
                finally:
                    hc.stop_hub(hub)


if __name__ == "__main__":
    unittest.main()
