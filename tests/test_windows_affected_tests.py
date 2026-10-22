"""Selection contracts for the PR Windows lane."""

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
