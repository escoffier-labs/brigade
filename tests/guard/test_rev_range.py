from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from brigade.guard.git_commits import _commit_revs
from brigade.guard.git_scan import _history_revs
from brigade.guard.rev_range import validate_rev_range_operand


ROOT = Path(__file__).resolve().parents[2]


class RevRangeValidationTests(unittest.TestCase):
    def test_rejects_option_injection_operands(self) -> None:
        for operand in (
            "--max-count=0",
            "--all",
            " --max-count=0",
            "--max-count=0 ",
            "-HEAD",
            "",
            "   ",
            "origin/main..",
            "..HEAD",
            "a....b",
            "a.....b",
            "a...b..c",
            "a..b...c",
            "HEAD;id",
            "HEAD|id",
            "HEAD\n--all",
        ):
            with self.subTest(operand=operand):
                with self.assertRaises(SystemExit) as ctx:
                    validate_rev_range_operand(operand)
                self.assertEqual(ctx.exception.code, 2)

    def test_accepts_common_revision_operands(self) -> None:
        for operand in (
            "HEAD",
            "origin/main..HEAD",
            "HEAD~1..HEAD",
            "refs/tags/v1.2.3",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "feature/foo_bar",
            "main...HEAD",
            "^HEAD",
            "HEAD^!",
            "@{upstream}",
            "v1.0.0+build.1",
        ):
            with self.subTest(operand=operand):
                validate_rev_range_operand(operand)

    def test_history_revs_rejects_injection_before_rev_list(self) -> None:
        import argparse

        args = argparse.Namespace(rev_range="--max-count=0", all=False, revs_stdin=False)
        with mock.patch("brigade.guard.git_scan.subprocess.run") as run:
            with self.assertRaises(SystemExit) as ctx:
                _history_revs(args)
            run.assert_not_called()
        self.assertEqual(ctx.exception.code, 2)

    def test_history_revs_rejects_explicit_empty_range_before_rev_list(self) -> None:
        import argparse

        for operand in ("", "   "):
            with self.subTest(operand=operand):
                args = argparse.Namespace(rev_range=operand, all=False, revs_stdin=False)
                with mock.patch("brigade.guard.git_scan.subprocess.run") as run:
                    with self.assertRaises(SystemExit) as ctx:
                        _history_revs(args)
                    run.assert_not_called()
                self.assertEqual(ctx.exception.code, 2)

    def test_commit_revs_rejects_injection_before_rev_list(self) -> None:
        import argparse

        args = argparse.Namespace(rev_range="--max-count=0", all=False)
        with mock.patch("brigade.guard.git_commits.subprocess.run") as run:
            with self.assertRaises(SystemExit) as ctx:
                _commit_revs(args)
            run.assert_not_called()
        self.assertEqual(ctx.exception.code, 2)

    def test_commit_revs_rejects_explicit_empty_range_before_rev_list(self) -> None:
        import argparse

        for operand in ("", "   "):
            with self.subTest(operand=operand):
                args = argparse.Namespace(rev_range=operand, all=False)
                with (
                    mock.patch("brigade.guard.git_commits._has_head") as has_head,
                    mock.patch("brigade.guard.git_commits._git") as git,
                ):
                    with self.assertRaises(SystemExit) as ctx:
                        _commit_revs(args)
                    has_head.assert_not_called()
                    git.assert_not_called()
                self.assertEqual(ctx.exception.code, 2)

    def test_git_scan_history_rejects_injected_range_cli(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_scan",
                    "--history",
                    "--range=--max-count=0",
                    "--json",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid revision range operand", proc.stderr)
        self.assertNotIn("Clean", proc.stdout)

    def test_git_scan_history_rejects_explicit_empty_range_cli(self) -> None:
        for operand in ("", "   "):
            with self.subTest(operand=operand), TemporaryDirectory() as tmp:
                repo = Path(tmp)
                self._init_repo(repo)
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "brigade.guard.git_scan",
                        "--history",
                        f"--range={operand}",
                        "--json",
                    ],
                    cwd=repo,
                    env={"PYTHONPATH": str(ROOT / "src")},
                    capture_output=True,
                    text=True,
                    check=False,
                )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("invalid revision range operand", proc.stderr)
            self.assertEqual(proc.stdout, "")

    def test_git_commits_rejects_injected_range_cli(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_commits",
                    "--range=--max-count=0",
                    "--json",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid revision range operand", proc.stderr)
        self.assertNotIn("Clean", proc.stdout)

    def test_publish_check_rejects_injected_commit_range_cli(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.publish_check",
                    "--commit-range=--max-count=0",
                    "--json",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid revision range operand", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_publish_check_rejects_explicit_empty_commit_range_cli(self) -> None:
        for operand in ("", "   "):
            with self.subTest(operand=operand), TemporaryDirectory() as tmp:
                repo = Path(tmp)
                self._init_repo(repo)
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "brigade.guard.publish_check",
                        f"--commit-range={operand}",
                        "--json",
                    ],
                    cwd=repo,
                    env={"PYTHONPATH": str(ROOT / "src")},
                    capture_output=True,
                    text=True,
                    check=False,
                )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("invalid revision range operand", proc.stderr)
            self.assertEqual(proc.stdout, "")

    def test_git_scan_history_accepts_valid_range_cli(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_scan",
                    "--history",
                    "--range",
                    "HEAD",
                    "--json",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        payload = __import__("json").loads(proc.stdout)
        self.assertGreaterEqual(payload["commits_scanned"], 1)

    def test_git_scan_history_handles_repository_without_head(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_scan",
                    "--history",
                    "--json",
                ],
                cwd=repo,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        payload = __import__("json").loads(proc.stdout)
        self.assertEqual(payload["commits_scanned"], 0)
        self.assertEqual(payload["commits_with_findings"], 0)

    def _init_repo(self, repo: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Example User"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "user@example"], cwd=repo, check=True)
        (repo / "README.md").write_text("example\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "feat: example"], cwd=repo, check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
