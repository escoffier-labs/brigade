from __future__ import annotations

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
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
            "--max-count=0 ",
            "-HEAD",
            "",
            "   ",
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
            "origin/main..",
            "..HEAD",
            "HEAD~1..HEAD",
            "refs/tags/v1.2.3",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "feature/foo_bar",
            "main...HEAD",
            "main...",
            "...HEAD",
            "^HEAD",
            "HEAD^!",
            "@{upstream}",
            "v1.0.0+build.1",
            "café",
            "feature#1",
            "release(v1)",
            "foo=bar",
            ":/search text",
        ):
            with self.subTest(operand=operand):
                validate_rev_range_operand(operand)

    def test_rejection_uses_a_fixed_bounded_error(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            validate_rev_range_operand("-" + "x" * 10_000)
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(stderr.getvalue(), "invalid revision range operand\n")

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
                    mock.patch("brigade.guard.git_commits.git_has_head") as has_head,
                    mock.patch("brigade.guard.git_commits._git") as git,
                ):
                    with self.assertRaises(SystemExit) as ctx:
                        _commit_revs(args)
                    has_head.assert_not_called()
                    git.assert_not_called()
                self.assertEqual(ctx.exception.code, 2)

    def test_commit_revs_validates_range_even_when_all_is_set(self) -> None:
        import argparse

        args = argparse.Namespace(rev_range="--max-count=0", all=True)
        with mock.patch("brigade.guard.git_commits._git") as git:
            with self.assertRaises(SystemExit) as ctx:
                _commit_revs(args)
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

    def test_git_commits_rejects_all_with_range_cli(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "brigade.guard.git_commits",
                "--all",
                "--range=HEAD",
                "--json",
            ],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not allowed with argument", proc.stderr)
        self.assertEqual(proc.stdout, "")

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

    def test_publish_check_rejects_all_commits_with_commit_range_cli(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "brigade.guard.publish_check",
                "--all-commits",
                "--commit-range=HEAD",
                "--json",
            ],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not allowed with argument", proc.stderr)
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

    def test_history_clis_accept_range_with_omitted_endpoint(self) -> None:
        invocations = (
            ("brigade.guard.git_scan", "--history", "--range=HEAD.."),
            ("brigade.guard.git_commits", "--range=HEAD.."),
            ("brigade.guard.publish_check", "--commit-range=HEAD.."),
        )
        for invocation in invocations:
            with self.subTest(module=invocation[0]), TemporaryDirectory() as tmp:
                repo = Path(tmp)
                self._init_repo(repo)
                proc = subprocess.run(
                    [sys.executable, "-m", *invocation, "--json"],
                    cwd=repo,
                    env={"PYTHONPATH": str(ROOT / "src")},
                    capture_output=True,
                    text=True,
                    check=False,
                )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)

    def test_history_clis_accept_valid_punctuation_and_unicode_refs(self) -> None:
        invocations = (
            ("brigade.guard.git_scan", "--history", "--range="),
            ("brigade.guard.git_commits", "--range="),
            ("brigade.guard.publish_check", "--commit-range="),
        )
        for ref_name in ("café", "feature#1", "release(v1)", "foo=bar"):
            for module, *prefix in invocations:
                with self.subTest(module=module, ref_name=ref_name), TemporaryDirectory() as tmp:
                    repo = Path(tmp)
                    self._init_repo(repo)
                    subprocess.run(["git", "branch", ref_name], cwd=repo, check=True)
                    range_arg = f"{prefix[-1]}{ref_name}"
                    proc = subprocess.run(
                        [sys.executable, "-m", module, *prefix[:-1], range_arg, "--json"],
                        cwd=repo,
                        env={"PYTHONPATH": str(ROOT / "src")},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)

    def test_history_clis_bound_git_errors_for_user_ranges(self) -> None:
        invocations = (
            ("brigade.guard.git_scan", "--history", "--range="),
            ("brigade.guard.git_commits", "--range="),
            ("brigade.guard.publish_check", "--commit-range="),
        )
        for operand in (".", "./README.md", "a....b", "A" * 10_000):
            for module, *prefix in invocations:
                with self.subTest(module=module, operand=operand[:20]), TemporaryDirectory() as tmp:
                    repo = Path(tmp)
                    self._init_repo(repo)
                    range_arg = f"{prefix[-1]}{operand}"
                    proc = subprocess.run(
                        [sys.executable, "-m", module, *prefix[:-1], range_arg, "--json"],
                        cwd=repo,
                        env={"PYTHONPATH": str(ROOT / "src")},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stderr, "invalid revision range operand\n")
                self.assertEqual(proc.stdout, "")

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

    def test_git_scan_history_fails_outside_repository(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard.git_scan",
                    "--history",
                    "--json",
                ],
                cwd=tmp,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a git repository", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_git_commits_fails_outside_repository(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, "-m", "brigade.guard.git_commits", "--json"],
                cwd=tmp,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a git repository", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def _init_repo(self, repo: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Example User"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "user@example"], cwd=repo, check=True)
        (repo / "README.md").write_text("example\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "feat: example"], cwd=repo, check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
