"""Exercise real Git isolation with provider and publication subprocesses mocked."""

from __future__ import annotations

import subprocess
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
            return proc.Result(0, "", "")
        if argv[0] in {"brigade", "vale"}:
            return proc.Result(int(state["failure"] == argv[0]), "", "")
        if argv[:2] == ["git", "push"]:
            return proc.Result(0, "", "")
        if argv[:3] == ["gh", "pr", "create"]:
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            assert "task_example" in body and "#2473" in body
            return proc.Result(0, "https://github.com/example/repo/pull/123\n", "")
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
