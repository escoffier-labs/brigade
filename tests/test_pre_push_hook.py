"""The seeded pre-push hook must not mislabel scanner errors as leaks (issue #82)."""

from __future__ import annotations

import json
import os
import shutil
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import brigade
import pytest

_HOOK = Path(brigade.__file__).resolve().parent / "templates" / "hooks" / "pre-push"
_VENV_BIN = Path(brigade.__file__).resolve().parents[2] / ".venv" / "bin"
_ZERO40 = "0" * 40
# content-guard: allow api-key-assignment
_LEAK_LINE = 'api_key = "AKIAIOSFODNN7EXAMPLE0123456789"\n'
_CLEAN_LINE = "just a normal change\n"


def _hook_text() -> str:
    return _HOOK.read_text()


def test_pre_push_hook_captures_exit_code():
    text = _hook_text()
    assert "|| rc=$?" in text


def test_pre_push_hook_only_blocks_on_findings_exit_code():
    text = _hook_text()
    # Issue #82: the "found violations" (BLOCKED) message is gated on the
    # leak verdict (scanner exit 1) specifically, and scanner/plumbing
    # failures (exit >1) are reported as a separate "failed to run" message
    # rather than mislabeled as leaks. The hook tracks these as distinct
    # outcomes (BLOCKED vs SCANNER_ERR) across multiple scans.
    assert "BLOCKED. content-guard found violations." in text
    assert "failed to run" in text
    assert "not a leak verdict" in text


def test_pre_push_hook_reports_scanner_errors_separately():
    text = _hook_text()
    assert "failed to run" in text
    assert "not a leak verdict" in text


def test_pre_push_hook_defaults_to_embedded_brigade_scrub():
    text = _hook_text()
    assert 'brigade scrub --target "$REPO_ROOT"' in text
    assert 'SCANNER_DIR="${CONTENT_GUARD_DIR:-' not in text
    assert "clone https://github.com" not in text


def test_pre_push_hook_keeps_external_checkout_as_explicit_override():
    text = _hook_text()
    assert 'if [[ -n "${CONTENT_GUARD_DIR:-}" ]]' in text
    assert 'python3 -I -c "$SCANNER_BOOTSTRAP"' in text
    assert '"$SCANNER_INIT" "$module" "$REPO_ROOT"' in text


def test_pre_push_hook_history_uses_revs_stdin_for_new_branches():
    text = _hook_text()
    assert "--revs-stdin" in text
    assert 'git ls-remote "$REMOTE_URL"' in text
    assert "git rev-list --ignore-missing" in text


def test_pre_push_hook_is_compatible_with_macos_bash_3():
    text = _hook_text()
    assert "shopt -s lastpipe" not in text
    assert "declare -A" not in text


# ---------------------------------------------------------------------------
# Functional tests: actually run the seeded hook against temp repos with a
# bare remote, exercising the new-branch history-scan fix. These verify the
# ls-remote-derived exclusion set, batching, fail-closed behavior, and the
# preserved existing-branch / deletion / anonymous-URL paths.
# ---------------------------------------------------------------------------


def _git_env(repo: Path) -> dict:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(repo),
        "USERPROFILE": str(repo),
        "XDG_CONFIG_HOME": str(repo / ".config"),
        # The embedded CLI starts a detached update refresh unless it is opted
        # out. It inherits this test's temporary HOME, so letting it run can
        # race TemporaryDirectory cleanup by creating .cache/brigade late.
        "BRIGADE_NO_UPDATE_CHECK": "1",
    }
    # Put the editable-install brigade on PATH so the hook's `brigade` and
    # `brigade guard git` invocations resolve to this checkout.
    if _VENV_BIN.is_dir():
        env["PATH"] = f"{_VENV_BIN}:{env.get('PATH', '')}"
    return env


def test_pre_push_hook_fixture_disables_background_update_checks(tmp_path: Path) -> None:
    assert _git_env(tmp_path)["BRIGADE_NO_UPDATE_CHECK"] == "1"


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_git_env(repo),
        capture_output=True,
        text=True,
        check=check,
    )
    return proc.stdout.strip()


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _bare_remote(tmp: Path, name: str = "remote.git") -> Path:
    remote = Path(tmp) / name
    _git(Path(tmp), "init", "--bare", "-q", str(remote))
    return remote


def _seed_remote(remote: Path, repo: Path, ref: str = "refs/heads/main") -> str:
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", str(remote), f"HEAD:{ref}")
    return sha


def _run_hook(
    repo: Path,
    remote: str,
    reflines: str,
    url: str | None = None,
    extra_policy: Path | None = None,
) -> subprocess.CompletedProcess:
    env = _git_env(repo)
    env["CONTENT_GUARD_EXTRA_POLICY"] = str(extra_policy or repo / "no-private-policy.json")
    argv = ["bash", str(_HOOK), remote]
    if url is not None:
        argv.append(url)
    return subprocess.run(
        argv,
        cwd=repo,
        env=env,
        input=reflines,
        capture_output=True,
        text=True,
        check=False,
    )


class PrePushHookFunctionalTests(unittest.TestCase):
    def _init_repo(self, repo: Path) -> None:
        _git(repo, "init", "-q")
        _git(repo, "config", "user.name", "t")
        _git(repo, "config", "user.email", "t@example.com")

    def test_new_branch_excludes_already_remote_ancestors(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            remote = _bare_remote(tmp)
            self._init_repo(repo)
            _commit(repo, "base.txt", "base\n")
            leak = _commit(repo, "leak.txt", _LEAK_LINE)
            _seed_remote(remote, repo, "refs/heads/main")
            _git(repo, "checkout", "-q", "-b", "newbr", leak)
            _git(repo, "rm", "-q", "leak.txt")
            (repo / "new.txt").write_text(_CLEAN_LINE)
            _git(repo, "add", "new.txt")
            _git(repo, "commit", "-q", "-m", "drop leak, add clean")
            new = _git(repo, "rev-parse", "HEAD")
            proc = _run_hook(
                repo,
                "origin",
                f"refs/heads/newbr {new} refs/heads/newbr {_ZERO40}\n",
                url=str(remote),
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            self.assertIn("scanning introduced content in 1 new commit(s)", proc.stdout)
            self.assertNotIn(leak, proc.stdout)

    def test_new_branch_still_scans_genuinely_new_leak(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            remote = _bare_remote(tmp)
            self._init_repo(repo)
            _commit(repo, "base.txt", "base\n")
            clean = _commit(repo, "clean.txt", _CLEAN_LINE)
            _seed_remote(remote, repo, "refs/heads/main")
            _git(repo, "checkout", "-q", "-b", "newbr", clean)
            leak = _commit(repo, "newleak.txt", _LEAK_LINE)
            _git(repo, "rm", "-q", "newleak.txt")
            _git(repo, "commit", "-q", "-m", "remove new leak")
            tip = _git(repo, "rev-parse", "HEAD")
            proc = _run_hook(
                repo,
                "origin",
                f"refs/heads/newbr {tip} refs/heads/newbr {_ZERO40}\n",
                url=str(remote),
            )
            self.assertEqual(proc.returncode, 1, msg=proc.stdout + proc.stderr)
            self.assertIn("scanning introduced content in 2 new commit(s)", proc.stdout)
            self.assertIn(leak[:12], proc.stdout)

    def test_new_branch_no_remote_refs_scans_all_ancestors(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            remote = _bare_remote(tmp)
            self._init_repo(repo)
            _commit(repo, "base.txt", "base\n")
            _commit(repo, "leak.txt", _LEAK_LINE)
            _git(repo, "rm", "-q", "leak.txt")
            _git(repo, "commit", "-q", "-m", "remove leak")
            tip = _git(repo, "rev-parse", "HEAD")
            proc = _run_hook(
                repo,
                "origin",
                f"refs/heads/newbr {tip} refs/heads/newbr {_ZERO40}\n",
                url=str(remote),
            )
            self.assertEqual(proc.returncode, 1, msg=proc.stdout + proc.stderr)
            self.assertIn("scanning introduced content in 3 new commit(s)", proc.stdout)

    def test_existing_branch_range_unchanged(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            remote = _bare_remote(tmp)
            self._init_repo(repo)
            _commit(repo, "base.txt", "base\n")
            rsha = _commit(repo, "remote.txt", _CLEAN_LINE)
            _seed_remote(remote, repo, "refs/heads/main")
            leak = _commit(repo, "newleak.txt", _LEAK_LINE)
            proc = _run_hook(
                repo,
                "origin",
                f"refs/heads/main {leak} refs/heads/main {rsha}\n",
                url=str(remote),
            )
            self.assertEqual(proc.returncode, 1, msg=proc.stdout + proc.stderr)
            self.assertIn(f"{rsha}..{leak}", proc.stdout)

    def test_branch_deletion_is_skipped(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            remote = _bare_remote(tmp)
            self._init_repo(repo)
            _commit(repo, "base.txt", "base\n")
            proc = _run_hook(
                repo,
                "origin",
                f"refs/heads/main {_ZERO40} refs/heads/main {_ZERO40}\n",
                url=str(remote),
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)

    def test_stale_local_tracking_ref_does_not_under_exclude(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            remote = _bare_remote(tmp)
            self._init_repo(repo)
            _commit(repo, "base.txt", "base\n")
            leak = _commit(repo, "leak.txt", _LEAK_LINE)
            _seed_remote(remote, repo, "refs/heads/main")
            _git(repo, "checkout", "-q", "-b", "newbr", leak)
            _git(repo, "rm", "-q", "leak.txt")
            (repo / "new.txt").write_text(_CLEAN_LINE)
            _git(repo, "add", "new.txt")
            _git(repo, "commit", "-q", "-m", "drop leak, add clean")
            new = _git(repo, "rev-parse", "HEAD")
            proc = _run_hook(
                repo,
                "origin",
                f"refs/heads/newbr {new} refs/heads/newbr {_ZERO40}\n",
                url=str(remote),
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            self.assertNotIn(leak, proc.stdout)

    def test_stale_local_tracking_ref_does_not_over_exclude(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            remote = _bare_remote(tmp)
            self._init_repo(repo)
            _commit(repo, "base.txt", "base\n")
            leak = _commit(repo, "leak.txt", _LEAK_LINE)
            _seed_remote(remote, repo, "refs/heads/main")
            _git(repo, "update-ref", "refs/remotes/origin/main", leak)
            _git(remote, "update-ref", "-d", "refs/heads/main")
            _git(repo, "checkout", "-q", "-b", "newbr", leak)
            _git(repo, "rm", "-q", "leak.txt")
            tip = _commit(repo, "clean.txt", _CLEAN_LINE)
            proc = _run_hook(
                repo,
                "origin",
                f"refs/heads/newbr {tip} refs/heads/newbr {_ZERO40}\n",
                url=str(remote),
            )

            self.assertEqual(proc.returncode, 1, msg=proc.stdout + proc.stderr)
            self.assertIn("scanning introduced content in 3 new commit(s)", proc.stdout)

    def test_remote_enumeration_failure_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            tip = _commit(repo, "base.txt", "base\n")
            bad_url = str(repo / "no-such-remote.git")
            proc = _run_hook(
                repo,
                "origin",
                f"refs/heads/newbr {tip} refs/heads/newbr {_ZERO40}\n",
                url=bad_url,
            )
            self.assertEqual(proc.returncode, 1, msg=proc.stdout + proc.stderr)
            self.assertIn("failed to enumerate advertised refs", proc.stderr)
            self.assertIn("not a leak verdict", proc.stderr)
            self.assertNotIn("found violations", proc.stderr)

    def test_missing_url_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            tip = _commit(repo, "base.txt", "base\n")
            proc = _run_hook(
                repo,
                "origin",
                f"refs/heads/newbr {tip} refs/heads/newbr {_ZERO40}\n",
                url=None,
            )
            self.assertEqual(proc.returncode, 1, msg=proc.stdout + proc.stderr)
            self.assertIn("cannot enumerate remote refs", proc.stderr)
            self.assertIn("not a leak verdict", proc.stderr)
            self.assertNotIn("found violations", proc.stderr)

    def test_extra_policy_is_used_for_tip_scan(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            remote = _bare_remote(tmp)
            self._init_repo(repo)
            tip = _commit(repo, "private.txt", "company codename oriole\n")
            extra_policy = repo / "private-policy.json"
            extra_policy.write_text(
                json.dumps(
                    {
                        "name": "private-test",
                        "rules": {"private-codename": "block"},
                        "custom_rules": [
                            {
                                "id": "private-codename",
                                "category": "business",
                                "pattern": "codename oriole",
                                "replacement": "[redacted-codename]",
                            }
                        ],
                    }
                )
            )
            proc = _run_hook(
                repo,
                "origin",
                f"refs/heads/newbr {tip} refs/heads/newbr {_ZERO40}\n",
                url=str(remote),
                extra_policy=extra_policy,
            )

            self.assertEqual(proc.returncode, 1, msg=proc.stdout + proc.stderr)
            self.assertIn("private-codename", proc.stdout)

    def test_remote_only_advertised_object_is_ignored(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            other = root / "other"
            repo.mkdir()
            other.mkdir()
            remote = _bare_remote(root)
            self._init_repo(repo)
            base = _commit(repo, "base.txt", "base\n")
            _seed_remote(remote, repo, "refs/heads/main")
            _git(repo, "checkout", "-q", "-b", "newbr", base)
            tip = _commit(repo, "new.txt", _CLEAN_LINE)
            self._init_repo(other)
            _commit(other, "other.txt", "collaborator\n")
            _seed_remote(remote, other, "refs/heads/collaborator")
            proc = _run_hook(
                repo,
                "origin",
                f"refs/heads/newbr {tip} refs/heads/newbr {_ZERO40}\n",
                url=str(remote),
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            self.assertIn("scanning introduced content in 1 new commit(s)", proc.stdout)

    def test_annotated_tag_excludes_peeled_commit_when_tag_object_is_not_local(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            repo = root / "repo"
            seed.mkdir()
            remote = _bare_remote(root)
            self._init_repo(seed)
            _commit(seed, "base.txt", "base\n")
            _commit(seed, "leak.txt", _LEAK_LINE)
            _git(seed, "push", "-q", str(remote), "HEAD:refs/heads/main")
            _git(root, "clone", "-q", "--no-tags", "--branch", "main", str(remote), str(repo))
            _git(seed, "tag", "-a", "public", "-m", "public tag")
            tag_sha = _git(seed, "rev-parse", "refs/tags/public")
            _git(seed, "push", "-q", str(remote), "refs/tags/public")
            self.assertNotEqual(
                subprocess.run(
                    ["git", "cat-file", "-e", tag_sha],
                    cwd=repo,
                    env=_git_env(repo),
                    capture_output=True,
                    text=True,
                    check=False,
                ).returncode,
                0,
            )
            _git(seed, "push", "-q", str(remote), ":refs/heads/main")
            _git(repo, "config", "user.name", "t")
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "rm", "-q", "leak.txt")
            tip = _commit(repo, "clean.txt", _CLEAN_LINE)
            proc = _run_hook(
                repo,
                "origin",
                f"refs/heads/newbr {tip} refs/heads/newbr {_ZERO40}\n",
                url=str(remote),
            )

            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            self.assertIn("scanning introduced content in 1 new commit(s)", proc.stdout)

    def test_new_branch_batches_into_one_history_scan(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            remote = _bare_remote(tmp)
            self._init_repo(repo)
            _commit(repo, "base.txt", "base\n")
            clean = _commit(repo, "remote.txt", _CLEAN_LINE)
            _seed_remote(remote, repo, "refs/heads/main")
            _git(repo, "checkout", "-q", "-b", "newbr", clean)
            for i in range(3):
                _commit(repo, f"new{i}.txt", _CLEAN_LINE)
            tip = _git(repo, "rev-parse", "HEAD")
            proc = _run_hook(
                repo,
                "origin",
                f"refs/heads/newbr {tip} refs/heads/newbr {_ZERO40}\n",
                url=str(remote),
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout.count("scanning introduced content in"), 1, msg=proc.stdout)
            self.assertIn("scanning introduced content in 3 new commit(s)", proc.stdout)

    def test_anonymous_url_push_uses_advertised_refs(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            remote = _bare_remote(tmp)
            self._init_repo(repo)
            _commit(repo, "base.txt", "base\n")
            leak = _commit(repo, "leak.txt", _LEAK_LINE)
            _seed_remote(remote, repo, "refs/heads/main")
            _git(repo, "checkout", "-q", "-b", "newbr", leak)
            _git(repo, "rm", "-q", "leak.txt")
            (repo / "new.txt").write_text(_CLEAN_LINE)
            _git(repo, "add", "new.txt")
            _git(repo, "commit", "-q", "-m", "drop leak, add clean")
            new = _git(repo, "rev-parse", "HEAD")
            proc = _run_hook(
                repo,
                "",
                f"refs/heads/newbr {new} refs/heads/newbr {_ZERO40}\n",
                url=str(remote),
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            self.assertNotIn(leak, proc.stdout)

    def test_scanner_error_categorized_not_as_leak(self) -> None:
        # A scanner/plumbing failure (exit >1) must surface as "failed to run",
        # not as "found violations" (issue #82).
        text = _hook_text()
        assert "failed to run" in text
        assert "not a leak verdict" in text
        assert "BLOCKED. content-guard found violations." in text


if __name__ == "__main__":
    unittest.main()


@pytest.fixture(scope="module")
def external_hook_shell():
    if os.name != "nt":
        return "bash"
    # Windows' default bash.exe can be the WSL bridge. These native hook
    # tests require the executable shipped with the verified Git installation.
    git = shutil.which("git")
    assert git, "native hook verification requires installed Git for Windows"
    git_path = Path(git).resolve()
    version = subprocess.check_output([str(git_path), "--version"], text=True, timeout=15)
    assert ".windows." in version.lower(), version
    candidates = [root / "bin/bash.exe" for root in git_path.parents if (root / "cmd/git.exe").is_file()]
    assert candidates, "Git for Windows installation does not contain bin/bash.exe"
    bash = candidates[0]
    assert bash.is_file() and os.access(bash, os.X_OK), f"Git Bash is not executable: {bash}"
    kernel = subprocess.check_output([str(bash), "-c", "uname -s"], text=True, timeout=15).strip()
    assert kernel.startswith(("MINGW", "MSYS")), f"expected native Git Bash, got {kernel}"
    return str(bash)


@pytest.mark.parametrize("mode", ["tip", "range", "revs-stdin"])
@pytest.mark.parametrize("shadow", ["module", "package", "pythonpath"])
def test_external_hook_runs_configured_scanner(tmp_path, mode, shadow, external_hook_shell):
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    before = _commit(repo, "first.txt", "clean\n")
    after = _commit(repo, "second.txt", "clean\n")
    configured = tmp_path / "scanner checkout"
    package = configured / "src/content_guard"
    package.mkdir(parents=True)
    policy = configured / "policies/public-repo.json"
    policy.parent.mkdir()
    policy.write_text("{}")
    probe = tmp_path / "trusted-scans.jsonl"
    (package / "__init__.py").write_text("")
    source = (
        "import json,os,sys\nfrom pathlib import Path\n"
        f"with Path({str(probe)!r}).open('a') as f: f.write(json.dumps({{'argv':sys.argv[1:], 'cwd':os.getcwd(), 'file':__file__, 'stdin':sys.stdin.read() if '--revs-stdin' in sys.argv else ''}})+'\\n')\n"
        f"raise SystemExit(1 if {'True' if mode == 'tip' else 'False'} or '--history' in sys.argv else 0)\n"
    )
    for name in ("__main__.py", "git_scan.py"):
        (package / name).write_text(source)
    marker = tmp_path / "untrusted-executed"
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    root = hostile if shadow == "pythonpath" else repo
    planted = root / ("content_guard/__init__.py" if shadow == "package" else "content_guard.py")
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise SystemExit(0)\n")
    env = {
        **_git_env(repo),
        "CONTENT_GUARD_DIR": configured.as_posix(),
        "CONTENT_GUARD_POLICY": policy.as_posix(),
        "PYTHONPATH": hostile.as_posix(),
        "CONTENT_GUARD_EXTRA_POLICY": (tmp_path / "absent-policy.json").as_posix(),
    }
    if os.name == "nt":
        # This host may expose only python.exe, while the production hook
        # requires python3. Supply that name only in this child environment,
        # forwarding to the already running native interpreter without a shell
        # lookup of Python or any global installation/alias changes.
        tools = tmp_path / "fixture tools"
        tools.mkdir()
        forwarder = tools / "python3"
        forwarder.write_text(f'#!/bin/sh\nexec {shlex.quote(Path(sys.executable).as_posix())} "$@"\n')
        forwarder.chmod(0o755)
        env["PATH"] = str(tools) + os.pathsep + env.get("PATH", "")
        identity_code = "import json,sys;print(json.dumps([sys.executable,sys.version]))"
        identity = subprocess.check_output(
            [external_hook_shell, "-c", f"exec python3 -I -c {shlex.quote(identity_code)}"],
            env=env,
            cwd=tmp_path,
            text=True,
            timeout=15,
        )
        executable, version = json.loads(identity)
        assert Path(executable).resolve().samefile(sys.executable)
        assert version == sys.version
    if mode == "tip":
        refs = ""
        remote = (tmp_path / "unused.git").as_posix()
    elif mode == "range":
        refs = f"refs/heads/main {after} refs/heads/main {before}\n"
        remote = (tmp_path / "unused.git").as_posix()
    else:
        remote_path = tmp_path / "remote.git"
        _git(tmp_path, "init", "--bare", "-q", str(remote_path))
        _git(repo, "push", "-q", str(remote_path), f"{before}:refs/heads/main")
        refs = f"refs/heads/new {after} refs/heads/new {'0' * 40}\n"
        remote = remote_path.as_posix()
    result = subprocess.run(
        [external_hook_shell, _HOOK.as_posix(), "origin", remote],
        cwd=repo,
        env=env,
        input=refs,
        capture_output=True,
        text=True,
    )
    assert not marker.exists(), "hook executed a scanner shadow"
    assert result.returncode == 1, result.stdout + result.stderr
    assert "BLOCKED. content-guard found violations." in result.stderr
    rows = [json.loads(line) for line in probe.read_text().splitlines()]
    assert len(rows) == (1 if mode == "tip" else 2)
    assert all(Path(row["file"]).resolve().parent == package.resolve() for row in rows)
    for row in rows:
        assert row["argv"].count("--policy") == 1
        assert row["argv"][-2] == "--policy"
        assert Path(row["argv"][-1]).resolve().samefile(policy)
    assert rows[0]["argv"][0] == "scan"
    assert len(rows[0]["argv"]) == 4
    assert Path(rows[0]["argv"][1]).resolve().samefile(repo)
    if mode == "tip":
        assert "--history" not in rows[0]["argv"]
    else:
        history = next(row for row in rows if "--history" in row["argv"])
        assert Path(history["cwd"]).resolve().samefile(repo)
        if mode == "range":
            assert history["argv"][:3] == ["--history", "--range", f"{before}..{after}"]
            assert len(history["argv"]) == 5
        else:
            assert history["argv"][:2] == ["--history", "--revs-stdin"]
            assert len(history["argv"]) == 4
            assert history["stdin"].strip() == after
