"""The deployment p99 uses every commit rather than sampled last-batch gauges."""

import json
from pathlib import Path
import tempfile
import unittest
from heartbeat import claim_summary, commit_p99


class CommitHistogram(unittest.TestCase):
    def test_window_subtracts_prior_commits_and_includes_overflow(self):
        def buckets(a, b, total):
            return {
                f'brigade_hub_writer_commit_duration_seconds_bucket{{le="{bound}"}}': count
                for bound, count in (("0.002", a), ("0.05", b), ("+Inf", total))
            }

        self.assertIsNone(commit_p99({}, {}))
        self.assertIsNone(commit_p99(buckets(100, 100, 100), buckets(100, 100, 100)))
        self.assertEqual(commit_p99(buckets(100, 100, 100), buckets(101, 200, 200)), 50)
        self.assertEqual(commit_p99({}, buckets(0, 1, 100)), "above 30000 ms")


class MixedClaims(unittest.TestCase):
    def test_claim_summary_reads_only_claims(self):
        records = [
            {"path": "/preference", "status": 200, "latency_ms": 900.0, "error": None},
            {"path": "/claims", "status": 200, "latency_ms": 10.0, "error": None},
            {"path": "/claims", "status": 503, "latency_ms": 30.0, "error": "busy"},
            {"path": "/claims", "status": None, "error": "TimeoutError"},
        ]
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "mixed.json"
            path.write_text(json.dumps(records), encoding="utf-8")
            summary = claim_summary(path)
        self.assertEqual(summary["attempts"], 3)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["statuses"], ["200", "503", "None"])
        self.assertEqual(summary["latency_ms"]["p99"], 30.0)
