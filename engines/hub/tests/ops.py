"""S9 HTTP drive and shadow housekeeping audit against a managed Python hub."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "registry/skills/verify-brigade"))
import hub_contract as hc
import hub_contract_ops
from transport import front, request


class Ops(unittest.TestCase):
    def test_metrics_admin_only_drive(self):
        with front() as (_, port):
            hub_contract_ops.drive(hc.Hub(url=f"http://{hc.LOOPBACK}:{port}", admin_token="fake-admin-token", mode="external"))

    def test_every_enabled_shadow_route_has_no_python_housekeeping_writes(self):
        # Health is the only enabled shadow family until S5-S8 supply their
        # native handlers and per-route housekeeping audits. Rules list it.
        self.assertEqual(set(hub_contract_ops.RULES["families"]), {"health"})
        with front(extra=("--route-mode", "health=shadow")) as (child, port):
            db = child.args[child.args.index("--db") + 1]
            watch = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            self.addCleanup(watch.close)
            before = watch.execute("PRAGMA data_version").fetchone()[0]
            status, _, body = request(port, "/health")
            self.assertEqual(watch.execute("PRAGMA data_version").fetchone()[0], before)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"ok": True, "service": "brigade-fleet-hub"})
            status, _, body = request(port, "/metrics", headers={"Authorization": "Bearer fake-admin-token"})
            self.assertEqual(status, 200)
            self.assertIn(b'brigade_hub_shadow_comparisons_total{family="health",result="match"}', body)
            # The front owns reader and CPU pools but no writer: the Python upstream commits.
            self.assertIn(b"\nbrigade_hub_reader_queue_depth 0\n", body)
            self.assertIn(b"\nbrigade_hub_cpu_queue_depth 0\n", body)
            self.assertIn(b"\nbrigade_hub_writer_queue_depth NaN\n", body)
            self.assertNotIn(b"Not yet wired", body)


if __name__ == "__main__":
    unittest.main()
