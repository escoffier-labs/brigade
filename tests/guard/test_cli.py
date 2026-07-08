from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[2]


class CliTests(unittest.TestCase):
    def test_scan_directory_reports_findings(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clean.md").write_text("This is clean.\n")
            # content-guard: allow localhost-port
            (root / "leak.md").write_text("Service is localhost:5204.\n")

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard",
                    "scan",
                    str(root),
                    "--policy",
                    str(ROOT / "src" / "brigade" / "guard" / "policies" / "public-content.json"),
                    "--json",
                ],
                cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["blocked"])
        self.assertEqual(payload["files_scanned"], 2)
        self.assertEqual(len(payload["files"]), 1)
        self.assertEqual(payload["files"][0]["findings"][0]["rule_id"], "localhost-port")

    def test_scan_directory_skips_generated_dirs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # content-guard: allow localhost-port
            leak_text = "Service is localhost:5204.\n"
            (root / "README.md").write_text(leak_text)

            for excluded in (
                "node_modules/foo",
                ".git",
                "dist",
                "build",
                "coverage",
                ".next",
                ".venv",
                "__pycache__",
                "vendor/sub",
                ".claude",
            ):
                excluded_dir = root / excluded
                excluded_dir.mkdir(parents=True)
                (excluded_dir / "README.md").write_text(leak_text)

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard",
                    "scan",
                    str(root),
                    "--policy",
                    str(ROOT / "src" / "brigade" / "guard" / "policies" / "public-content.json"),
                    "--json",
                ],
                cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 1, msg=proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["files_scanned"], 1)
        self.assertEqual(len(payload["files"]), 1)
        self.assertTrue(payload["files"][0]["path"].endswith("README.md"))
        for entry in payload["files"]:
            for excluded in (
                "node_modules",
                ".git",
                "dist",
                "build",
                "coverage",
                ".next",
                ".venv",
                "__pycache__",
                "vendor",
                ".claude",
            ):
                self.assertNotIn(f"/{excluded}/", entry["path"])

    def test_pr_draft_helper_is_advisory_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "pr-body.md"
            # content-guard: allow all
            target.write_text("PR body with localhost:5204 and alice@solomonneas.dev\n")

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.pr_draft",
                    str(target),
                    "--policy",
                    str(ROOT / "src" / "brigade" / "guard" / "policies" / "pr-draft.json"),
                ],
                cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

            public_path = root / "pr-body.public.md"
            report_path = root / "pr-body.content-guard.json"

            self.assertEqual(proc.returncode, 0)
            self.assertIn("advisory=true", proc.stdout)
            self.assertTrue(public_path.exists())
            self.assertTrue(report_path.exists())
            self.assertIn("[redacted-service]", public_path.read_text())
            self.assertIn("<PRIVATE_EMAIL>", public_path.read_text())
            self.assertTrue(json.loads(report_path.read_text())["blocked"])

    def test_pr_draft_helper_strict_blocks(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "pr-body.md"
            # content-guard: allow localhost-port
            target.write_text("PR body with localhost:5204\n")

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.pr_draft",
                    str(target),
                    "--policy",
                    str(ROOT / "src" / "brigade" / "guard" / "policies" / "pr-draft.json"),
                    "--strict",
                ],
                cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 1)

    def test_pr_prepare_writes_publish_handoff_from_stdin(self) -> None:
        with TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "guarded"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.pr_prepare",
                    "--out-dir",
                    str(out_dir),
                    "--name",
                    "watchtower fix",
                    "--policy",
                    str(ROOT / "src" / "brigade" / "guard" / "policies" / "pr-draft.json"),
                    "--json",
                ],
                cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "src")},
                # content-guard: allow all
                input="PR body with localhost:5204 and alice@solomonneas.dev.\n",
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0)
            payload = json.loads(proc.stdout)
            publish_path = Path(payload["publish_body_file"])
            self.assertEqual(payload["source"], "<stdin>")
            self.assertTrue(payload["blocked"])
            self.assertTrue(payload["advisory"])
            self.assertEqual(publish_path, out_dir / "watchtower-fix.public.md")
            self.assertEqual(Path(payload["draft"]), out_dir / "watchtower-fix.draft.md")
            self.assertEqual(Path(payload["report"]), out_dir / "watchtower-fix.content-guard.json")
            self.assertIn("[redacted-service]", publish_path.read_text())
            self.assertIn("<PRIVATE_EMAIL>", publish_path.read_text())
            self.assertEqual(json.loads(Path(payload["report"]).read_text())["publish_body_file"], str(publish_path))

    def test_pr_prepare_strict_blocks(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "pr-body.md"
            # content-guard: allow localhost-port
            target.write_text("PR body with localhost:5204\n")

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.pr_prepare",
                    str(target),
                    "--out-dir",
                    str(Path(tmp) / "guarded"),
                    "--policy",
                    str(ROOT / "src" / "brigade" / "guard" / "policies" / "pr-draft.json"),
                    "--strict",
                ],
                cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 1)

    def test_git_commit_scan_blocks_coauthor_trailer(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Example User"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "user@example"], cwd=repo, check=True)
            (repo / "README.md").write_text("example\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-m",
                    "feat: example",
                    "-m",
                    "Co-authored-by: Other User <other@example>",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_commits",
                    "--range",
                    "HEAD",
                    "--policy",
                    str(ROOT / "src" / "brigade" / "guard" / "policies" / "public-repo.json"),
                    "--json",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["blocked"])
        self.assertEqual(payload["commits_with_findings"], 1)
        self.assertEqual(payload["commits"][0]["findings"][0]["rule_id"], "coauthored-by-trailer")

    def test_git_commit_scan_allows_clean_commit_message(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
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

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_commits",
                    "--range",
                    "HEAD",
                    "--policy",
                    str(ROOT / "src" / "brigade" / "guard" / "policies" / "public-repo.json"),
                    "--json",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["blocked"])
        self.assertEqual(payload["commits_with_findings"], 0)

    def test_git_commit_scan_redacts_report_subject(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Example User"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "user@example"], cwd=repo, check=True)
            (repo / "README.md").write_text("example\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(
                # content-guard: allow email
                ["git", "commit", "-m", "fix: contact person@solomonneas.dev"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_commits",
                    "--range",
                    "HEAD",
                    "--policy",
                    str(ROOT / "src" / "brigade" / "guard" / "policies" / "public-repo.json"),
                    "--json",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["commits"][0]["subject"], "fix: contact <PRIVATE_EMAIL>")

    def test_git_commit_scan_handles_empty_repository(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_commits",
                    "--policy",
                    str(ROOT / "src" / "brigade" / "guard" / "policies" / "public-repo.json"),
                    "--json",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["commits_scanned"], 0)
        self.assertEqual(payload["commits_with_findings"], 0)

    def test_publish_check_prepares_pr_body_advisory_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            pr_body = repo / "pr-body.md"
            # content-guard: allow all
            pr_body.write_text("PR body mentions localhost:5204 and person@solomonneas.dev.\n")

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.publish_check",
                    "--pr-body",
                    str(pr_body),
                    "--json",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0)
            payload = json.loads(proc.stdout)
            publish_body_file = repo / payload["publish_body_file"]
            self.assertTrue(payload["blocked"])
            self.assertFalse(payload["would_fail"])
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["checks"]["pr_body"]["advisory"])
            self.assertEqual(publish_body_file.name, "pr-body.public.md")
            self.assertIn("[redacted-service]", publish_body_file.read_text())
            self.assertIn("<PRIVATE_EMAIL>", publish_body_file.read_text())

    def test_publish_check_blocks_staged_file_findings(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            leak = repo / "leak.md"
            # content-guard: allow api-key-assignment
            leak.write_text("Temporary token=abc123abc123abc123abc123abc123.\n")
            subprocess.run(["git", "add", "leak.md"], cwd=repo, check=True)

            proc = subprocess.run(
                [sys.executable, "-m", "brigade.guard.publish_check", "--json"],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["blocked"])
        self.assertTrue(payload["would_fail"])
        self.assertTrue(payload["checks"]["staged_files"]["blocked"])
        self.assertEqual(payload["checks"]["staged_files"]["files"][0]["path"], "leak.md")

    def test_publish_check_advisory_only_reports_would_fail_but_exits_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            leak = repo / "leak.md"
            # content-guard: allow api-key-assignment
            leak.write_text("Temporary token=abc123abc123abc123abc123abc123.\n")
            subprocess.run(["git", "add", "leak.md"], cwd=repo, check=True)

            proc = subprocess.run(
                [sys.executable, "-m", "brigade.guard.publish_check", "--json", "--advisory-only"],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["blocked"])
        self.assertTrue(payload["would_fail"])
        self.assertTrue(payload["advisory_only"])

    def test_publish_check_blocks_commit_message_findings(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Example User"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "user@example"], cwd=repo, check=True)
            (repo / "README.md").write_text("example\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-m",
                    "feat: example",
                    "-m",
                    "Co-authored-by: Other User <other@example>",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

            proc = subprocess.run(
                [sys.executable, "-m", "brigade.guard.publish_check", "--json"],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["would_fail"])
        self.assertTrue(payload["checks"]["commit_messages"]["blocked"])
        self.assertEqual(
            payload["checks"]["commit_messages"]["commits"][0]["findings"][0]["rule_id"],
            "coauthored-by-trailer",
        )

    def test_n8n_advisory_scans_text_payload(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "brigade.guard.n8n_advisory"],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src")},
            input=json.dumps(
                {
                    "text": "Public post draft for release notes",
                    "name": "social-draft",
                    "policy": "public-content",
                    "source": "n8n:unit-test",
                }
            ),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["blocked"])
        self.assertEqual(payload["name"], "social-draft")
        self.assertEqual(payload["source"], "n8n:unit-test")
        self.assertIn("sanitized_text", payload)

    def test_n8n_advisory_strict_blocks_findings(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "brigade.guard.n8n_advisory", "--strict"],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src")},
            # content-guard: allow localhost-port
            input=json.dumps({"text": "Draft references localhost:5204", "policy": "public-content"}),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["blocked"])
        self.assertTrue(payload["advisory"])
        self.assertIn("[redacted-service]", payload["sanitized_text"])

    def test_n8n_advisory_rejects_missing_text(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "brigade.guard.n8n_advisory"],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src")},
            input=json.dumps({"policy": "public-content"}),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("text", payload["error"])

    def test_n8n_validation_pack_passes(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "brigade.guard.n8n_validate", "--json"],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(len(payload["fixtures"]), 5)

    def test_n8n_validation_pack_reports_expectation_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture_dir = Path(tmp)
            (fixture_dir / "mismatch.json").write_text(
                json.dumps(
                    {
                        "request": {
                            "text": "Clean publish draft",
                            "name": "mismatch",
                            "policy": "public-content",
                        },
                        "expected": {
                            "blocked": True,
                            "changed": True,
                            "finding_rule_ids": ["localhost-port"],
                        },
                    }
                )
            )

            proc = subprocess.run(
                [sys.executable, "-m", "brigade.guard.n8n_validate", str(fixture_dir), "--json"],
                cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("expected blocked", payload["fixtures"][0]["failures"][0])

    def _init_repo(self, repo: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Example User"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "user@example"], cwd=repo, check=True)
        (repo / "README.md").write_text("example\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "feat: example"], cwd=repo, check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
