"""Selection contracts for the PR Windows lane."""

import json
import re
import subprocess

import pytest

from scripts import windows_affected_tests as selector


def write(repo, name, text=""):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_added_modified_deleted_and_empty(tmp_path):
    write(tmp_path, "tests/test_added.py")
    write(tmp_path, "tests/nested/test_modified.py")
    write(tmp_path, "tests/test_unrelated.py")
    assert selector.select_tests(tmp_path, []) == []
    assert selector.select_tests(
        tmp_path, ["tests/test_added.py", "tests/nested/test_modified.py", "tests/test_deleted.py"]
    ) == ["nested/test_modified.py", "test_added.py"]


@pytest.mark.parametrize(
    "statement",
    [
        "import brigade.grokbot_t3.ledger as ledger",
        "from brigade.grokbot_t3.ledger import read",
        "from brigade.grokbot_t3 import ledger",
        "import brigade.grokbot_t3",
        "from brigade import grokbot_t3",
    ],
)
def test_module_imports_and_parent_packages(tmp_path, statement):
    write(tmp_path, "tests/test_match.py", statement)
    write(tmp_path, "tests/test_other.py", "import brigade.other")
    assert selector.select_tests(tmp_path, ["src/brigade/grokbot_t3/ledger.py"]) == ["test_match.py"]


def test_package_init_and_deleted_module_still_select_importers(tmp_path):
    write(tmp_path, "tests/test_match.py", "import brigade.example.child")
    assert selector.select_tests(tmp_path, ["src/brigade/example/__init__.py"]) == ["test_match.py"]
    assert selector.select_tests(tmp_path, ["src/brigade/example/child.py"]) == ["test_match.py"]


@pytest.mark.parametrize(
    "changed",
    [
        "tests/nested/conftest.py",
        "tests/support.py",
        "tests/fixtures/input.json",
        "src/brigade/assets/example.json",
        "pyproject.toml",
        "scripts/windows_pytest.py",
        "scripts/windows_pytest_diagnostics.py",
        "tests/windows_pytest_allowlist.json",
        "src/brigade/__init__.py",
        ".github/workflows/ci.yml",
    ],
)
def test_shared_infrastructure_adds_bounded_deterministic_sample(tmp_path, changed):
    for i in range(50):
        write(tmp_path, f"tests/test_{i:02}.py", "import brigade.example")
    selected = selector.select_tests(tmp_path, [changed])
    assert len(selected) == selector.BROAD_SAMPLE_SIZE
    assert selected == selector.select_tests(tmp_path, [changed])
    # Changed tests are never dropped to satisfy the broader sample cap.
    assert "test_49.py" in selector.select_tests(tmp_path, [changed, "tests/test_49.py"])


def test_git_diff_cli_uses_explicit_base_and_head(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q")
    git("config", "user.email", "contributor@example.com")
    git("config", "user.name", "Contributor")
    write(tmp_path, "tests/test_old.py")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    write(tmp_path, "tests/test_new.py")
    (tmp_path / "tests/test_old.py").unlink()
    git("add", "-A")
    git("commit", "-qm", "head")
    output = tmp_path / "selected.txt"
    assert selector.main(["--repo", str(tmp_path), "--base", base, "--head", "HEAD", "--output", str(output)]) == 0
    assert output.read_text() == "test_new.py\n"


def test_named_root_import_does_not_select_unrelated_siblings(tmp_path):
    write(tmp_path, "tests/test_cli.py", "from brigade import cli")
    write(tmp_path, "tests/test_parent.py", "import brigade.grokbot_t3")
    write(tmp_path, "tests/test_roster.py", "from brigade import roster")
    assert selector.select_tests(tmp_path, ["src/brigade/roster.py"]) == ["test_roster.py"]


def test_merge_first_parent_excludes_changes_already_on_the_base_branch(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "contributor@example.com")
    git("config", "user.name", "Contributor")
    write(tmp_path, "src/brigade/engine.py", "VALUE = 1\n")
    write(tmp_path, "tests/test_engine.py", "import brigade.engine\n")
    git("add", ".")
    git("commit", "-qm", "base")
    stale_base = git("rev-parse", "HEAD")
    git("switch", "-qc", "docs-only")
    write(tmp_path, "docs/notes.md", "notes\n")
    git("add", ".")
    git("commit", "-qm", "docs only")
    git("switch", "-q", "main")
    # Another pull request lands on main after this one was opened.
    write(tmp_path, "src/brigade/engine.py", "VALUE = 2\n")
    git("commit", "-qam", "engine change on main")
    # GitHub's test merge: first parent is the base tip, second is the PR head.
    git("merge", "-q", "--no-ff", "--no-edit", "docs-only")
    stale = tmp_path / "stale.txt"
    current = tmp_path / "current.txt"
    selector.main(["--repo", str(tmp_path), "--base", stale_base, "--head", "HEAD", "--output", str(stale)])
    selector.main(["--repo", str(tmp_path), "--base", "HEAD^1", "--head", "HEAD", "--output", str(current)])
    assert stale.read_text() == "test_engine.py\n"
    assert current.read_text() == ""


def test_driver_change_adds_the_flush_tests_that_exist(tmp_path):
    for name in (*selector.FLUSH_TESTS[:2], "test_unrelated.py"):
        write(tmp_path, f"tests/{name}")
    assert selector.select_tests(tmp_path, ["scripts/windows_pytest.py"]) == sorted(
        [*selector.FLUSH_TESTS[:2], "test_unrelated.py"]
    )
    assert selector.select_tests(tmp_path, ["tests/test_unrelated.py"]) == ["test_unrelated.py"]


def test_flush_tests_exist_in_the_real_suite():
    missing = [name for name in selector.FLUSH_TESTS if not (selector.Path("tests") / name).exists()]
    assert not missing


def test_serial_files_match_the_ci_driver_arguments():
    workflow = (selector.Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    section = workflow.split("  windows-affected-shard:", 1)[1].split("  windows-affected-tests:", 1)[0]
    assert set(re.findall(r"--serial (\S+)", section)) == selector.SERIAL_FILES


def test_plan_runs_every_selected_file_exactly_once_heaviest_first():
    names = [f"test_{i:03}.py" for i in range(120)]
    weights = {name: float(300 - i) for i, name in enumerate(names)}
    shards = selector.plan_shards(names, weights, 4.0)
    assert 1 < len(shards) <= selector.MAX_SHARDS
    assert sorted(name for shard in shards for name in shard) == names
    for shard in shards:
        assert shard == sorted(shard, key=lambda name: (-weights[name], name))
    loads = [sum(weights[name] for name in shard) for shard in shards]
    assert max(loads) < 1.1 * min(loads)


def test_plan_keeps_a_small_selection_on_one_runner_and_caps_a_large_one():
    small = [f"test_{i}.py" for i in range(20)]
    assert len(selector.plan_shards(small, {}, 4.0)) == 1
    assert selector.plan_shards([], {}, 4.0) == []
    huge = [f"test_{i}.py" for i in range(2000)]
    assert len(selector.plan_shards(huge, {}, 4.0)) == selector.MAX_SHARDS


def test_plan_never_creates_an_empty_shard_for_few_heavy_files():
    serial = sorted(selector.SERIAL_FILES)[0]
    for names, weights in (
        (["test_heavy.py"], {"test_heavy.py": 30000.0}),
        ([serial], {serial: 3000.0}),
        ([serial, "test_heavy.py"], {serial: 3000.0, "test_heavy.py": 30000.0}),
    ):
        shards = selector.plan_shards(names, weights, 4.0)
        assert all(shards), shards
        assert sorted(name for shard in shards for name in shard) == sorted(names)


def test_plan_counts_serial_files_as_wall_time():
    serial = sorted(selector.SERIAL_FILES)[0]
    names = [serial, *(f"test_{i:02}.py" for i in range(30))]
    shards = selector.plan_shards(names, {serial: 500.0}, 4.0)
    assert len(shards) == 2
    # The 500 second serial file outweighs all 30 light files combined.
    assert [serial] in shards


def test_load_weights_degrades_to_defaults(tmp_path):
    assert selector.load_weights(tmp_path / "missing.json") == ({}, 5.0)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert selector.load_weights(broken) == ({}, 5.0)
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"default_seconds": 3, "seconds": {"test_a.py": 40}}))
    assert selector.load_weights(good) == ({"test_a.py": 40.0}, 3.0)


def test_committed_weights_name_existing_tests():
    weights, default = selector.load_weights(selector.DEFAULT_WEIGHTS)
    assert weights and default > 0
    assert not [name for name in weights if not (selector.Path("tests") / name).exists()]


def test_plan_cli_writes_matrix_and_inert_entry_for_empty_selection(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q")
    git("config", "user.email", "contributor@example.com")
    git("config", "user.name", "Contributor")
    write(tmp_path, "tests/test_old.py")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    write(tmp_path, "tests/test_new.py")
    git("add", "-A")
    git("commit", "-qm", "head")
    plan = tmp_path / "plan.json"
    argv = ["--repo", str(tmp_path), "--base", base, "--output", str(tmp_path / "files.txt"), "--plan", str(plan)]
    assert selector.main(argv) == 0
    assert json.loads(plan.read_text()) == {
        "count": 1,
        "matrix": {"include": [{"shard": 1, "total": 1, "files": "test_new.py\n"}]},
    }
    assert selector.main([*argv[:2], "--base", "HEAD", *argv[4:]]) == 0
    assert json.loads(plan.read_text()) == {
        "count": 0,
        "matrix": {"include": [{"shard": 1, "total": 1, "files": ""}]},
    }


def test_weights_command_keeps_heavy_files_and_averages_the_rest(tmp_path):
    def record(name, rows):
        path = tmp_path / name
        path.write_text(json.dumps({"results": [{"name": n, "status": s, "seconds": t} for n, s, t in rows]}))
        return path

    one = record(
        "a.json", [("test_heavy.py", "passed", 100), ("test_light.py", "passed", 2), ("test_gone.py", "unstarted", 9)]
    )
    two = record("b.json", [("test_heavy.py", "failed", 200), ("test_light.py", "passed", 4)])
    output = tmp_path / "weights.json"
    assert selector.main(["weights", str(one), str(two), "--output", str(output)]) == 0
    assert json.loads(output.read_text()) == {
        "schema": "brigade.windows_test_weights.v1",
        "default_seconds": 3.0,
        "seconds": {"test_heavy.py": 150},
    }
