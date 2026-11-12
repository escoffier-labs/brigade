from __future__ import annotations

import json
import os
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

    def test_git_commit_scan_blocks_approved_agent_coauthor_trailer_by_default(self) -> None:
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
                    # content-guard: allow email
                    "Co-authored-by: Codex <codex@openai.com>",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

            proc = subprocess.run(
                [sys.executable, "-m", "brigade.guard.git_commits", "--range", "HEAD", "--json"],
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

    def test_git_scan_revs_stdin_scans_exact_set(self) -> None:
        # --revs-stdin with --history scans exactly the revisions fed on
        # stdin, bypassing `git rev-list`. Lets the pre-push hook batch a
        # precomputed new-commit set into one process without putting
        # revisions on argv (no ARG_MAX, no option injection).
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            (repo / "clean.txt").write_text("clean\n")
            subprocess.run(["git", "add", "clean.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "feat: clean"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            # content-guard: allow api-key-assignment
            (repo / "leak.txt").write_text('api_key = "AKIAIOSFODNN7EXAMPLE0123456789"\n')
            subprocess.run(["git", "add", "leak.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "feat: leak"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            leak_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
            ).stdout.strip()
            clean_sha = subprocess.run(
                ["git", "rev-parse", "HEAD~1"], cwd=repo, capture_output=True, text=True, check=True
            ).stdout.strip()

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_scan",
                    "--history",
                    "--revs-stdin",
                    "--policy",
                    str(ROOT / "src" / "brigade" / "guard" / "policies" / "public-repo.json"),
                    "--json",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                input=f"{leak_sha}\n{clean_sha}\n",
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 1, msg=proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["blocked"])
        self.assertEqual(payload["commits_scanned"], 2)
        self.assertEqual(payload["commits_with_findings"], 1)
        self.assertEqual(payload["commits"][0]["commit"], leak_sha)

    def test_git_scan_revs_stdin_empty_input_scans_nothing(self) -> None:
        # Empty stdin -> empty revision set -> clean, zero commits scanned.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_scan",
                    "--history",
                    "--revs-stdin",
                    "--policy",
                    str(ROOT / "src" / "brigade" / "guard" / "policies" / "public-repo.json"),
                    "--json",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                input="",
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["blocked"])
        self.assertEqual(payload["commits_scanned"], 0)

    def test_git_scan_revs_stdin_rejects_non_sha_input(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_scan",
                    "--history",
                    "--revs-stdin",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                input="--help\n",
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 2, msg=proc.stdout + proc.stderr)
        self.assertIn("expected a full 40- or 64-character hex SHA", proc.stderr)

    def test_git_scan_revs_stdin_rejects_unknown_sha(self) -> None:
        # A well-formed SHA that does not resolve locally must fail closed
        # (exit 2) rather than be silently skipped as clean.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_scan",
                    "--history",
                    "--revs-stdin",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                input=f"{'0' * 40}\n",
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 2, msg=proc.stdout + proc.stderr)
        self.assertIn("cannot read commit", proc.stderr)

    def test_git_scan_revs_stdin_requires_history(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "brigade.guard.git_scan", "--revs-stdin"],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src")},
            input="",
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(proc.returncode, 2, msg=proc.stdout + proc.stderr)
        self.assertIn("--revs-stdin requires --history", proc.stderr)

    def test_git_scan_revs_stdin_accepts_sha256(self) -> None:
        # SHA-256 repos emit 64-character SHAs; --revs-stdin must accept them.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(
                ["git", "init", "--object-format=sha256"], cwd=repo, check=True, capture_output=True, text=True
            )
            subprocess.run(["git", "config", "user.name", "Example User"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "user@example"], cwd=repo, check=True)
            (repo / "README.md").write_text("example\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "feat: example"], cwd=repo, check=True, capture_output=True, text=True
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(len(sha), 64)

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_scan",
                    "--history",
                    "--revs-stdin",
                    "--policy",
                    str(ROOT / "src" / "brigade" / "guard" / "policies" / "public-repo.json"),
                    "--json",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                input=f"{sha}\n",
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["commits_scanned"], 1)

    def test_git_scan_accepts_packaged_policy_name(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_scan",
                    "--all-tracked",
                    "--policy",
                    "public-repo",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)

    def _init_repo(self, repo: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Example User"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "user@example"], cwd=repo, check=True)
        (repo / "README.md").write_text("example\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "feat: example"], cwd=repo, check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()


def test_r1_staged_newline_filename_is_scanned(tmp_path, monkeypatch):
    from brigade.guard import git_scan

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    path = tmp_path / ("line\nbreak.txt" if os.name != "nt" else "portable.txt")
    path.write_text("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n")  # content-guard: allow secret/aws-access-key
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    if os.name == "nt":
        # Win32 cannot create control-character filenames. Exercise Git's raw path
        # protocol and the scanner with the same bytes on that platform.
        monkeypatch.setattr(
            git_scan.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0], 0, (b"100644 " + b"a" * 40 + b" 0\t" if "--stage" in a[0] else b"") + b"line\nbreak.txt\0", b""
            ),
        )
        monkeypatch.setattr(git_scan, "_read_text", lambda _: path.read_text())
    monkeypatch.chdir(tmp_path)
    assert git_scan.main(["--staged"]) == 1


@__import__("pytest").mark.parametrize("executable", [False, True])
def test_r1_staged_unreadable_fails_closed(tmp_path, monkeypatch, executable):
    from brigade.guard import git_scan

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    path = tmp_path / "missing.txt"
    path.write_text("content\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    if executable:
        subprocess.run(["git", "-C", str(tmp_path), "update-index", "--chmod=+x", "missing.txt"], check=True)
    path.unlink()
    monkeypatch.chdir(tmp_path)
    with __import__("pytest").raises(SystemExit) as error:
        git_scan.main(["--staged"])
    assert error.value.code == 2


def test_r1_staged_undecodable_path_fails_closed(tmp_path, monkeypatch):
    from brigade.guard import git_scan

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    if os.name == "nt":
        monkeypatch.setattr(
            git_scan.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, b"bad-\xff.txt\0", b"")
        )
    else:
        with open(os.fsencode(tmp_path) + b"/bad-\xff.txt", "wb") as stream:
            stream.write(b"content\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    monkeypatch.chdir(tmp_path)
    with __import__("pytest").raises(SystemExit) as error:
        git_scan.main(["--staged"])
    assert error.value.code == 2


def test_r1_history_disables_textconv(tmp_path, monkeypatch):
    from brigade.guard import git_scan

    def git(*args):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )

    git("init", "-b", "main")
    (tmp_path / ".gitattributes").write_text("*.txt diff=example\n")
    (tmp_path / "file.txt").write_text("ordinary content\n")
    git("add", ".")
    git("-c", "user.name=Example", "-c", "user.email=dev@example.invalid", "commit", "-m", "test: example")
    git("config", "diff.example.textconv", "")
    monkeypatch.chdir(tmp_path)
    assert git_scan.main(["--history", "--all"]) == 0


@__import__("pytest").mark.parametrize("initialized", [False, True])
@__import__("pytest").mark.parametrize("mode", ["--staged", "--all-tracked", "publish-check"])
def test_r2_gitlinks_are_not_file_content(tmp_path, monkeypatch, initialized, mode):
    from brigade.guard import git_scan, publish_check

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-b", "main")
    (tmp_path / "README.md").write_text("Example project\n")
    git("add", "README.md")
    git("-c", "user.name=Example", "-c", "user.email=dev@example.invalid", "commit", "-m", "test: initial")
    sha = git("rev-parse", "HEAD")
    git("update-index", "--add", "--cacheinfo", f"160000,{sha},module")
    if initialized:
        module = tmp_path / "module"
        module.mkdir()
        subprocess.check_call(["git", "init", str(module)], stdout=subprocess.DEVNULL)
        (module / "README.md").write_text("Example submodule\n")
    monkeypatch.chdir(tmp_path)
    if mode == "publish-check":
        assert publish_check.main(["--all-tracked"]) == 0
    else:
        assert git_scan.main([mode]) == 0


def _symlink_scan_repo(tmp_path, kind, target_text=None):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args, stdin=None):
        return subprocess.check_output(["git", *args], cwd=repo, input=stdin).decode().strip()

    git("init", "-b", "main")
    (repo / "README.md").write_text("Example project\n")
    git("add", "README.md")
    git("-c", "user.name=Example", "-c", "user.email=dev@example.invalid", "commit", "-m", "test: initial")
    sample = "AKIAIOSFODNN7EXAMPLE"  # content-guard: allow bare-token-prefix
    if kind == "directory":
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "payload.txt").write_text(sample)
        target = "../assets"
    elif kind == "dangling":
        target = "../missing"
    else:
        (tmp_path / "target.txt").write_text(sample)
        target = "../target.txt"
    target = target_text or target
    oid = git("hash-object", "-w", "--stdin", stdin=target.encode())
    git("update-index", "--add", "--cacheinfo", f"120000,{oid},linked-assets")
    link = repo / "linked-assets"
    if os.name == "nt":
        # Git's core.symlinks=false checkout stores link text as a regular file.
        link.write_text(target)
    else:
        link.symlink_to(target, target_is_directory=kind == "directory")
    if kind != "staged":
        git("-c", "user.name=Example", "-c", "user.email=dev@example.invalid", "commit", "-m", "test: link")
    return repo


@__import__("pytest").mark.parametrize("kind", ["directory", "dangling", "staged"])
@__import__("pytest").mark.parametrize("scanner", ["git", "publish", "audit", "baseline"])
def test_r2_indexed_symlinks_never_follow_targets(tmp_path, monkeypatch, kind, scanner):
    from brigade.guard import git_scan, publish_check
    from brigade.guard.audit import run_audit
    from brigade.guard.baseline import init_baseline
    from brigade.guard.policy import default_policy, load_policy

    repo = _symlink_scan_repo(tmp_path, kind)
    policy = load_policy(default_policy("public-repo.json"))
    monkeypatch.chdir(repo if scanner in {"git", "publish"} else tmp_path)
    if scanner == "git":
        assert git_scan.main(["--staged" if kind == "staged" else "--all-tracked"]) == 0
    elif scanner == "publish":
        assert publish_check.main(["--all-tracked"]) == 0
    elif scanner == "audit":
        assert not run_audit(repo, policy=policy).blocked
    else:
        assert not init_baseline(repo, policy=policy, scope="tracked").entries


@__import__("pytest").mark.parametrize("scanner", ["git", "publish", "audit", "baseline"])
def test_r2_symlink_target_text_is_scanned(tmp_path, monkeypatch, scanner):
    from brigade.guard import git_scan, publish_check
    from brigade.guard.audit import run_audit
    from brigade.guard.baseline import init_baseline
    from brigade.guard.policy import default_policy, load_policy

    sample = "AKIAIOSFODNN7EXAMPLE"  # content-guard: allow bare-token-prefix
    repo = _symlink_scan_repo(tmp_path, "staged", target_text=sample)
    policy = load_policy(default_policy("public-repo.json"))
    monkeypatch.chdir(repo if scanner in {"git", "publish"} else tmp_path)
    if scanner == "git":
        assert git_scan.main(["--staged"]) == 1
    elif scanner == "publish":
        assert publish_check.main(["--all-tracked"]) == 1
    elif scanner == "audit":
        assert run_audit(repo, policy=policy).blocked
    else:
        assert any(
            entry.path == "linked-assets" for entry in init_baseline(repo, policy=policy, scope="tracked").entries
        )


def test_r2_unknown_index_mode_fails_closed(tmp_path, monkeypatch, capsys):
    from brigade.guard import git_scan

    repo = _symlink_scan_repo(tmp_path, "directory")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        git_scan.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"040000 " + b"a" * 40 + b" 0\tREADME.md\0", b""),
    )
    with __import__("pytest").raises(SystemExit) as error:
        git_scan.main(["--all-tracked"])
    assert error.value.code == 2
    assert "unsupported-index-mode" in capsys.readouterr().err


def test_r2_audit_does_not_skip_symlink_to_baseline(tmp_path):
    from brigade.guard.audit import run_audit
    from brigade.guard.baseline import Baseline, save_baseline
    from brigade.guard.policy import default_policy, load_policy

    sample = "AKIAIOSFODNN7EXAMPLE"  # content-guard: allow bare-token-prefix
    repo = _symlink_scan_repo(tmp_path, "staged", target_text=sample)
    baseline = Baseline(entries=[], created_at="2026-01-01T00:00:00+00:00")
    path = repo / sample
    save_baseline(baseline, path)
    report = run_audit(
        repo, policy=load_policy(default_policy("public-repo.json")), baseline=baseline, baseline_path=path
    )
    assert report.blocked


def test_r2_symlink_reads_original_index_blob(tmp_path, monkeypatch):
    from brigade.guard import git_scan

    sample = "AKIAIOSFODNN7EXAMPLE"  # content-guard: allow bare-token-prefix
    repo = _symlink_scan_repo(tmp_path, "staged", target_text=sample)
    original = subprocess.check_output(["git", "rev-parse", ":linked-assets"], cwd=repo).decode().strip()
    replacement = (
        subprocess.check_output(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=b"../safe").decode().strip()
    )
    subprocess.check_call(["git", "replace", original, replacement], cwd=repo)
    monkeypatch.chdir(repo)
    assert git_scan.main(["--staged"]) == 1


def test_r2_staged_symlink_from_subdirectory(tmp_path, monkeypatch):
    from brigade.guard import git_scan

    repo = _symlink_scan_repo(tmp_path, "staged")
    nested = repo / "nested"
    nested.mkdir()
    sample = "AKIAIOSFODNN7EXAMPLE"  # content-guard: allow bare-token-prefix
    oid = (
        subprocess.check_output(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=sample.encode())
        .decode()
        .strip()
    )
    subprocess.check_call(["git", "update-index", "--add", "--cacheinfo", f"120000,{oid},nested/nested-link"], cwd=repo)
    monkeypatch.chdir(nested)
    assert git_scan.main(["--staged"]) == 1
