"""Exercise real Git isolation with provider and publication subprocesses mocked."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from brigade import cli, cloud_tracker, codex_cloud, codex_cloud_land, proc
from tests._home import set_home


GIT_EXECUTABLE = shutil.which("git")


def git(root, *args):
    return subprocess.check_output([GIT_EXECUTABLE, *args], cwd=root, text=True).strip()


@pytest.fixture
def landing(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("Landing private-directory and descriptor guarantees require POSIX")
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
    git(caller, "remote", "set-url", "origin", "https://github.com/example/repo.git")
    (caller / "file.txt").write_text("caller dirty\n")
    cloud_tracker.register(caller, provider="codex-cloud", task_id="task_example", label="example")
    monkeypatch.setattr(codex_cloud, "_repo_from_cwd", lambda _: "example/repo")
    original_which = shutil.which

    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(0o700)

    def trusted_tool(name, **kwargs):
        if name == "gh":
            return str(gh)
        if name in {"codex", "gh", "brigade", "vale"}:
            return "/opt/trusted-tools/" + name
        return original_which(name, **kwargs)

    monkeypatch.setattr(shutil, "which", trusted_tool)
    home = tmp_path / "home"
    home.mkdir()
    trusted_hooks = home / "hooks"
    trusted_hooks.mkdir()
    (trusted_hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    (trusted_hooks / "pre-push").chmod(0o700)
    (home / ".gitconfig").write_text(
        f"[core]\n hooksPath = {trusted_hooks}\n[user]\n name = Example\n email = dev@example.invalid\n"
        f'[credential "https://github.com"]\n helper = !{gh} auth git-credential\n'
    )
    set_home(monkeypatch, home)
    original = proc.run
    calls = []
    state = {"status": "[READY] example", "failure": None, "path": "file.txt", "text_scans": []}

    def probe_transport(argv, kwargs):
        # Replace HTTPS at the transport boundary, after real Git resolves its config.
        safe = state["https_marker"]
        safe.unlink(missing_ok=True)
        result = original(argv, **{**kwargs, "env": {**kwargs["env"], "GIT_EXEC_PATH": str(state["exec_path"])}})
        return None if safe.exists() else result

    def run(argv, **kwargs):
        actual_argv = argv
        state.setdefault("raw_calls", []).append((list(argv), kwargs.get("env")))
        if "brigade.guard" in argv and "--no-allow-comments" in argv:
            state["text_scans"].append((list(argv), kwargs["stdin"].decode()))
        name = "gh" if Path(argv[0]).resolve() == gh.resolve() else Path(argv[0]).name.removesuffix(".js")
        argv = [name, *argv[1:]]
        if argv[0] == "git":
            while argv[1:2] == ["-c"]:
                del argv[1:3]
            if argv[1:2] == ["--no-pager"]:
                del argv[1]
        calls.append((argv, kwargs.get("cwd")))
        if argv[:3] == ["codex", "cloud", "status"]:
            if callback := state.get("during_status"):
                callback()
            return proc.Result(0, state["status"], "")
        if argv[:3] == ["codex", "cloud", "diff"]:
            if state.get("diff"):
                return proc.Result(0, state["diff"], "")
            if callback := state.get("during_diff"):
                callback(kwargs["cwd"])
            path = state["path"]
            patch = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-before\n+after\n"
            for name, content in state.get("extra_files", {}).items():
                lines = content.splitlines()
                mode = "100755" if name in state.get("executable_files", []) else "100644"
                patch += (
                    f"diff --git a/{name} b/{name}\nnew file mode {mode}\n--- /dev/null\n+++ b/{name}\n@@ -0,0 +1,{len(lines)} @@\n"
                    + "".join("+" + line + "\n" for line in lines)
                )
            state["last_diff"] = patch.encode()
            return proc.Result(0, patch, "")
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
            receipt_directory = guard_cwd / ".brigade"
            receipt_directory.mkdir(exist_ok=True)
            (receipt_directory / "receipt-test").touch()
            assert Path(script).read_text().splitlines()[0] == f"#!{sys.executable} -I"
            return original(
                [sys.executable, "-I", script],
                cwd=guard_cwd,
                env={**os.environ, "PYTHONPATH": "src", **state.get("guard_env", {})},
            )
        if argv[0] in {"brigade", "vale"}:
            return proc.Result(int(state["failure"] == argv[0]), "", "")
        if argv[:2] == ["git", "push"]:
            if state.get("probe_rewrite"):
                result = probe_transport(actual_argv, kwargs)
                if result is not None:
                    return result
            if state.get("probe_proxy"):
                effective = original(
                    [actual_argv[0], "config", "--get-urlmatch", "http", "https://github.com/example/repo.git"],
                    cwd=kwargs["cwd"],
                    env=kwargs["env"],
                )
                state["push_http"] = effective.stdout.lower()
            if state["failure"] == "push":
                return proc.Result(1, "", state.get("push_error", "network timeout"))
            if callback := state.get("during_push"):
                callback(kwargs["cwd"])
            state["remote_sha"] = git(kwargs["cwd"], "rev-parse", argv[-1].split(":")[0])
            state["commit_message"] = git(kwargs["cwd"], "log", "-1", "--format=%B")
            state["published_file"] = (kwargs["cwd"] / "file.txt").read_text()
            return proc.Result(0, "", "")
        if argv[:2] == ["git", "ls-remote"]:
            if state.get("probe_rewrite"):
                result = probe_transport(actual_argv, kwargs)
                if result is not None:
                    return result
            return proc.Result(0, state.get("remote_sha", "") + "\t" + argv[-1] if state.get("remote_sha") else "", "")
        if argv[:4] == ["git", "config", "--get", "remote.origin.url"]:
            return proc.Result(0, "https://github.com/example/repo.git\n", "")
        if argv[:2] == ["git", "fetch"] and "https://github.com/example/repo.git" in argv:
            return original(
                ["git", "-c", "protocol.file.allow=always", "fetch", "--no-tags", str(remote), "refs/heads/main"],
                **kwargs,
            )
        if argv[:2] == ["git", "apply"] and "--index" in argv:
            state["applied_diff"] = kwargs["stdin"]
            if state["failure"] == "apply":
                return proc.Result(1, "", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return proc.Result(0, json.dumps([{"url": state["published"]}] if state.get("published") else []), "")
        if argv[:3] == ["gh", "pr", "create"]:
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            assert "task_example" in body and "#2473" in body
            state["body"] = body
            state["title"] = argv[argv.index("--title") + 1]
            if state["failure"] == "pr":
                return proc.Result(1, "", "")
            state["published"] = "https://github.com/example/repo/pull/123"
            if state.get("lose_pr_response"):
                return proc.Result(1, "", "")
            return proc.Result(0, state["published"] + "\n", "")
        return original(actual_argv, **kwargs)

    monkeypatch.setattr(proc, "run", run)
    return caller, state, calls


def test_land_isolated_and_registered(landing):
    caller, state, calls = landing
    head = git(caller, "rev-parse", "HEAD")
    result = codex_cloud_land.land(caller, "task_example", 2473)
    assert result["pr_url"] == "https://github.com/example/repo/pull/123"
    assert git(caller, "rev-parse", "HEAD") == head
    assert (caller / "file.txt").read_text() == "caller dirty\n"
    assert len(git(caller, "worktree", "list").splitlines()) == 1
    commit = state["commit_message"]
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
    assert sum(argv[:3] == ["codex", "cloud", "diff"] for argv, _ in calls) == 1


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
    caller, state, calls = landing
    hooks = tmp_path / "trusted-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text('#!/bin/sh\nprintf "hook change\\n" > file.txt\ngit add file.txt\n')
    hook.chmod(0o700)
    git(caller, "config", "core.hooksPath", str(hooks))
    codex_cloud_land.land(caller, "task_example", 2473)
    assert state["published_file"] == "after\n"


@pytest.mark.parametrize(
    "path",
    [
        ".github/actions/test.yml",
        ".gitattributes",
        ".gitmodules",
        ".pre-commit-config.yaml",
        ".husky/pre-push",
        "lefthook.yml",
        ".lefthook.yaml",
        ".overcommit.yml",
        ".pre-commit-hooks.yaml",
        ".simple-git-hooks.json",
    ],
)
def test_r1_refuse_execution_policy_paths(landing, path):
    caller, state, _ = landing
    state["path"] = path
    with pytest.raises(codex_cloud_land.LandError, match="protected-"):
        codex_cloud_land.land(caller, "task_example", 2473)


@pytest.mark.parametrize("value", ["line\nname.txt", "tab\tname.txt", "escape\x1bname.txt", "delete\x7fname.txt"])
def test_r1_control_paths_refused(value, tmp_path):
    with pytest.raises(codex_cloud_land.LandError, match="unsafe-path"):
        codex_cloud_land._check_path(value, tmp_path, ())


def test_r1_caller_and_worktree_hooks_never_execute(landing, tmp_path):
    caller, state, _ = landing
    hooks = tmp_path / "caller-hooks"
    hooks.mkdir()
    marker = tmp_path / "executed"
    for name in ("pre-commit", "post-checkout", "pre-push"):
        hook = hooks / name
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
        hook.chmod(0o700)
    git(caller, "config", "core.hooksPath", str(hooks))
    codex_cloud_land.land(caller, "task_example", 2473)
    assert not marker.exists()


def test_r1_exact_pinned_patch_and_hash(landing):
    caller, state, calls = landing
    result = codex_cloud_land.land(caller, "task_example", 2473)
    entry = cloud_tracker.load_registry(caller)["entries"][0]
    assert state["applied_diff"] == state["last_diff"]
    assert entry["landing_diff_sha256"] == hashlib.sha256(state["last_diff"]).hexdigest()
    assert entry["landing_attempt"] == 1
    assert len(entry["landing_diff_sha256"]) == hashlib.sha256().digest_size * 2
    assert entry["landing_commit"] == state["remote_sha"]
    assert not any(argv[:3] == ["codex", "cloud", "apply"] for argv, _ in calls)
    assert any(argv[:3] == ["codex", "cloud", "diff"] and "--attempt" in argv for argv, _ in calls)
    assert any(
        argv[:2] == ["git", "push"]
        and argv[-2] == "https://github.com/example/repo.git"
        and argv[-1].endswith(":refs/heads/" + result["branch"])
        for argv, _ in calls
    )


def test_r1_receipts_outside_applied_tree(landing, tmp_path):
    caller, _, calls = landing
    codex_cloud_land.land(caller, "task_example", 2473)
    verification = next((argv, cwd) for argv, cwd in calls if argv[0] == "brigade")
    argv, _ = verification
    receipt_target = Path(argv[argv.index("--target") + 1])
    tree = next(cwd for argv, cwd in calls if argv[:2] == ["git", "commit"])
    assert receipt_target != tree and tree not in receipt_target.parents


@pytest.mark.parametrize("failure,reason", [("push", "push-failed"), ("pr", "pr-create-failed")])
def test_r1_publication_retry_reuses_commit(landing, failure, reason):
    caller, state, calls = landing
    state["failure"] = failure
    with pytest.raises(codex_cloud_land.LandError, match=reason):
        codex_cloud_land.land(caller, "task_example", 2473)
    entry = cloud_tracker.load_registry(caller)["entries"][0]
    sha = entry["landing_commit"]
    assert entry.get("landing_state") == ("prepared" if failure == "push" else "branch-pushed")
    private = Path(entry["landing_workspace"]) / "repository"
    assert git(private, "rev-parse", entry["landing_branch"]) == sha
    assert git(caller, "branch", "--list", entry["landing_branch"]) == ""
    assert cloud_tracker.status_payload(caller)["entries"][0]["landing_state"] == entry["landing_state"]
    assert cloud_tracker.sync_payload(caller, hub_leases=[])["pending_landings"][0]["landing_commit"] == sha
    state["failure"] = None
    codex_cloud_land.land(caller, "task_example", 2473)
    assert state["remote_sha"] == sha
    assert sum(argv[:3] == ["codex", "cloud", "diff"] for argv, _ in calls) == 1
    assert cloud_tracker.load_registry(caller)["entries"][0]["landing_state"] == "pr-created"


def test_r1_duplicate_registration_reuses_entry(landing):
    caller, _, _ = landing
    original = cloud_tracker.load_registry(caller)["entries"][0]
    repeated = cloud_tracker.register(caller, provider="codex-cloud", task_id="task_example", label="duplicate")
    assert repeated == original
    assert cloud_tracker.load_registry(caller)["entries"] == [original]


def test_r1_registry_revision_conflict(landing):
    caller, state, _ = landing

    def replace_entry():
        with cloud_tracker.registry_lock(caller):
            registry = cloud_tracker.load_registry(caller)
            registry["entries"][0]["id"] = "cloud-replaced"
            cloud_tracker.save_registry(caller, registry)

    state["during_status"] = replace_entry
    with pytest.raises(codex_cloud_land.LandError, match="registry-conflict"):
        codex_cloud_land.land(caller, "task_example", 2473)
    assert not cloud_tracker.load_registry(caller)["entries"][0].get("pr_url")


def test_r1_shallow_refused_up_front(landing):
    caller, _, calls = landing
    (caller / ".git" / "shallow").write_text(git(caller, "rev-parse", "HEAD") + "\n")
    with pytest.raises(codex_cloud_land.LandError, match="shallow-target"):
        codex_cloud_land.land(caller, "task_example", 2473)
    assert not any(argv[:2] == ["git", "fetch"] for argv, _ in calls)


@pytest.mark.parametrize(
    "message,reason",
    [
        ("pre-push: content-guard refused secret", "guard-refused"),
        ("Authentication failed secret", "auth"),
        ("Could not resolve host secret", "network"),
        ("rejected non-fast-forward secret", "rejected"),
    ],
)
def test_r1_push_failure_classified_without_output(landing, message, reason):
    caller, state, _ = landing
    state.update(failure="push", push_error=message)
    with pytest.raises(codex_cloud_land.LandError) as error:
        codex_cloud_land.land(caller, "task_example", 2473)
    assert str(error.value) == "push-failed:" + reason


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Repair cloud retry", "fix: Repair cloud retry"),
        ("feat: add cloud support", "feat: add cloud support"),
        ("", "fix: address #2473"),
    ],
)
def test_r1_task_title(landing, title, expected):
    caller, state, _ = landing
    state["status"] = "[READY] " + title
    codex_cloud_land.land(caller, "task_example", 2473)
    assert state["title"] == expected


def test_r1_closes_issue(landing):
    caller, state, _ = landing
    codex_cloud_land.land(caller, "task_example", 2473, closes=True)
    assert "Closes #2473" in state["body"]


def test_r1_git_environment_and_absolute_tools(landing, monkeypatch):
    caller, state, _ = landing
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "untrusted-command")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "untrusted-command")
    codex_cloud_land.land(caller, "task_example", 2473)
    for argv, environment in state["raw_calls"]:
        assert Path(argv[0]).is_absolute()
        assert "GIT_EXTERNAL_DIFF" not in environment
        if Path(argv[0]).name == "git" and "commit" in argv:
            assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
            assert Path(environment["GIT_CONFIG_GLOBAL"]).name == "gitconfig"
            assert not Path(environment["GIT_CONFIG_GLOBAL"]).is_relative_to(caller)


def test_r1_filters_do_not_execute(landing, tmp_path):
    caller, state, _ = landing
    remote = caller.parent / "remote"
    (remote / ".gitattributes").write_text("*.txt filter=hostile diff=hostile\n")
    git(remote, "add", ".gitattributes")
    git(remote, "commit", "-m", "test: attributes")
    marker = tmp_path / "executed"
    for key in ("filter.hostile.smudge", "filter.hostile.clean", "diff.hostile.textconv", "core.fsmonitor"):
        git(caller, "config", key, f'touch "{marker}"')
    codex_cloud_land.land(caller, "task_example", 2473)
    assert not marker.exists()


def test_r1_worktree_local_hook_never_executes(landing, tmp_path):
    caller, state, _ = landing
    marker = tmp_path / "executed"
    hooks = tmp_path / "worktree-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    hook.chmod(0o700)
    git(caller, "config", "extensions.worktreeConfig", "true")

    def inject(_):
        # Caller worktree config remains outside the landing repository.
        git(caller, "config", "--worktree", "core.hooksPath", str(hooks))

    state["during_diff"] = inject
    codex_cloud_land.land(caller, "task_example", 2473)
    assert not marker.exists()


def test_r1_guard_receipt_ignores_base_symlinks(landing, tmp_path):
    caller, state, _ = landing
    remote = caller.parent / "remote"
    victim = tmp_path / "victim"
    victim.mkdir()
    (remote / ".brigade").symlink_to(victim, target_is_directory=True)
    (remote / "memory").symlink_to(victim, target_is_directory=True)
    git(remote, "add", ".brigade", "memory")
    git(remote, "commit", "-m", "test: base symlinks")
    state["execute_guard"] = True
    codex_cloud_land.land(caller, "task_example", 2473)
    assert list(victim.iterdir()) == []


def test_r1_remote_ref_conflict_never_overwritten(landing):
    caller, state, _ = landing
    state["failure"] = "push"
    with pytest.raises(codex_cloud_land.LandError, match="push-failed"):
        codex_cloud_land.land(caller, "task_example", 2473)
    state.update(failure=None, remote_sha="a" * 40)
    with pytest.raises(codex_cloud_land.LandError, match="landing-ref-mismatch"):
        codex_cloud_land.land(caller, "task_example", 2473)


def test_r1_land_help_explains_ci_boundary(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["run", "cloud", "land", "--help"])
    assert error.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "no secrets" in output and "write token or trusted caches" in output
    assert "#2512" in output
    assert "--closes" in output


def test_r1_task_modified_signer_never_executes(landing, tmp_path):
    caller, state, _ = landing
    remote = caller.parent / "remote"
    signer = remote / "signer"
    signer.write_text("#!/bin/sh\nexit 1\n")
    signer.chmod(0o700)
    git(remote, "add", "signer")
    git(remote, "commit", "-m", "test: signer")
    marker = tmp_path / "executed"
    state["diff"] = (
        "diff --git a/signer b/signer\n--- a/signer\n+++ b/signer\n@@ -1,2 +1,3 @@\n"
        f' #!/bin/sh\n+touch "{marker}"\n exit 1\n'
    )
    git(caller, "config", "commit.gpgsign", "true")
    git(caller, "config", "gpg.program", "./signer")
    try:
        result = codex_cloud_land.land(caller, "task_example", 2473)
    finally:
        assert not marker.exists()
    assert result["pr_url"].endswith("/123")


def test_r1_other_executable_config_neutralized(landing):
    caller, state, _ = landing
    keys = (
        "core.editor",
        "core.pager",
        "core.sshCommand",
        "sequence.editor",
        "merge.example.driver",
        "filter.example.process",
        "diff.example.command",
        "credential.helper",
        "credential.https://github.com.helper",
        "gpg.program",
        "gpg.ssh.program",
        "gpg.x509.program",
    )
    for key in keys:
        git(caller, "config", key, "./task-program")
    codex_cloud_land.land(caller, "task_example", 2473)
    # Caller executable settings never enter the private repository.
    assert state["remote_sha"]
    assert state["published_file"] == "after\n"


@pytest.mark.parametrize("included", [False, True])
@pytest.mark.parametrize("rewrite", ["insteadOf", "pushInsteadOf"])
def test_r2_caller_rewrite_cannot_run_helper(landing, tmp_path, included, rewrite):
    caller, state, _ = landing
    marker = tmp_path / "executed"
    helper = tmp_path / "probe"
    helper.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    helper.chmod(0o700)
    config = caller / ".git" / "config"
    if included:
        config = tmp_path / "included.gitconfig"
        git(caller, "config", "include.path", str(config))
    subprocess.check_call(
        ["git", "config", "--file", str(config), f"url.ext::{helper}.{rewrite}", "https://github.com/example/repo.git"]
    )
    subprocess.check_call(["git", "config", "--file", str(config), "protocol.ext.allow", "always"])
    executables = tmp_path / "git-exec"
    executables.mkdir()
    safe = tmp_path / "https-executed"
    https = executables / "git-remote-https"
    https.write_text(f'#!/bin/sh\ntouch "{safe}"\nexit 1\n')
    https.chmod(0o700)
    (executables / "git-remote-ext").symlink_to(Path(git(caller, "--exec-path")) / "git-remote-ext")
    state.update(exec_path=executables, https_marker=safe)
    state["probe_rewrite"] = True
    try:
        result = codex_cloud_land.land(caller, "task_example", 2473)
    finally:
        assert not marker.exists()
    assert result["pr_url"].endswith("/123")


@pytest.mark.parametrize("scoped", [False, True])
def test_r2_caller_proxy_cannot_affect_push(landing, scoped):
    caller, state, _ = landing
    prefix = "http.https://github.com." if scoped else "http."
    git(caller, "config", prefix + "proxy", "http://proxy.example.invalid:8080")
    git(caller, "config", prefix + "sslVerify", "false")
    state["probe_proxy"] = True
    codex_cloud_land.land(caller, "task_example", 2473)
    assert "proxy" not in state["push_http"]
    assert "http.sslverify true" in state["push_http"]


def test_r2_non_posix_refused_before_work(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_cloud_land, "os", types.SimpleNamespace(**{**vars(os), "name": "nt"}))
    with pytest.raises(codex_cloud_land.LandError, match="^unsupported-platform$"):
        codex_cloud_land.land(tmp_path, "task_example", 2473)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("url.ext::probe.insteadOf", "https://github.com/"),
        ("url.ext::probe.pushInsteadOf", "https://github.com/"),
        ("protocol.ext.allow", "always"),
        ("protocol.https.allow", "never"),
        ("http.proxy", "http://proxy.example.invalid:8080"),
        ("http.https://github.com.proxy", "http://proxy.example.invalid:8080"),
        ("http.sslVerify", "false"),
        ("http.https://github.com.sslVerify", "false"),
        ("include.path", "missing.gitconfig"),
        ("includeIf.gitdir:/.path", "missing.gitconfig"),
    ],
)
def test_r2_unsafe_effective_config_refused_before_transport(landing, key, value):
    caller, state, calls = landing

    def inject(cwd):
        git(cwd, "config", key, value)

    state["during_diff"] = inject
    with pytest.raises(codex_cloud_land.LandError, match="^unsafe-git-config$"):
        codex_cloud_land.land(caller, "task_example", 2473)
    injected = next(index for index, (argv, _) in enumerate(calls) if argv[:3] == ["codex", "cloud", "diff"])
    assert not any(
        argv[:2] in (["git", "fetch"], ["git", "push"], ["git", "ls-remote"]) for argv, _ in calls[injected + 1 :]
    )


@pytest.mark.parametrize(
    "helper", ["store", "!gh auth git-credential extra", "/nonexistent/credential", "!touch probe"]
)
def test_r2_untrusted_operator_helper_refused(landing, helper):
    caller, _, calls = landing
    git(caller, "config", "--global", "credential.https://github.com.helper", helper)
    with pytest.raises(codex_cloud_land.LandError, match="^unsafe-git-config$"):
        codex_cloud_land.land(caller, "task_example", 2473)
    assert not any(argv[:2] in (["git", "fetch"], ["git", "push"], ["git", "ls-remote"]) for argv, _ in calls)


def test_r2_all_git_calls_use_private_repository(landing):
    caller, state, calls = landing
    codex_cloud_land.land(caller, "task_example", 2473)
    for argv, cwd in calls:
        if argv[0] == "git":
            assert not cwd.is_relative_to(caller)
            assert cwd.parent.name.startswith("brigade-cloud-land-")
    assert git(caller, "branch", "--list", "codex/*") == ""
    assert state["published_file"] == "after\n"


@pytest.fixture
def interrupted_publication(landing, monkeypatch):
    caller, state, calls = landing
    original = codex_cloud_land._LandingGit.git
    interrupted = False

    def git_with_interrupt(self, args, *argv, **kwargs):
        nonlocal interrupted
        result = original(self, args, *argv, **kwargs)
        if args[:2] == ["worktree", "add"] and Path(args[-2]).name == "publication" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(codex_cloud_land._LandingGit, "git", git_with_interrupt)
    with pytest.raises(KeyboardInterrupt):
        codex_cloud_land.land(caller, "task_example", 2473)
    entry = cloud_tracker.load_registry(caller)["entries"][0]
    publication = Path(entry["landing_workspace"]) / "publication"
    assert publication.is_dir()
    assert git(publication, "rev-parse", "HEAD") == entry["landing_commit"]
    return caller, state, calls, entry, publication


def test_r2_interrupted_publication_worktree_is_reused(interrupted_publication):
    caller, state, calls, entry, publication = interrupted_publication
    result = codex_cloud_land.land(caller, "task_example", 2473)
    assert result["pr_url"].endswith("/123")
    assert state["remote_sha"] == entry["landing_commit"]
    assert sum(argv[:2] == ["git", "commit"] for argv, _ in calls) == 1
    assert sum(argv[:2] == ["git", "worktree"] and "add" in argv and str(publication) in argv for argv, _ in calls) == 1


@pytest.mark.parametrize("damage", ["branch", "head", "local-ref", "unregistered", "missing", "symlink", "file"])
def test_r2_invalid_publication_worktree_is_recreated(interrupted_publication, tmp_path, damage):
    caller, state, calls, entry, publication = interrupted_publication
    repository = publication.parent / "repository"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep\n")
    if damage == "branch":
        git(publication, "checkout", "-b", "other")
    elif damage == "head":
        git(publication, "checkout", "--detach", "HEAD^")
    elif damage == "local-ref":
        git(publication, "update-ref", "refs/heads/" + entry["landing_branch"], "HEAD^")
    elif damage in {"unregistered", "file"}:
        git(repository, "worktree", "remove", "--force", str(publication))
        if damage == "file":
            publication.write_text("stale\n")
        else:
            publication.mkdir()
            (publication / "stale.txt").write_text("stale\n")
            (publication / "linked-directory").symlink_to(victim, target_is_directory=True)
    else:
        shutil.rmtree(publication)
        if damage == "symlink":
            publication.symlink_to(victim, target_is_directory=True)
    result = codex_cloud_land.land(caller, "task_example", 2473)
    assert result["pr_url"].endswith("/123")
    assert state["remote_sha"] == entry["landing_commit"]
    assert (victim / "keep.txt").read_text() == "keep\n"
    assert sum(argv[:2] == ["git", "commit"] for argv, _ in calls) == 1
    assert sum(argv[:2] == ["git", "worktree"] and "add" in argv and str(publication) in argv for argv, _ in calls) == 2


def test_r2_symlinked_gh_helper_uses_pinned_executable(landing, tmp_path):
    caller, state, _ = landing
    alias = tmp_path / "gh"
    executable = tmp_path / "gh-versioned"
    alias.rename(executable)
    alias.symlink_to(executable)
    result = codex_cloud_land.land(caller, "task_example", 2473)
    assert result["pr_url"].endswith("/123")
    assert any(Path(argv[0]) == executable for argv, _ in state["raw_calls"] if argv[1:3] == ["pr", "create"])


@pytest.mark.parametrize("operation", ["prepare-add", "prepare-remove", "publication-add", "publication-remove"])
@pytest.mark.parametrize("point", ["before", "after"])
def test_r2_retry_reconciles_every_checkout_transition(landing, monkeypatch, operation, point):
    caller, state, calls = landing
    codex_cloud_land.land(caller, "task_example", 2473)
    reference = cloud_tracker.load_registry(caller)["entries"][0]
    fields = (
        "landing_state",
        "landing_tree",
        "landing_base",
        "landing_diff_sha256",
        "landing_attempt",
        "landing_title",
        "expected_artifact",
        "pr_url",
        "landing_workspace",
    )
    expected = {name: reference[name] for name in fields}
    with cloud_tracker.registry_lock(caller):
        registry = cloud_tracker.load_registry(caller)
        registry["entries"] = []
        cloud_tracker.save_registry(caller, registry)
    cloud_tracker.register(caller, provider="codex-cloud", task_id="task_example", label="example")
    for name in ("remote_sha", "published"):
        state.pop(name, None)
    calls.clear()
    original = codex_cloud_land._LandingGit.git
    interrupted = False

    def git_with_interrupt(self, args, *argv, **kwargs):
        nonlocal interrupted
        current = None
        if args[:2] == ["worktree", "add"]:
            current = ("prepare" if Path(args[-2]).name == "worktree" else "publication") + "-add"
        elif args[:2] == ["worktree", "remove"]:
            current = ("prepare" if Path(args[-1]).name == "worktree" else "publication") + "-remove"
        trigger = current == operation and not interrupted
        if trigger and point == "before":
            interrupted = True
            raise KeyboardInterrupt
        result = original(self, args, *argv, **kwargs)
        if trigger and point == "after":
            interrupted = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(codex_cloud_land._LandingGit, "git", git_with_interrupt)
    with pytest.raises(KeyboardInterrupt):
        codex_cloud_land.land(caller, "task_example", 2473)
    partial = cloud_tracker.load_registry(caller)["entries"][0]
    pinned = partial.get("landing_commit")
    result = codex_cloud_land.land(caller, "task_example", 2473)
    final = cloud_tracker.load_registry(caller)["entries"][0]
    assert {name: final[name] for name in fields} == expected
    assert result["branch"] == final["landing_branch"]
    assert state["remote_sha"] == final["landing_commit"]
    if pinned:
        assert final["landing_commit"] == pinned
    assert sum(argv[:2] == ["git", "commit"] for argv, _ in calls) == 1
    assert sum(argv[:3] == ["gh", "pr", "create"] for argv, _ in calls) == 1
    assert git(caller, "branch", "--list", "codex/*") == ""


@pytest.mark.parametrize("state_kind", ["moved", "stray", "stray-symlink", "stray-parent-symlink"])
def test_r2_reconcile_all_owned_checkouts(interrupted_publication, tmp_path, state_kind):
    caller, state, _, entry, publication = interrupted_publication
    repository = publication.parent / "repository"
    other = publication.parent / "another-checkout"
    if state_kind == "stray-parent-symlink":
        other = publication.parent / "nested" / "another-checkout"
        other.parent.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep\n")
    if state_kind == "moved":
        git(repository, "worktree", "move", str(publication), str(other))
    else:
        git(repository, "worktree", "add", "--detach", str(other), entry["landing_base"])
        if state_kind in {"stray-symlink", "stray-parent-symlink"}:
            linked = other.parent if state_kind == "stray-parent-symlink" else other
            shutil.rmtree(linked)
            linked.symlink_to(victim, target_is_directory=True)

    def during_push(cwd):
        if state_kind == "moved":
            assert cwd == other
        else:
            assert not other.exists() and not other.is_symlink()
            assert str(other) not in git(repository, "worktree", "list", "--porcelain")
        assert (victim / "keep.txt").read_text() == "keep\n"

    state["during_push"] = during_push
    assert codex_cloud_land.land(caller, "task_example", 2473)["pr_url"].endswith("/123")
    assert state["remote_sha"] == entry["landing_commit"]
    assert (victim / "keep.txt").read_text() == "keep\n"


def test_r2_gh_alias_to_other_executable_is_refused(landing, tmp_path):
    caller, _, calls = landing
    other = tmp_path / "other-executable"
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(0o700)
    alias = tmp_path / "other-gh"
    alias.symlink_to(other)
    git(caller, "config", "--global", "credential.https://github.com.helper", f"!{alias} auth git-credential")
    with pytest.raises(codex_cloud_land.LandError, match="^unsafe-git-config$"):
        codex_cloud_land.land(caller, "task_example", 2473)
    assert not any(argv[:2] in (["git", "fetch"], ["git", "push"], ["git", "ls-remote"]) for argv, _ in calls)


def test_r2_reconcile_preserves_trusted_tools(interrupted_publication):
    caller, _, _, entry, publication = interrupted_publication
    root = publication.parent
    tools = root / "tools"
    shutil.rmtree(tools)
    git(root / "repository", "worktree", "add", "--detach", str(tools), entry["landing_base"])
    with pytest.raises(codex_cloud_land.LandError, match="^unsafe-git-config$"):
        codex_cloud_land.land(caller, "task_example", 2473)
    assert (tools / "brigade").is_file()


@pytest.mark.parametrize(
    "key",
    [
        "CONTENT_GUARD_DIR",
        "CONTENT_GUARD_POLICY",
        "CONTENT_GUARD_EXTRA_POLICY",
        "CONTENT_GUARD_PRIVATE_POLICY",
        "CONTENT_GUARD_OPF_BIN",
        "BRIGADE_GUARD_BIN",
        "BRIGADE_GUARD_BACKEND",
        "BRIGADE_HOME",
        "BRIGADE_USER_DIR",
        "BRIGADE_EVIDENCE_BIN",
        "BRIGADE_CODE_GRAPH_BIN",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
    ],
)
def test_r2_guard_path_overrides_are_discarded(monkeypatch, key):
    monkeypatch.setenv(key, "compat")
    assert key not in codex_cloud_land._environment()


@pytest.mark.parametrize("override", ["compat", "native"])
def test_r2_relative_guard_override_never_executes(landing, tmp_path, monkeypatch, override):
    caller, state, _ = landing
    marker = tmp_path / "executed"
    state["extra_files"] = {
        "compat/src/content_guard/__init__.py": (
            "import os\nfrom pathlib import Path\nPath(os.environ['LAND_TEST_MARKER']).touch()\n"
        ),
        "compat/src/content_guard/__main__.py": "raise SystemExit(0)\n",
        "compat/policies/public-repo.json": "{}\n",
        "compat/scanner": '#!/bin/sh\ntouch "$LAND_TEST_MARKER"\nexit 1\n',
    }
    state["executable_files"] = {"compat/scanner"}
    hook = caller.parent / "home" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexec brigade scrub --target . --policy public-repo --no-receipt\n")
    if override == "compat":
        monkeypatch.setenv("CONTENT_GUARD_DIR", "compat")
    else:
        scanner = caller / "compat" / "scanner"
        scanner.parent.mkdir()
        scanner.write_text(state["extra_files"]["compat/scanner"])
        scanner.chmod(0o700)
        monkeypatch.setenv("BRIGADE_GUARD_BACKEND", "native")
        monkeypatch.setenv("BRIGADE_GUARD_BIN", str(scanner))

    def during_push(cwd):
        environment = state["raw_calls"][-1][1]
        result = subprocess.run(
            [str(cwd.parent / "hooks" / "pre-push")],
            cwd=cwd,
            env={**environment, "LAND_TEST_MARKER": str(marker)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert not marker.exists()
        assert result.returncode == 0
        if override == "native":
            assert "BRIGADE_GUARD_BIN" not in environment
            assert environment["BRIGADE_GUARD_BACKEND"] == "python"

    state["during_push"] = during_push
    codex_cloud_land.land(caller, "task_example", 2473)
    assert not marker.exists()


@pytest.mark.parametrize("allow_marker", [False, True])
def test_r2_blocked_title_refused_before_commit(landing, capsys, allow_marker):
    caller, state, calls = landing
    blocked = ".".join(map(str, (10, 42, 7, 9)))
    suffix = " # content-guard: allow all" if allow_marker else ""
    state["status"] = f"[READY] fix: contact {blocked}{suffix}"
    with pytest.raises(codex_cloud_land.LandError, match="^title-refused$") as error:
        codex_cloud_land.land(caller, "task_example", 2473)
    assert not any(argv[:2] in (["git", "commit"], ["git", "push"]) for argv, _ in calls)
    assert not any(argv[:3] == ["gh", "pr", "create"] for argv, _ in calls)
    captured = capsys.readouterr()
    assert blocked not in captured.out + captured.err + str(error.value)
    assert "landing_title" not in cloud_tracker.load_registry(caller)["entries"][0]


def test_r2_private_policy_applies_to_title(landing):
    caller, state, calls = landing
    policy = caller.parent / "home" / ".config" / "content-guard" / "internal.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({"known_hosts": ["guard-only.example.invalid"]}))
    state["status"] = "[READY] fix: contact guard-only.example.invalid"
    with pytest.raises(codex_cloud_land.LandError, match="^title-refused$"):
        codex_cloud_land.land(caller, "task_example", 2473)
    assert not any(argv[:2] == ["git", "commit"] for argv, _ in calls)


def test_r2_all_publication_text_is_scanned(landing):
    caller, state, _ = landing
    codex_cloud_land.land(caller, "task_example", 2473)
    scanned = [text for _, text in state["text_scans"]]
    assert state["title"] in scanned
    assert state["commit_message"] in scanned
    assert state["body"] in scanned
    push_environment = next(environment for argv, environment in state["raw_calls"] if "push" in argv)
    assert all(
        argv[argv.index("--policy") + 1] == push_environment["CONTENT_GUARD_POLICY"] for argv, _ in state["text_scans"]
    )


def test_r2_retry_refuses_unsafe_saved_title(landing):
    caller, state, calls = landing
    state["failure"] = "push"
    with pytest.raises(codex_cloud_land.LandError, match="^push-failed:network$"):
        codex_cloud_land.land(caller, "task_example", 2473)
    with cloud_tracker.registry_lock(caller):
        registry = cloud_tracker.load_registry(caller)
        registry["entries"][0]["landing_title"] = "fix: contact " + ".".join(map(str, (10, 42, 7, 9)))
        cloud_tracker.save_registry(caller, registry)
    calls.clear()
    state["failure"] = None
    with pytest.raises(codex_cloud_land.LandError, match="^title-refused$"):
        codex_cloud_land.land(caller, "task_example", 2473)
    assert not any(argv[:2] == ["git", "push"] for argv, _ in calls)
    assert not any(argv[:3] == ["gh", "pr", "create"] for argv, _ in calls)


@pytest.mark.parametrize("kind", ["relative", "proposal"])
def test_r2_guard_home_must_be_external_and_absolute(landing, monkeypatch, kind):
    caller, _, calls = landing
    set_home(monkeypatch, Path("compat") if kind == "relative" else caller)
    with pytest.raises(codex_cloud_land.LandError, match="^unsafe-git-config$"):
        codex_cloud_land.land(caller, "task_example", 2473)
    assert not calls


def test_r2_trusted_tool_symlink_cannot_enter_private_checkout(interrupted_publication, tmp_path, monkeypatch):
    caller, _, _, _, publication = interrupted_publication
    marker = tmp_path / "executed"
    probe = publication / "tool-probe"
    probe.write_text('#!/bin/sh\ntouch "$LAND_TEST_MARKER"\n')
    probe.chmod(0o700)
    alias = tmp_path / "operator-tools" / "vale"
    alias.parent.mkdir()
    alias.symlink_to(probe)
    previous = shutil.which

    def which(name, **kwargs):
        return str(alias) if name == "vale" else previous(name, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setenv("LAND_TEST_MARKER", str(marker))
    try:
        with pytest.raises(codex_cloud_land.LandError, match="^trusted-tool-unavailable$"):
            codex_cloud_land.land(caller, "task_example", 2473)
    finally:
        assert not marker.exists()


def test_r2_exported_guard_function_never_executes(landing, monkeypatch):
    caller, state, _ = landing
    state["extra_files"] = {"compat/probe.py": "from pathlib import Path\nPath('guard-executed').touch()\n"}
    hook = caller.parent / "home" / "hooks" / "pre-push"
    hook.write_text("#!/usr/bin/env bash\nbrigade scrub --target . --policy public-repo --no-receipt\n")
    monkeypatch.setenv("BASH_FUNC_brigade%%", "() { python3 compat/probe.py; }")

    def during_push(cwd):
        result = subprocess.run(
            [str(cwd.parent / "hooks" / "pre-push")],
            cwd=cwd,
            env=state["raw_calls"][-1][1],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert not (cwd / "guard-executed").exists()
        assert result.returncode == 0

    state["during_push"] = during_push
    codex_cloud_land.land(caller, "task_example", 2473)
