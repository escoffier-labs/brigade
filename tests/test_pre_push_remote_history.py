"""Destination reachability excludes public history, never unpublished leaks."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_pre_push_hook import (
    _CLEAN_LINE,
    _HOOK,
    _LEAK_LINE,
    _ZERO40,
    _bare_remote,
    _commit,
    _git,
    _git_env,
    _hook_shell,
    _run_hook,
    _seed_remote,
    external_hook_shell as external_hook_shell,
)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    base = _commit(repo, "base.txt", _CLEAN_LINE)
    remote = _bare_remote(tmp_path)
    _seed_remote(remote, repo)
    return repo, remote, base


def _run_with_git_shim(repo, remote, base, tip, tools, shim, enumerations):
    # Git Bash may prepend its bundled Git while starting. Set PATH inside
    # that verified shell and source the hook without another shell startup.
    marker = tools / "git-invocations"
    shim.write_text(
        shim.read_text().replace(
            "#!/bin/sh\n", f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> {shlex.quote(marker.as_posix())}\n", 1
        ),
        newline="\n",
    )
    shim.chmod(0o755)
    env = _git_env(repo)
    env["CONTENT_GUARD_EXTRA_POLICY"] = str(tools / "absent.json")
    # Even a broken fixture must never contact an external destination.
    env["GIT_ALLOW_PROTOCOL"] = "file"
    script = (
        "tools=$1; hook=$2; shift 2\n"
        'if command -v cygpath >/dev/null 2>&1; then tools=$(cygpath -u "$tools"); fi\n'
        'export PATH="$tools:$PATH"\n'
        "hash -r\n"
        '[ "$(command -v git)" = "$tools/git" ] || exit 97\n'
        '. "$hook" "$@"\n'
    )
    result = subprocess.run(
        [_hook_shell(), "-c", script, "hook-fixture", tools.as_posix(), _HOOK.as_posix(), "origin", remote],
        cwd=repo,
        env=env,
        input=f"refs/heads/main {tip} refs/heads/main {base}\n",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert marker.is_file(), result.stdout + result.stderr
    assert marker.read_text().splitlines().count("ls-remote") == enumerations, result.stdout + result.stderr
    return result


@pytest.mark.parametrize("unpublished_leak", [False, True])
def test_existing_branch_merging_published_history_still_scans_unpublished(tmp_path, unpublished_leak):
    repo, remote, base = _repo(tmp_path)
    _git(repo, "branch", "feature", base)
    _git(repo, "push", "-q", str(remote), "feature")
    _commit(repo, "public.txt", _LEAK_LINE)
    _git(repo, "rm", "public.txt")
    _commit(repo, "main.txt", _CLEAN_LINE)
    _seed_remote(remote, repo)
    _git(repo, "checkout", "-q", "feature")
    _commit(repo, "private.txt", _LEAK_LINE if unpublished_leak else _CLEAN_LINE)
    _git(repo, "rm", "private.txt")
    _commit(repo, "feature.txt", _CLEAN_LINE)
    _git(repo, "merge", "--no-ff", "main", "-m", "integrate published main")
    tip = _git(repo, "rev-parse", "HEAD")
    result = _run_hook(repo, "origin", f"refs/heads/feature {tip} refs/heads/feature {base}\n", url=str(remote))
    assert result.returncode == int(unpublished_leak), result.stdout + result.stderr
    assert "scanning introduced content in 3 new commit(s)" in result.stdout
    assert ("BLOCKED. content-guard found violations." in result.stderr) == unpublished_leak


@pytest.mark.parametrize("expected", ["wrong", "missing", "new"])
def test_existing_push_rejects_stale_or_wrong_destination_tuple(tmp_path, expected):
    repo, remote, base = _repo(tmp_path)
    tip = _commit(repo, "new.txt", _CLEAN_LINE)
    old = tip if expected == "wrong" else _ZERO40 if expected == "new" else base
    ref = "refs/heads/missing" if expected == "missing" else "refs/heads/main"
    result = _run_hook(repo, "origin", f"refs/heads/main {tip} {ref} {old}\n", url=str(remote))
    assert result.returncode == 1
    assert "stale or unverifiable destination ref" in result.stderr
    assert "not a leak verdict" in result.stderr


@pytest.mark.parametrize("url_present", [False, True])
def test_existing_push_remote_evidence_failure_is_closed(tmp_path, url_present):
    repo, _, base = _repo(tmp_path)
    tip = _commit(repo, "new.txt", _CLEAN_LINE)
    result = _run_hook(
        repo,
        "origin",
        f"refs/heads/main {tip} refs/heads/main {base}\n",
        url=str(tmp_path / "missing.git") if url_present else None,
    )
    assert result.returncode == 1
    assert "not a leak verdict" in result.stderr


@pytest.mark.parametrize("advertisement", ["malformed", "changed", "unavailable"])
def test_advertisement_must_be_valid_and_stable(tmp_path, advertisement):
    repo, remote, base = _repo(tmp_path)
    tip = _commit(repo, "new.txt", _CLEAN_LINE)
    tools = tmp_path / "tools"
    tools.mkdir()
    counter = tmp_path / "enumerations"
    real_git = Path(shutil.which("git")).as_posix()
    shim = tools / "git"
    # Only this test child gets a Git forwarder; production still contacts its
    # actual destination. Alter enumeration, leaving every other command real.
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = ls-remote ]; then\n'
        f"  if [ -f {shlex.quote(counter.as_posix())} ]; then\n"
        + (
            "    exit 1\n"
            if advertisement == "unavailable"
            else f"    printf '{tip}\\trefs/heads/main\\n'\n    exit 0\n"
        )
        + "  fi\n"
        f"  touch {shlex.quote(counter.as_posix())}\n"
        + ("  printf 'invalid advertisement\\n'\n  exit 0\n" if advertisement == "malformed" else "")
        + "fi\n"
        f'exec {shlex.quote(real_git)} "$@"\n'
    )
    result = _run_with_git_shim(
        repo, remote.as_posix(), base, tip, tools, shim, 1 if advertisement == "malformed" else 2
    )
    assert result.returncode == 1
    assert "not a leak verdict" in result.stderr
    assert (
        "malformed destination advertisement" in result.stderr
        if advertisement == "malformed"
        else "could not be reverified" in result.stderr
    )


def test_shallow_history_cannot_prove_all_unpublished_commits(tmp_path):
    repo, remote, base = _repo(tmp_path)
    tip = _commit(repo, "new.txt", _CLEAN_LINE)
    (repo / ".git/shallow").write_text(tip + "\n")
    result = _run_hook(repo, "origin", f"refs/heads/main {tip} refs/heads/main {base}\n", url=str(remote))
    assert result.returncode == 1
    assert "complete local history is required" in result.stderr


def test_missing_local_ancestor_is_not_silently_ignored(tmp_path):
    repo, remote, base = _repo(tmp_path)
    tip = _commit(repo, "new.txt", _CLEAN_LINE)
    missing = repo / ".git/objects" / base[:2] / base[2:]
    missing.chmod(0o600)
    missing.unlink()
    assert not missing.exists()
    result = _run_hook(repo, "origin", f"refs/heads/main {tip} refs/heads/main {base}\n", url=str(remote))
    assert result.returncode == 1
    assert "incomplete local ancestry" in result.stderr


def test_replace_object_cannot_hide_unpublished_scrubbed_leak(tmp_path):
    repo, remote, base = _repo(tmp_path)
    _commit(repo, "leak.txt", _LEAK_LINE)
    _git(repo, "rm", "leak.txt")
    tip = _commit(repo, "clean.txt", _CLEAN_LINE)
    _git(repo, "replace", tip, base)
    result = _run_hook(repo, "origin", f"refs/heads/main {tip} refs/heads/main {base}\n", url=str(remote))
    assert result.returncode == 1
    assert "scanning introduced content in 2 new commit(s)" in result.stdout
    assert "BLOCKED. content-guard found violations." in result.stderr


@pytest.mark.parametrize("location", ["default", "environment"])
def test_legacy_grafts_cannot_hide_unpublished_ancestors(tmp_path, monkeypatch, location):
    repo, remote, base = _repo(tmp_path)
    _commit(repo, "leak.txt", _LEAK_LINE)
    _git(repo, "rm", "leak.txt")
    tip = _commit(repo, "clean.txt", _CLEAN_LINE)
    graft = repo / ".git" / ("info/grafts" if location == "default" else "other-grafts")
    graft.write_text(tip + "\n")
    if location == "environment":
        monkeypatch.setenv("GIT_GRAFT_FILE", str(graft))
    result = _run_hook(repo, "origin", f"refs/heads/main {tip} refs/heads/main {base}\n", url=str(remote))
    assert result.returncode == 1
    assert "grafted local ancestry cannot be verified" in result.stderr


def test_successful_remote_diagnostics_are_not_ref_records(tmp_path, external_hook_shell):
    repo, remote, base = _repo(tmp_path)
    tip = _commit(repo, "new.txt", _CLEAN_LINE)
    tools = tmp_path / "tools"
    tools.mkdir()
    shim = tools / "git"
    real_git = Path(shutil.which("git")).as_posix()
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = ls-remote ]; then printf "transport notice\\n" >&2; fi\n'
        f'exec {shlex.quote(real_git)} "$@"\n'
    )
    result = _run_with_git_shim(repo, remote.as_posix(), base, tip, tools, shim, 2)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr.count("transport notice") == 2
    assert "scanning introduced content in 1 new commit(s)" in result.stdout


def test_remote_failure_message_does_not_echo_destination_credentials(tmp_path, external_hook_shell):
    repo, _, base = _repo(tmp_path)
    tip = _commit(repo, "new.txt", _CLEAN_LINE)
    tools = tmp_path / "tools"
    tools.mkdir()
    shim = tools / "git"
    real_git = Path(shutil.which("git")).as_posix()
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = ls-remote ]; then printf "fixture transport refused\\n" >&2; exit 1; fi\n'
        f'exec {shlex.quote(real_git)} "$@"\n'
    )
    username, password = "EXAMPLE_USER", "EXAMPLE_PASSWORD"
    userinfo = username + ":" + password
    remote = "https://" + userinfo + "@example.invalid/repo.git"
    result = _run_with_git_shim(repo, remote, base, tip, tools, shim, 1)
    assert result.returncode == 1
    assert "failed to enumerate advertised refs" in result.stderr
    assert "fixture transport refused" in result.stderr
    assert "not a leak verdict" in result.stderr
    assert remote not in result.stdout + result.stderr
    assert userinfo not in result.stdout + result.stderr
    assert username not in result.stdout + result.stderr
    assert password not in result.stdout + result.stderr
