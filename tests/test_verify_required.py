from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from pathlib import Path

import pytest

from brigade import cli, verify_manifest_git
from brigade.work_cmd import verify_gate, verify_required


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "--all")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def write(repo: Path, name: str, text: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def configure(repo: Path, **overrides) -> None:
    config = {
        "schema": "brigade.verify_required_config.v1",
        "planner_files": ["planner.py"],
        "planner_argv": [
            "{python}",
            "{planner_dir}/planner.py",
            "--base",
            "{base}",
            "--head",
            "{head}",
            "--output",
            "{output}",
            "--repo",
            "{repo}",
            "--planner-dir",
            "{planner_dir}",
        ],
        "mode_key": "mode",
        "selected_key": "selected",
        "digest_key": "selector_digest",
        "run_argv": ["focused", "{tests}"],
        **overrides,
    }
    write(repo, verify_required.CONFIG_PATH, json.dumps(config))


def planner(repo: Path, body: str) -> None:
    write(
        repo,
        "planner.py",
        "import json, pathlib, sys, time\n"
        "arg = lambda name: sys.argv[sys.argv.index('--' + name) + 1]\n"
        "out = pathlib.Path(arg('output'))\n"
        "assert pathlib.Path.cwd() == pathlib.Path(__file__).resolve().parent\n"
        "assert pathlib.Path(arg('planner-dir')) == pathlib.Path.cwd()\n"
        "assert (pathlib.Path(arg('repo')) / 'tests' / 'a.py').is_file()\n" + body + "\n",
    )


def plan(repo: Path, selected, *, mode="selective", **extra) -> None:
    payload = dict(mode=mode, selected=selected, selector_digest="self-reported", **extra)
    planner(repo, f"out.write_text(json.dumps({payload!r}))")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    target = tmp_path / "repository with spaces"
    target.mkdir()
    git(target, "init", "-q", "-b", "main")
    git(target, "config", "user.email", "test@example.invalid")
    git(target, "config", "user.name", "Test User")
    git(target, "config", "core.autocrlf", "false")
    write(target, ".gitignore", "/.brigade/\n")
    write(target, "tests/a.py", "")
    write(target, "tests/z.py", "")
    configure(target)
    plan(target, ["tests/z.py", "tests/a.py"])
    base = commit(target, "base")
    write(target, "change.py", "changed = True\n")
    commit(target, "head")
    return target, base


def pin_base(target: Path, base: str) -> None:
    git(target, "remote", "add", "origin", "unused.git")
    git(target, "update-ref", "refs/remotes/origin/main", base)


def run(target, capsys, *, status=0, human=False):
    args = ["work", "verify", "required", "--target", str(target)]
    if not human:
        args.append("--json")
    assert cli.main(args) == status
    output = capsys.readouterr().out
    if human:
        return output
    result = json.loads(output)
    if result["record_path"]:
        assert json.loads(Path(result["record_path"]).read_text(encoding="utf-8")) == result
    return result


def error(target, capsys):
    result = run(target, capsys, status=1)
    assert result["mode"] == "error"
    assert result["local_requirement"] == "ci"
    assert result["tests"] == []
    return result


def test_selective_plan_sorts_tests_prints_command_and_writes_record(repo, capsys):
    target, base = repo
    pin_base(target, base)
    result = run(target, capsys)
    assert result["tests"] == ["tests/a.py", "tests/z.py"]
    assert result["run_argv"] == ["focused", "tests/a.py", "tests/z.py"]
    assert result["local_requirement"] == "tests"
    assert result["covers_worktree"] is True
    assert result["base_source"] == "default_branch"
    assert result["base_trusted"] is True
    assert Path(result["record_path"]).name == f"{result['head_sha']}.{base[:12]}.json"
    assert "focused tests/a.py tests/z.py" in run(target, capsys, human=True)


def test_full_plan_requires_ci_and_keeps_planner_reasons(repo, capsys):
    target, _ = repo
    reasons = [{"path": "suite.ini", "reason": "global change"}]
    plan(target, ["tests/a.py"], mode="full", full_triggers=reasons)
    commit(target, "full planner")
    result = run(target, capsys)
    assert result["mode"] == "full"
    assert result["local_requirement"] == "ci"
    assert result["tests"] == result["run_argv"] == []
    assert result["reasons"] == reasons
    output = run(target, capsys, human=True)
    assert "Required CI on head commit" in output
    assert "focused" not in output


@pytest.mark.parametrize("committed", [False, True])
def test_base_planner_and_its_import_are_pinned(repo, capsys, monkeypatch, committed):
    target, _ = repo
    configure(target, planner_files=["planner.py", "helper.py"])
    write(target, "helper.py", "selected = ['tests/a.py']\n")
    planner(
        target, "from helper import selected\nout.write_text(json.dumps(dict(mode='selective', selected=selected)))"
    )
    pinned = [(name, (target / name).read_bytes()) for name in ["helper.py", "planner.py"]]
    pin_base(target, commit(target, "pin planner"))
    write(target, "helper.py", "selected = ['tests/z.py']\n")
    plan(target, ["tests/z.py"])
    # A caller's PYTHONPATH must not replace the base import or load checkout startup code.
    write(target, "sitecustomize.py", "raise SystemExit('unexpected startup code')\n")
    monkeypatch.setenv("PYTHONPATH", str(target))
    if committed:
        commit(target, "change planner")
    result = run(target, capsys)
    assert result["tests"] == ["tests/a.py"]
    digest = hashlib.sha256()
    for name, blob in pinned:
        digest.update(name.encode() + b"\0" + str(len(blob)).encode() + b"\0" + blob)
    assert result["planner_digest"] == digest.hexdigest()
    assert result["covers_worktree"] is committed


def test_worktree_config_change_cannot_replace_base_config(repo, capsys):
    target, _ = repo
    configure(target, planner_files=["missing.py"])
    assert run(target, capsys)["tests"] == ["tests/a.py", "tests/z.py"]


def test_missing_base_config_is_unconfigured(repo, capsys):
    target, _ = repo
    (target / verify_required.CONFIG_PATH).unlink()
    pin_base(target, commit(target, "remove config"))
    configure(target)
    commit(target, "configure HEAD")
    result = run(target, capsys, status=3)
    assert result["mode"] == "unconfigured"
    assert result["local_requirement"] == "unknown"


@pytest.mark.parametrize("raw", [b"{", b"\xff", b'{"invalid": NaN}'])
def test_invalid_base_config_is_error(repo, capsys, raw):
    target, _ = repo
    (target / verify_required.CONFIG_PATH).write_bytes(raw)
    commit(target, "broken config")
    assert "UTF-8 JSON" in error(target, capsys)["reason"]


@pytest.mark.parametrize(("command", "timeout"), [("ls-tree", True), ("cat-file", False)])
def test_git_read_failures_cannot_look_unconfigured(repo, capsys, monkeypatch, command, timeout):
    target, _ = repo
    run_git = subprocess.run

    def fail_read(argv, *args, **kwargs):
        if argv[:4] == ["git", "--no-replace-objects", "--literal-pathspecs", command]:
            if timeout:
                raise subprocess.TimeoutExpired(argv, timeout=30)
            return subprocess.CompletedProcess(argv, 128, b"", b"")
        return run_git(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_read)
    reason = error(target, capsys)["reason"]
    assert reason == (f"git {command} timed out" if timeout else f"git {command} failed (exit 128)")


@pytest.mark.parametrize("path", [verify_required.CONFIG_PATH, "planner.py", "tests/a.py"])
def test_nonregular_tree_entries_are_errors(repo, capsys, path):
    target, _ = repo
    # Put a symlink into the Git tree without requiring Windows symlink privileges.
    oid = git(target, "hash-object", "-w", "change.py")
    git(target, "update-index", "--cacheinfo", f"120000,{oid},{path}")
    git(target, "commit", "-qm", "record symlink")
    assert "regular file" in error(target, capsys)["reason"]


def test_missing_planner_file_at_base(repo, capsys):
    target, _ = repo
    configure(target, planner_files=["planner.py", "missing.py"])
    commit(target, "reference missing planner file")
    write(target, "missing.py", "")
    assert "missing.py" in error(target, capsys)["reason"]


@pytest.mark.parametrize(
    ("body", "timeout", "reason"),
    [
        ("raise SystemExit(7)", False, "status 7"),
        ("import missing_planner_dependency", False, "status 1"),
        ("time.sleep(1)", True, "timed out"),
        ("out.write_text('not json')", False, "UTF-8 JSON"),
        ("pass", False, "plan.json"),
    ],
)
def test_planner_failures_are_errors(repo, capsys, monkeypatch, body, timeout, reason):
    target, _ = repo
    planner(target, body)
    commit(target, "broken planner")
    if timeout:
        monkeypatch.setattr(verify_required, "PLANNER_TIMEOUT_SECONDS", 0.01)
    assert reason in error(target, capsys)["reason"]


@pytest.mark.parametrize(
    ("mode", "selected"),
    [
        ("selective", []),
        ("selective", {}),
        ("selective", "tests/a.py"),
        ("selective", [1]),
        ("other", ["tests/a.py"]),
        ("selective", ["--collect-only"]),
        ("selective", ["tests/../tests/a.py"]),
        ("selective", ["tests\\a.py"]),
        ("selective", ["C:/tests/a.py"]),
        ("selective", ["C:tests/a.py"]),
        ("selective", ["/tests/a.py"]),
        ("selective", ["tests"]),
        ("selective", ["tests/missing.py"]),
        ("selective", ["tests/a*.py"]),
        ("full", ["tests/missing.py"]),
    ],
)
def test_malformed_selections_fail_closed(repo, capsys, mode, selected):
    target, _ = repo
    plan(target, selected, mode=mode)
    commit(target, "configure malformed plan")
    error(target, capsys)


def test_selected_object_uses_keys(repo, capsys):
    target, _ = repo
    plan(target, {"tests/a.py": ["changed"]})
    commit(target, "use selection reasons")
    result = run(target, capsys)
    assert result["tests"] == ["tests/a.py"]
    assert result["planner_digest"] != "self-reported"


@pytest.mark.parametrize("fetch_default", [False, True])
def test_git_remote_requires_local_default_branch_ref(repo, capsys, tmp_path, fetch_default):
    target, base = repo
    remote = tmp_path / "bare.git"
    git(tmp_path, "init", "--bare", "-q", "-b", "main", str(remote))
    git(target, "remote", "add", "origin", str(remote))
    git(target, "push", "-q", "origin", f"{base}:refs/heads/main")
    if fetch_default:
        git(target, "fetch", "-q", "origin")
        result = run(target, capsys)
        assert result["base_source"] == "default_branch"
        assert result["base_trusted"] is True
        assert result["base"] == base != result["head_sha"]
    else:
        git(target, "update-ref", "-d", "refs/remotes/origin/main")
        result = error(target, capsys)
        assert "default branch is unresolved" in result["reason"]
        assert result["base_trusted"] is False


def test_no_remote_uses_head_as_base(repo, capsys):
    result = run(repo[0], capsys)
    assert result["base_source"] == "no_remote"
    assert result["base"] == result["head_sha"]
    assert result["base_trusted"] is False
    output = run(repo[0], capsys, human=True)
    assert (
        "The plan was computed from local HEAD because the repository has no remote. It is not an independent check."
        in output.splitlines()
    )


def test_cli_rejects_base_override(repo, capsys):
    target, base = repo
    args = ["work", "verify", "required", "-t", str(target), f"--base={base}"]
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 2
    assert "unrecognized arguments: --base=" in capsys.readouterr().err


@pytest.mark.parametrize("entry_point", ["required", "required_with_snapshot", "resolve_base"])
def test_git_subprocesses_use_target_environment(repo, capsys, monkeypatch, tmp_path, entry_point):
    target, _ = repo
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    if entry_point == "required_with_snapshot":
        config = json.loads((target / verify_required.CONFIG_PATH).read_text(encoding="utf-8"))
        config["planner_argv"].extend(["--head-dir", "{head_dir}"])
        configure(target, **config)
        commit(target, "configure committed source snapshot")
    head = git(target, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("Git_Config_Count", "0")
    caller_only = ("LD_PRELOAD", "PYTHONPATH", "PYTHONSTARTUP", "BRIGADE_TEST_EXTRA")
    if entry_point != "resolve_base":
        for name in caller_only:
            monkeypatch.setenv(name, "caller-value")
    original_run = subprocess.run
    planner_environments = []

    def check_environment(argv, *args, **kwargs):
        env = kwargs.get("env")
        assert env is not None
        assert "PATH" in env
        if entry_point != "resolve_base":
            assert all(name not in env for name in caller_only)
        git_keys = {key for key in env if key.upper().startswith("GIT_")}
        if argv[0] == "git":
            assert git_keys == ({"GIT_INDEX_FILE"} if argv[1] == "-C" else set())
        else:
            if entry_point == "required_with_snapshot":
                assert git_keys == {"GIT_DIR", "GIT_WORK_TREE", "GIT_NO_REPLACE_OBJECTS"}
                assert Path(env["GIT_DIR"]).resolve() == (target / ".git").resolve()
                assert Path(env["GIT_WORK_TREE"]) == kwargs["cwd"].parent / "head"
                assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
            else:
                assert git_keys == set()
            planner_environments.append(git_keys)
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", check_environment)
    if entry_point == "resolve_base":
        assert verify_manifest_git.resolve_base(target).commit == head
    else:
        result = run(target, capsys)
        assert result["head_sha"] == head
        assert result["tests"] == ["tests/a.py", "tests/z.py"]
        assert result["covers_worktree"] is True
        assert len(planner_environments) == 1


def test_ignored_untracked_files_are_outside_worktree_coverage(repo, capsys):
    target, _ = repo
    write(target, ".git/info/exclude", "/conftest.py\n")
    write(target, "conftest.py", "# Ignored test configuration\n")
    assert run(target, capsys)["covers_worktree"] is True


def test_subdirectory_target_is_error(repo, capsys):
    target, _ = repo
    result = error(target / "tests", capsys)
    assert "repository root" in result["reason"]
    assert Path(result["record_path"]).is_relative_to(target / ".brigade")


def test_invalid_target_prints_one_json_object(tmp_path, capsys):
    result = error(tmp_path, capsys)
    assert result["head_sha"] is None
    assert Path(result["record_path"]).name == "unknown.unknown.json"
    assert error(tmp_path / "missing", capsys)["record_path"] is None


def test_two_bases_write_two_records(repo, capsys):
    target, base = repo
    pin_base(target, base)
    first = run(target, capsys)
    git(target, "remote", "remove", "origin")
    second = run(target, capsys)
    assert first["record_path"] != second["record_path"]
    assert json.loads(Path(first["record_path"]).read_text(encoding="utf-8")) == first


def test_dirty_worktree_is_not_covered(repo, capsys):
    target, _ = repo
    write(target, "change.py", "dirty = True\n")
    assert run(target, capsys)["covers_worktree"] is False


def test_hint_quotes_arguments_for_bash(repo, capsys):
    target, _ = repo
    paths = ["tests/a file.py", "tests/x;echo.py"]
    for path in paths:
        write(target, path, "")
    plan(target, paths)
    commit(target, "add files with shell characters")
    output = run(target, capsys, human=True)
    assert "Bash" in output
    assert shlex.split(output.splitlines()[0]) == ["focused", *paths]


def test_test_filenames_are_not_pytest_argument_files(repo, capsys):
    target, _ = repo
    write(target, "@test_a.py", "")
    plan(target, ["@test_a.py"])
    commit(target, "add test with an at sign")
    result = run(target, capsys)
    assert result["tests"] == ["@test_a.py"]
    assert result["run_argv"] == ["focused", "./@test_a.py"]


@pytest.mark.parametrize("long_paths", [False, True])
def test_hint_limits_file_count_and_command_length(repo, capsys, long_paths):
    target, _ = repo
    # 93 letters keeps deep temporary paths under Windows limits; Windows CI remains unverified until CI runs.
    name = "a" * 93 if long_paths else "a"
    files = [f"tests/{name}{i:03}.py" for i in range(130 if long_paths else 200)]
    for path in files:
        write(target, path, "")
    plan(target, files)
    commit(target, "add large plan")
    output = run(target, capsys, human=True)
    command, *message = output.splitlines()
    shown = len(shlex.split(command)) - 1
    assert 0 < shown <= 40
    assert (shown < 40) is long_paths
    assert len(command) <= 4000
    assert f"{len(files) - shown} more" in " ".join(message)
    assert str(target / ".brigade/work/verify-required") in output
    assert run(target, capsys)["tests"] == files


def test_bundled_planner_reads_committed_sources(repo, capsys):
    target, _ = repo
    project = Path(__file__).resolve().parents[1]
    config_file = project / verify_required.CONFIG_PATH
    config = json.loads(config_file.read_text(encoding="utf-8"))
    write(target, verify_required.CONFIG_PATH, config_file.read_text(encoding="utf-8"))
    for name in config["planner_files"]:
        write(target, name, (project / name).read_text(encoding="utf-8"))
    write(target, "src/brigade/a.py", "value = 1\n")
    write(target, "tests/test_a.py", "from brigade import a\n")
    write(target, "tests/test_module_size_ratchet.py", "")
    pin_base(target, commit(target, "install bundled planner"))
    write(target, "src/brigade/a.py", "value = 2\n")
    commit(target, "change a module")
    write(target, "tests/test_a.py", "\n")
    result = run(target, capsys)
    assert result["tests"] == ["tests/test_a.py", "tests/test_module_size_ratchet.py"]
    assert result["covers_worktree"] is False


def test_repository_planner_template_satisfies_declared_coverage():
    project = Path(__file__).resolve().parents[1]
    policy = verify_gate._parse_declaration((project / "verify/required-check.json").read_bytes())
    assert policy.needs_plan_coverage is True
    config = json.loads((project / verify_required.CONFIG_PATH).read_text(encoding="utf-8"))
    files = ["tests/test_verify_gate.py", "tests/test_verify_required.py"]
    entries = {name: (b"100644", b"") for name in files if (project / name).is_file()}
    argv = verify_required._run_argv(config["run_argv"], files)
    prefix = list(policy.argv_prefix)
    assert argv[: len(prefix)] == prefix
    selectors = argv[len(prefix) :]
    assert selectors == ["--", *files] or selectors == files
    assert verify_gate._command_files(selectors, entries) == set(files)
