"""The deployment p99 uses every commit rather than sampled last-batch gauges."""

import unittest
from heartbeat import commit_p99


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
