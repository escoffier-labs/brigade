# content-guard: allow private-ipv4 file
# content-guard: allow loopback-ipv4 file
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[2]


class AuditCliTests(unittest.TestCase):
    """End-to-end CLI tests for `content-guard audit`."""

    def test_audit_tracked_scope_counts_findings_in_tracked_files_only(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            (repo / "tracked.md").write_text("Service runs on 192.168.99.10.\n")
            subprocess.run(["git", "add", "tracked.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add tracked"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            (repo / "untracked.md").write_text("Other service on 192.168.99.20.\n")

            proc = self._audit(repo, str(repo), "--scope", "tracked", "--json")

        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        # Two files exist on disk; only one is tracked.
        self.assertEqual(payload["summary"]["files_scanned"], 2)
        # README.md is tracked too (from _init_repo) but has no findings.
        self.assertEqual(payload["summary"]["files_with_findings"], 1)
        self.assertEqual(payload["summary"]["total_findings"], 1)
        offenders = {entry["path"] for entry in payload["top_offenders"]}
        self.assertEqual(offenders, {"tracked.md"})

    def test_audit_tree_scope_counts_all_files(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            (repo / "tracked.md").write_text("Service runs on 192.168.99.10.\n")
            subprocess.run(["git", "add", "tracked.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add tracked"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            (repo / "untracked.md").write_text("Other service on 192.168.99.20.\n")

            proc = self._audit(repo, str(repo), "--scope", "tree", "--json")

        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        # README.md, tracked.md, untracked.md
        self.assertEqual(payload["summary"]["files_scanned"], 3)
        self.assertEqual(payload["summary"]["files_with_findings"], 2)
        self.assertEqual(payload["summary"]["total_findings"], 2)
        offenders = {entry["path"] for entry in payload["top_offenders"]}
        self.assertEqual(offenders, {"tracked.md", "untracked.md"})

    def test_audit_json_output_has_expected_shape(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            (repo / "leak.md").write_text("Host: 192.168.99.10 and 172.16.0.5.\n")
            subprocess.run(["git", "add", "leak.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add leak"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

            proc = self._audit(repo, str(repo), "--scope", "tracked", "--json")

        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("summary", payload)
        self.assertIn("by_category", payload)
        self.assertIn("by_rule", payload)
        self.assertIn("top_offenders", payload)
        # Summary shape
        for key in ("target", "scope", "files_scanned", "files_with_findings", "total_findings", "blocked"):
            self.assertIn(key, payload["summary"])
        # by_rule is a list of {rule_id, count}
        self.assertTrue(payload["by_rule"], "expected at least one rule in by_rule")
        for entry in payload["by_rule"]:
            self.assertIn("rule_id", entry)
            self.assertIn("count", entry)
        # top_offenders entries have path/findings/blocked
        self.assertTrue(payload["top_offenders"], "expected at least one offender")
        for entry in payload["top_offenders"]:
            self.assertIn("path", entry)
            self.assertIn("findings", entry)
            self.assertIn("blocked", entry)

    def test_audit_text_output_includes_section_headers(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            (repo / "leak.md").write_text("Host: 192.168.99.10.\n")
            subprocess.run(["git", "add", "leak.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add leak"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

            proc = self._audit(repo, str(repo), "--scope", "tracked")

        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("Summary", proc.stdout)
        self.assertIn("By category", proc.stdout)
        self.assertIn("Top offenders", proc.stdout)

    def test_audit_strict_flag_exits_1_on_blocking_findings(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            (repo / "leak.md").write_text("Host: 192.168.99.10.\n")
            subprocess.run(["git", "add", "leak.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add leak"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

            proc = self._audit(repo, str(repo), "--scope", "tracked", "--strict", "--json")

        self.assertEqual(proc.returncode, 1, msg=proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["summary"]["blocked"])

    def test_audit_exits_zero_without_strict(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            (repo / "leak.md").write_text("Host: 192.168.99.10.\n")
            subprocess.run(["git", "add", "leak.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add leak"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

            proc = self._audit(repo, str(repo), "--scope", "tracked", "--json")

        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        # Even though there are blocking findings, audit is non-blocking by default.
        self.assertTrue(payload["summary"]["blocked"])

    def _init_repo(self, repo: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Example User"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "user@example"], cwd=repo, check=True)
        (repo / "README.md").write_text("example\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "feat: example"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def _audit(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "brigade.guard", "audit", *args],
            cwd=cwd,
            env={"PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
