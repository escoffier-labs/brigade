"""Exercise real Git isolation with provider and publication subprocesses mocked."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from brigade import cli, cloud_tracker, codex_cloud, codex_cloud_land, proc


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


@pytest.fixture
def landing(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    git(remote, "config", "user.email", "dev@example.invalid")
    git(remote, "config", "user.name", "Example")
    git(remote, "config", "core.hooksPath", "/dev/null")
    (remote / "file.txt").write_text("before\n")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "test: initial")
    caller = tmp_path / "caller"
    git(tmp_path, "clone", str(remote), str(caller))
    git(caller, "config", "user.email", "dev@example.invalid")
    git(caller, "config", "user.name", "Example")
    git(caller, "config", "core.hooksPath", "/dev/null")
    (caller / "file.txt").write_text("caller dirty\n")
    cloud_tracker.register(caller, provider="codex-cloud", task_id="task_example", label="example")
    monkeypatch.setattr(codex_cloud, "_repo_from_cwd", lambda _: "example/repo")
    original = proc.run
    calls = []
    state = {"status": "[READY] example", "failure": None, "path": "file.txt"}

    def run(argv, **kwargs):
        calls.append((argv, kwargs.get("cwd")))
        if argv[:3] == ["codex", "cloud", "status"]:
            if callback := state.get("during_status"):
                callback()
            return proc.Result(0, state["status"], "")
        if argv[:3] == ["codex", "cloud", "diff"]:
            path = state["path"]
            return proc.Result(
                0, f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-before\n+after\n", ""
            )
        if argv[:3] == ["codex", "cloud", "apply"]:
            assert kwargs["cwd"] != caller
            if state["failure"] == "apply":
                return proc.Result(1, "private failure", "")
            (kwargs["cwd"] / "file.txt").write_text("after\n")
            for name, content in state.get("extra_files", {}).items():
                path = kwargs["cwd"] / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                if name in state.get("executable_files", []):
                    path.chmod(0o700)
            return proc.Result(0, "", "")
        if argv[0] == "brigade" and state.get("execute_guard"):
            script = argv[argv.index("--command") + 1]
            guard_cwd = Path(argv[argv.index("--target") + 1])
            assert Path(script).read_text().splitlines()[0] == f"#!{sys.executable} -I"
            return original(
                [sys.executable, "-I", script],
                cwd=guard_cwd,
                env={**os.environ, "PYTHONPATH": "src", **state.get("guard_env", {})},
            )
        if argv[0] in {"brigade", "vale"}:
            return proc.Result(int(state["failure"] == argv[0]), "", "")
        if argv[:2] == ["git", "push"]:
            return proc.Result(0, "", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return proc.Result(0, json.dumps([{"url": state["published"]}] if state.get("published") else []), "")
        if argv[:3] == ["gh", "pr", "create"]:
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            assert "task_example" in body and "#2473" in body
            state["published"] = "https://github.com/example/repo/pull/123"
            if state.get("lose_pr_response"):
                return proc.Result(1, "", "")
            return proc.Result(0, state["published"] + "\n", "")
        return original(argv, **kwargs)

    monkeypatch.setattr(proc, "run", run)
    return caller, state, calls


def test_land_isolated_and_registered(landing):
    caller, _, calls = landing
    head = git(caller, "rev-parse", "HEAD")
    result = codex_cloud_land.land(caller, "task_example", 2473)
    assert result["pr_url"] == "https://github.com/example/repo/pull/123"
    assert git(caller, "rev-parse", "HEAD") == head
    assert (caller / "file.txt").read_text() == "caller dirty\n"
    assert len(git(caller, "worktree", "list").splitlines()) == 1
    commit = git(caller, "log", "-1", "--format=%B", result["branch"])
    assert commit.endswith("Co-Authored-By: Codex <codex@openai.com>")  # content-guard: allow pii/email
    entry = cloud_tracker.load_registry(caller)["entries"][0]
    assert entry["pr_url"] == result["pr_url"]
    payload = cloud_tracker.status_payload(caller)
    assert payload["entries"][0]["pr_url"] == result["pr_url"]
    sync = cloud_tracker.sync_payload(caller, hub_leases=[])
    assert sync["prs"] == [{"task_id": "task_example", "pr_url": result["pr_url"]}]
    count = len(calls)
    assert codex_cloud_land.land(caller, "task_example", 2473) == result
    assert len(calls) == count
    assert not any("merge" in argv for argv, _ in calls)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/test.yml",
        "CODEOWNERS",
        "docs/CODEOWNERS",
        ".env",
        "secrets/key",
        "auth.json",
        ".ssh/config",
        "client.pem",
        "hooks/pre-push",
        ".kube/config",
        ".docker/config.json",
        "service-account.json",
        "auth.toml",
        "id_ecdsa",
    ],
)
def test_refuse_sensitive_diff_before_apply(landing, path):
    caller, state, calls = landing
    state["path"] = path
    with pytest.raises(codex_cloud_land.LandError, match="protected-policy-path|credential-path|protected-hook-path"):
        codex_cloud_land.land(caller, "task_example", 2473)
    assert not any(argv[:3] == ["codex", "cloud", "apply"] for argv, _ in calls)
    assert (caller / "file.txt").read_text() == "caller dirty\n"


@pytest.mark.parametrize(
    "failure,reason", [("apply", "apply-conflict"), ("brigade", "guard-failed"), ("vale", "pr-body-lint-failed")]
)
def test_failure_never_publishes(landing, failure, reason):
    caller, state, calls = landing
    state["failure"] = failure
    with pytest.raises(codex_cloud_land.LandError, match=reason):
        codex_cloud_land.land(caller, "task_example", 2473)
    assert not any(argv[:2] == ["git", "push"] for argv, _ in calls)
    assert cloud_tracker.load_registry(caller)["entries"][0].get("pr_url") is None
    assert git(caller, "branch", "--list", "codex/*") == ""
    assert len(git(caller, "worktree", "list").splitlines()) == 1


def test_nonready_refused(landing):
    caller, state, calls = landing
    state["status"] = "[RUNNING] ready in title"
    with pytest.raises(codex_cloud_land.LandError, match="task-not-ready"):
        codex_cloud_land.land(caller, "task_example", 2473)
    assert not any(argv[:3] == ["git", "worktree", "add"] for argv, _ in calls)


def test_command_shape(tmp_path, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(codex_cloud_land, "land", lambda *args: seen.append(args) or {"pr_url": "example"})
    assert (
        cli.main(
            [
                "run",
                "cloud",
                "land",
                "--provider",
                "codex-cloud",
                "--task-id",
                "task_example",
                "--issue",
                "2473",
                "--target",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert seen == [(tmp_path, "task_example", 2473)]
    assert capsys.readouterr().out.strip() == "example"


def test_apply_conflict_before_provider_mutation(landing):
    caller, state, calls = landing
    state["path"] = "missing.txt"
    with pytest.raises(codex_cloud_land.LandError, match="apply-conflict"):
        codex_cloud_land.land(caller, "task_example", 2473)
    assert not any(argv[:3] == ["codex", "cloud", "apply"] for argv, _ in calls)


def test_unregistered_task_refused(landing):
    caller, _, calls = landing
    with pytest.raises(codex_cloud_land.LandError, match="task-not-uniquely-registered"):
        codex_cloud_land.land(caller, "task_unregistered", 2473)
    assert calls == []


@pytest.mark.parametrize("hooks_path", [".husky", "custom/hooks"])
def test_effective_hooks_directory_refused(landing, hooks_path):
    caller, state, calls = landing
    git(caller, "config", "core.hooksPath", hooks_path)
    state["path"] = hooks_path + "/pre-commit"
    with pytest.raises(codex_cloud_land.LandError, match="protected-hook-path"):
        codex_cloud_land.land(caller, "task_example", 2473)
    assert not any(argv[:3] == ["codex", "cloud", "apply"] for argv, _ in calls)


def test_guard_wrapper_ignores_task_pythonpath(landing, tmp_path):
    caller, state, _ = landing
    marker = tmp_path / "executed"
    state["extra_files"] = {
        "src/subprocess.py": 'import os\nfrom pathlib import Path\nPath(os.environ["BRIGADE_TEST_MARKER"]).touch()\nraise SystemExit(0)\n',
        "src/brigade/__init__.py": 'import os\nfrom pathlib import Path\nPath(os.environ["BRIGADE_TEST_MARKER"]).touch()\nraise SystemExit(0)\n',
    }
    state["guard_env"] = {"BRIGADE_TEST_MARKER": str(marker)}
    state["execute_guard"] = True
    result = codex_cloud_land.land(caller, "task_example", 2473)
    assert result["pr_url"].endswith("/123")
    assert not marker.exists()


def test_task_executable_cannot_shadow_git_via_relative_path(landing, tmp_path, monkeypatch):
    caller, state, _ = landing
    marker = tmp_path / "executed"
    state["extra_files"] = {"git": '#!/bin/sh\n: > "$BRIGADE_TEST_MARKER"\nexit 1\n'}
    state["executable_files"] = ["git"]
    monkeypatch.setenv("BRIGADE_TEST_MARKER", str(marker))
    monkeypatch.setenv("PATH", "." + os.pathsep + os.environ["PATH"])
    result = codex_cloud_land.land(caller, "task_example", 2473)
    assert result["pr_url"].endswith("/123")
    assert not marker.exists()


@pytest.mark.parametrize("failure", ["response", "registry"])
def test_created_pr_recovered_without_duplicate(landing, monkeypatch, failure):
    caller, state, calls = landing
    original = cloud_tracker.save_registry
    fail_once = failure == "registry"

    def save(target, registry):
        nonlocal fail_once
        if fail_once and any(row.get("pr_url") for row in registry["entries"]):
            fail_once = False
            raise OSError("simulated registry write failure")
        return original(target, registry)

    monkeypatch.setattr(cloud_tracker, "save_registry", save)
    state["lose_pr_response"] = failure == "response"
    with pytest.raises(codex_cloud_land.LandError, match="pr-create-failed|registry-update-failed"):
        codex_cloud_land.land(caller, "task_example", 2473)
    branch = cloud_tracker.load_registry(caller)["entries"][0]["landing_branch"]
    assert branch
    result = codex_cloud_land.land(caller, "task_example", 2473)
    assert result["pr_url"] == state["published"]
    assert result["branch"] == branch
    assert sum(argv[:3] == ["gh", "pr", "create"] for argv, _ in calls) == 1
    assert sum(argv[:3] == ["codex", "cloud", "apply"] for argv, _ in calls) == 1


def test_external_calls_leave_registry_available(landing):
    caller, state, _ = landing
    errors = []

    def register():
        try:
            cloud_tracker.register(caller, provider="codex-cloud", task_id="task_other", label="other")
        except Exception as exc:
            errors.append(exc)

    def during_status():
        thread = threading.Thread(target=register)
        thread.start()
        thread.join(2)
        blocked = thread.is_alive()
        thread.join(5)
        assert not blocked
        assert not errors

    state["during_status"] = during_status
    codex_cloud_land.land(caller, "task_example", 2473)
    assert {row["task_id"] for row in cloud_tracker.load_registry(caller)["entries"]} == {"task_example", "task_other"}


def test_hook_cannot_publish_a_tree_that_guard_did_not_check(landing, tmp_path):
    caller, _, calls = landing
    hooks = tmp_path / "trusted-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text('#!/bin/sh\nprintf "hook change\\n" > file.txt\ngit add file.txt\n')
    hook.chmod(0o700)
    git(caller, "config", "core.hooksPath", str(hooks))
    with pytest.raises(codex_cloud_land.LandError, match="commit-tree-mismatch"):
        codex_cloud_land.land(caller, "task_example", 2473)
    assert not any(argv[:2] == ["git", "push"] for argv, _ in calls)
