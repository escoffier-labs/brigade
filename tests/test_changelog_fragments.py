"""Tests for scripts/changelog_fragments.py (#2231)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("changelog_fragments_test", ROOT / "scripts/changelog_fragments.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cf = _load()

CHANGELOG = (
    "# Changelog\n\nIntro.\n\n## [Unreleased]\n\n- Existing one.\n- Existing two.\n\n"
    "## [1.0.0] - 2026-01-01\n\n### Added\n\n- Old entry.\n"
)


def _write(root: Path, name: str, text: str) -> None:
    (root / "changelog.d").mkdir(exist_ok=True)
    (root / "changelog.d" / name).write_text(text, encoding="utf-8")


def _repo(tmp_path: Path, changelog: str = CHANGELOG) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return tmp_path


def test_parse_accepts_bullets_wrapped_lines_and_blank_separators():
    text = "- First entry.\n\n- Second entry\n  wraps onto a second line.\n- Third.\n"
    assert cf.parse_fragment(text) == ["- First entry.", "- Second entry\n  wraps onto a second line.", "- Third."]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n\n  \n",
        "# Heading\n- entry\n",
        "Some prose.\n",
        "-\n",
        "- \n",
        "  indented first\n- entry\n",
        "-no space\n",
    ],
)
def test_parse_rejects_malformed_fragments(text):
    with pytest.raises(cf.FragmentError):
        cf.parse_fragment(text)


def test_natural_order_sorts_pr_numbers_numerically():
    names = ["2231.md", "99.md", "b10.md", "b2.md", "alpha.md"]
    assert sorted(names, key=cf.natural_key) == ["99.md", "2231.md", "alpha.md", "b2.md", "b10.md"]


@pytest.mark.parametrize("name", ["aux.md", "CON.md", "nul.md", "com1.md", "LPT9.md", "prn.v2.md"])
def test_validate_names_rejects_windows_reserved_device_names(name):
    # Upper case also fails the lowercase-only name rule, so look for the reserved-name problem.
    assert any("Windows reserved device name" in problem for problem in cf.validate_names([name]))


@pytest.mark.parametrize("name", ["com10.md", "console.md", "auxiliary.md", "lpt0.md", "my-con.md"])
def test_validate_names_allows_names_that_only_resemble_device_names(name):
    assert cf.validate_names([name]) == []


def test_validate_names_rejects_bad_and_case_duplicate_names():
    assert cf.validate_names(["2231.md", "fix-thing.md", "a.b_c.md"]) == []
    problems = cf.validate_names(["Upper.md", "note.txt", "-dash.md", "dup.md", "DUP.md"])
    assert len([p for p in problems if "name must match" in p]) == 4
    assert any("duplicates" in p for p in problems)


def test_load_fragments_ignores_readme_and_reports_every_problem(tmp_path):
    _write(tmp_path, "README.md", "free-form docs\n")
    _write(tmp_path, ".gitkeep", "")
    _write(tmp_path, "1.md", "- One.\n")
    _write(tmp_path, "2.md", "")
    _write(tmp_path, "3.md", "- One.\n")
    _write(tmp_path, "BadName.md", "- Fine.\n")
    (tmp_path / "changelog.d" / "sub").mkdir()
    fragments, problems = cf.load_fragments(tmp_path)
    assert [name for name, _ in fragments] == ["1.md", "3.md"]
    joined = "\n".join(problems)
    assert "2.md: no entries" in joined
    assert "3.md: repeats an entry already in changelog.d/1.md" in joined
    assert "BadName.md: name must match" in joined
    assert "sub: must be a regular file" in joined


def test_load_fragments_without_directory_is_empty(tmp_path):
    assert cf.load_fragments(tmp_path) == ([], [])


def test_insert_appends_to_end_of_unreleased_and_preserves_the_rest():
    updated = cf.insert_into_unreleased(CHANGELOG, ["- New A.", "- New B\n  wrapped."])
    assert updated == CHANGELOG.replace(
        "- Existing two.\n\n## [1.0.0]", "- Existing two.\n\n- New A.\n- New B\n  wrapped.\n\n## [1.0.0]"
    )


def test_insert_into_empty_unreleased_and_unreleased_at_end_of_file():
    empty = "# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - 2026-01-01\n\n- Old.\n"
    assert cf.insert_into_unreleased(empty, ["- New."]) == (
        "# Changelog\n\n## [Unreleased]\n\n- New.\n\n## [1.0.0] - 2026-01-01\n\n- Old.\n"
    )
    assert cf.insert_into_unreleased("# Changelog\n\n## [Unreleased]\n", ["- New."]) == (
        "# Changelog\n\n## [Unreleased]\n\n- New.\n"
    )


def test_insert_keeps_crlf_line_endings():
    crlf = CHANGELOG.replace("\n", "\r\n")
    updated = cf.insert_into_unreleased(crlf, ["- New."])
    assert "\r\n- New.\r\n" in updated
    assert "\n" not in updated.replace("\r\n", "")


@pytest.mark.parametrize("changelog", ["# Changelog\n", "## [Unreleased]\n\n## [Unreleased]\n"])
def test_insert_requires_exactly_one_unreleased_heading(changelog):
    with pytest.raises(cf.FragmentError, match="exactly one"):
        cf.insert_into_unreleased(changelog, ["- New."])


def test_assemble_round_trip_orders_fragments_and_deletes_them(tmp_path):
    _repo(tmp_path)
    _write(tmp_path, "README.md", "docs\n")
    _write(tmp_path, "2231.md", "- Later PR.\n")
    _write(tmp_path, "99.md", "- Earlier PR one.\n- Earlier PR two.\n")
    count, problems = cf.assemble(tmp_path)
    assert (count, problems) == (2, [])
    updated = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "- Existing two.\n\n- Earlier PR one.\n- Earlier PR two.\n- Later PR.\n\n## [1.0.0]" in updated
    # Everything outside the Unreleased section is byte-identical.
    assert updated.split("## [1.0.0]")[1] == CHANGELOG.split("## [1.0.0]")[1]
    assert sorted(path.name for path in (tmp_path / "changelog.d").iterdir()) == ["README.md"]
    assert cf.assemble(tmp_path) == (0, [])
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == updated


def test_assemble_preserves_crlf_changelogs(tmp_path):
    (tmp_path / "CHANGELOG.md").write_bytes(CHANGELOG.replace("\n", "\r\n").encode())
    _write(tmp_path, "1.md", "- Good.\n")
    assert cf.assemble(tmp_path) == (1, [])
    data = (tmp_path / "CHANGELOG.md").read_bytes()
    assert b"- Good.\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b"")


def test_assemble_dry_run_and_invalid_fragments_change_nothing(tmp_path):
    _repo(tmp_path)
    _write(tmp_path, "1.md", "- Good.\n")
    assert cf.assemble(tmp_path, dry_run=True) == (1, [])
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == CHANGELOG
    assert (tmp_path / "changelog.d" / "1.md").exists()
    _write(tmp_path, "2.md", "not a bullet\n")
    count, problems = cf.assemble(tmp_path)
    assert count == 0 and problems
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == CHANGELOG
    assert (tmp_path / "changelog.d" / "1.md").exists()


def test_assemble_reports_a_changelog_without_unreleased_heading(tmp_path):
    _repo(tmp_path, "# Changelog\n")
    _write(tmp_path, "1.md", "- Good.\n")
    count, problems = cf.assemble(tmp_path)
    assert count == 0 and "exactly one" in problems[0]
    assert (tmp_path / "changelog.d" / "1.md").exists()


def _git_text(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _git(root: Path, *args: str) -> None:
    _git_text(root, *args)


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)


@pytest.fixture
def git_repo(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _repo(tmp_path)
    _write(tmp_path, "README.md", "docs\n")
    (tmp_path / "src.py").write_text("x = 1\n")
    _commit(tmp_path, "base")
    return tmp_path


def test_guard_allows_code_and_fragment_only_changes(git_repo):
    _write(git_repo, "5.md", "- Entry.\n")
    (git_repo / "src.py").write_text("x = 2\n")
    _commit(git_repo, "feature")
    assert cf.guard(git_repo, "HEAD~1") == []


def test_guard_rejects_a_changelog_edit_outside_a_release_commit(git_repo):
    (git_repo / "CHANGELOG.md").write_text(CHANGELOG.replace("- Existing two.\n", "- Existing two.\n- Sneaky.\n"))
    _commit(git_repo, "feature")
    problems = cf.guard(git_repo, "HEAD~1")
    assert len(problems) == 1
    assert "changelog.d/<pr-number-or-slug>.md" in problems[0]


def test_guard_rejects_a_changelog_edit_that_only_adds_a_non_release_heading(git_repo):
    (git_repo / "CHANGELOG.md").write_text(CHANGELOG + "\n## [Notes]\n\n- Not a release.\n")
    _commit(git_repo, "feature")
    assert len(cf.guard(git_repo, "HEAD~1")) == 1


RELEASED = CHANGELOG.replace(
    "## [Unreleased]\n\n- Existing one.\n- Existing two.\n",
    "## [Unreleased]\n\n## [1.1.0] - 2026-02-01\n\n- Existing one.\n",
)
RELEASE_HEADING = "## [1.1.0] - 2026-02-01"


def test_guard_accepts_a_release_commit_that_deleted_every_fragment(git_repo):
    _write(git_repo, "5.md", "- Pending.\n")
    _commit(git_repo, "pending fragment")
    cf.assemble(git_repo)
    text = (git_repo / "CHANGELOG.md").read_text()
    (git_repo / "CHANGELOG.md").write_text(text.replace("## [1.0.0]", "## [1.1.0] - 2026-02-01\n\n## [1.0.0]", 1))
    _commit(git_repo, "release")
    assert cf.guard(git_repo, "HEAD~1") == []


def test_guard_rejects_a_release_commit_that_leaves_a_fragment(git_repo):
    _write(git_repo, "5.md", "- Pending.\n")
    (git_repo / "CHANGELOG.md").write_text(RELEASED)
    _commit(git_repo, "release")
    problems = cf.guard(git_repo, "HEAD~1")
    assert problems and "changelog.d/5.md" in problems[0]
    assert "assemble" in problems[0]


def test_guard_rejects_a_release_commit_that_leaves_a_preexisting_fragment(git_repo):
    # Only a merge of the base relaxes the check; a release commit that leaves
    # a fragment the base branch already had still fails.
    _write(git_repo, "5.md", "- Pending.\n")
    _commit(git_repo, "pending fragment")
    (git_repo / "CHANGELOG.md").write_text(RELEASED)
    _commit(git_repo, "release")
    problems = cf.guard(git_repo, "HEAD~1")
    assert len(problems) == 1
    assert "changelog.d/5.md" in problems[0]


def _test_merge(origin: Path, base: str, head: str) -> None:
    """Create the pull request test merge GitHub checks out, parents [base, head]."""
    _git(origin, "checkout", "-q", "--detach", base)
    _git(origin, "merge", "--no-ff", "--no-edit", head)
    _git(origin, "update-ref", "refs/pull/1/merge", "HEAD")
    _git(origin, "checkout", "-q", "main")


def _shallow_ci_checkout(origin: Path, destination: Path) -> Path:
    """Fetch the test merge as actions/checkout does with fetch-depth: 2."""
    destination.mkdir()
    _git(destination, "init", "-q")
    _git(destination, "remote", "add", "origin", origin.resolve().as_uri())
    _git(destination, "fetch", "-q", "--depth=2", "origin", "+refs/pull/1/merge:refs/remotes/pull/1/merge")
    _git(destination, "checkout", "-q", "--detach", "refs/remotes/pull/1/merge")
    return destination


def _fetch_head_parents(clone: Path, head: str) -> list[str]:
    """Fetch the change head's parents as the changelog-fragments job does."""
    lines = _git_text(clone, "cat-file", "-p", head).splitlines()
    parents = [line.split()[1] for line in lines if line.startswith("parent ")]
    for parent in parents:
        _git(clone, "fetch", "-q", "--no-tags", "--depth=1", "origin", parent)
    return parents


def _origin_with_pull_request(tmp_path: Path, edit, *, prepare=None) -> tuple[Path, str, str]:
    """A base commit on main and a feature branch that applies ``edit``."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    # The job fetches parents by object id; GitHub serves reachable objects.
    _git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    _repo(origin)
    _write(origin, "README.md", "docs\n")
    if prepare is not None:
        prepare(origin)
    _commit(origin, "base")
    base = _git_text(origin, "rev-parse", "HEAD").strip()
    _git(origin, "checkout", "-q", "-b", "feature")
    edit(origin)
    _commit(origin, "feature")
    head = _git_text(origin, "rev-parse", "HEAD").strip()
    return origin, base, head


def _origin_with_released_main_and_integration_branch(
    tmp_path: Path, *, post_release_fragment: str | None = None, advance_base: bool = False
) -> tuple[Path, str, str]:
    """main with a release, and an integration branch that merges that release.

    Returns ``(origin, base, head)``: ``base`` is the branch tip GitHub merges
    into and ``head`` is the pull request head, a merge of the released main.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    _repo(origin)
    _write(origin, "README.md", "docs\n")
    _write(origin, "10.md", "- Released entry.\n")
    (origin / "src.py").write_text("x = 1\n")
    _commit(origin, "base")
    seed = _git_text(origin, "rev-parse", "HEAD").strip()
    (origin / "changelog.d" / "10.md").unlink()
    (origin / "CHANGELOG.md").write_text(RELEASED)
    _commit(origin, "release")
    if post_release_fragment:
        _write(origin, post_release_fragment, "- After the release.\n")
        _commit(origin, "post release")
    _git(origin, "checkout", "-q", "-b", "overhaul", seed)
    _write(origin, "overhaul-1.md", "- Branch entry.\n")
    (origin / "feature.py").write_text("y = 1\n")
    _commit(origin, "overhaul work")
    _git(origin, "merge", "--no-edit", "main")
    head = _git_text(origin, "rev-parse", "HEAD").strip()
    base = _git_text(origin, "rev-parse", "HEAD^1").strip()
    if advance_base:
        # A sibling commit on the branch tip, as when the base moves without
        # taking the pull request head.
        _git(origin, "checkout", "-q", "--detach", base)
        _write(origin, "30.md", "- Base advanced.\n")
        _commit(origin, "advance base")
        base = _git_text(origin, "rev-parse", "HEAD").strip()
    return origin, base, head


def test_guard_rejects_a_release_commit_with_a_preexisting_fragment_at_ci_checkout_depth(tmp_path):
    # This is the regression: the guard used to relax just because the test
    # merge's first parent is the base, so any release PR passed at CI depth.
    def prepare(origin: Path) -> None:
        _write(origin, "5.md", "- Pending.\n")

    def edit(origin: Path) -> None:
        (origin / "CHANGELOG.md").write_text(RELEASED)

    origin, base, head = _origin_with_pull_request(tmp_path, edit, prepare=prepare)
    _test_merge(origin, base, head)
    clone = _shallow_ci_checkout(origin, tmp_path / "clone")
    _fetch_head_parents(clone, head)
    # Neither the pre-correction invocation handed the synthetic test merge nor
    # the corrected one that names the pull request head may relax here.
    for arguments in (("HEAD^1",), ("HEAD^1", "HEAD^2")):
        problems = cf.guard(clone, *arguments)
        assert len(problems) == 1
        assert "changelog.d/5.md" in problems[0]


def test_guard_accepts_an_integration_merge_only_with_upstream_evidence_at_ci_checkout_depth(tmp_path):
    origin, base, head = _origin_with_released_main_and_integration_branch(tmp_path)
    _test_merge(origin, base, head)
    clone = _shallow_ci_checkout(origin, tmp_path / "clone")
    # Fail closed: until the job fetches the head's parents, the release cannot
    # be attributed to an upstream, so the base branch's fragment still fails.
    problems = cf.guard(clone, "HEAD^1", "HEAD^2")
    assert problems and "changelog.d/overhaul-1.md" in problems[0]
    parents = _fetch_head_parents(clone, head)
    assert base in parents
    upstream = next(parent for parent in parents if parent != base)
    assert RELEASE_HEADING in _git_text(clone, "show", f"{upstream}:CHANGELOG.md")
    assert cf.guard(clone, "HEAD^1", "HEAD^2") == []


def test_guard_rejects_a_direct_changelog_edit_at_ci_checkout_depth(tmp_path):
    def edit(origin: Path) -> None:
        changelog = origin / "CHANGELOG.md"
        changelog.write_text(changelog.read_text() + "\n## [Notes]\n\n- Not a release.\n")

    origin, base, head = _origin_with_pull_request(tmp_path, edit)
    _test_merge(origin, base, head)
    clone = _shallow_ci_checkout(origin, tmp_path / "clone")
    problems = cf.guard(clone, "HEAD^1", "HEAD^2")
    assert len(problems) == 1
    assert "changelog.d/<pr-number-or-slug>.md" in problems[0]


def test_guard_rejects_a_dirty_release_commit_at_ci_checkout_depth(tmp_path):
    def edit(origin: Path) -> None:
        (origin / "CHANGELOG.md").write_text(RELEASED)
        _write(origin, "9.md", "- Never assembled.\n")

    origin, base, head = _origin_with_pull_request(tmp_path, edit)
    _test_merge(origin, base, head)
    clone = _shallow_ci_checkout(origin, tmp_path / "clone")
    problems = cf.guard(clone, "HEAD^1", "HEAD^2")
    assert len(problems) == 1
    assert "changelog.d/9.md" in problems[0]


def test_guard_covers_upstream_post_release_fragments_at_ci_checkout_depth(tmp_path):
    origin, base, head = _origin_with_released_main_and_integration_branch(tmp_path, post_release_fragment="20.md")
    _test_merge(origin, base, head)
    clone = _shallow_ci_checkout(origin, tmp_path / "clone")
    _fetch_head_parents(clone, head)
    # overhaul-1.md came from the base branch and 20.md from the merged
    # upstream; neither is this change's fragment.
    assert cf.guard(clone, "HEAD^1", "HEAD^2") == []


def test_guard_covers_an_advanced_base_at_ci_checkout_depth(tmp_path):
    origin, base, head = _origin_with_released_main_and_integration_branch(tmp_path, advance_base=True)
    _test_merge(origin, base, head)
    clone = _shallow_ci_checkout(origin, tmp_path / "clone")
    # Without the fetched parents the advanced base keeps the strict rule.
    assert cf.guard(clone, "HEAD^1", "HEAD^2")
    _fetch_head_parents(clone, head)
    assert cf.guard(clone, "HEAD^1", "HEAD^2") == []


def test_guard_keeps_the_strict_rule_for_a_merge_group_shape(tmp_path):
    origin, base, head = _origin_with_released_main_and_integration_branch(tmp_path)
    _test_merge(origin, base, head)
    clone = _shallow_ci_checkout(origin, tmp_path / "clone")
    # Merge groups pass no change head, so the release is never attributed.
    problems = cf.guard(clone, "HEAD^1")
    assert problems and "changelog.d/overhaul-1.md" in problems[0]


def test_guard_rejects_a_fragment_added_by_a_merge_of_a_released_main(tmp_path):
    origin, base, _head = _origin_with_released_main_and_integration_branch(tmp_path)
    # The branch keeps its own fragment but also adds one in the merge commit.
    _git(origin, "checkout", "-q", "--detach", base)
    _git(origin, "merge", "--no-commit", "main")
    _write(origin, "extra.md", "- Added by the merge.\n")
    _commit(origin, "merge main")
    head = _git_text(origin, "rev-parse", "HEAD").strip()
    _test_merge(origin, base, head)
    clone = _shallow_ci_checkout(origin, tmp_path / "clone")
    _fetch_head_parents(clone, head)
    problems = cf.guard(clone, "HEAD^1", "HEAD^2")
    # The fragment that was already on the base branch is not the change's.
    assert len(problems) == 1
    assert "changelog.d/extra.md" in problems[0]


def test_guard_with_an_unknown_base_is_an_operational_error(git_repo, capsys):
    with pytest.raises(cf.FragmentError):
        cf.guard(git_repo, "no-such-revision")
    assert cf.main(["--root", str(git_repo), "guard", "--base", "no-such-revision"]) == 2
    assert "error:" in capsys.readouterr().err


def test_cli_check_assemble_and_exit_codes(tmp_path, capsys, monkeypatch):
    # Under GitHub Actions problems are printed as stdout annotations instead.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    _repo(tmp_path)
    assert cf.main(["--root", str(tmp_path), "check"]) == 0
    _write(tmp_path, "1.md", "- Good.\n")
    assert cf.main(["--root", str(tmp_path), "assemble", "--dry-run"]) == 0
    assert "would assemble 1" in capsys.readouterr().out
    assert cf.main(["--root", str(tmp_path), "assemble"]) == 0
    assert not (tmp_path / "changelog.d" / "1.md").exists()
    _write(tmp_path, "2.md", "oops\n")
    assert cf.main(["--root", str(tmp_path), "check"]) == 1
    assert "2.md" in capsys.readouterr().err


def test_cli_annotates_problems_under_github_actions(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _write(tmp_path, "2.md", "oops\n")
    assert cf.main(["--root", str(tmp_path), "check"]) == 1
    assert "::error file=changelog.d/2.md::" in capsys.readouterr().out


def test_repository_fragments_are_valid_and_changelog_is_assemblable():
    fragments, problems = cf.load_fragments(ROOT)
    assert problems == []
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    cf.insert_into_unreleased(text, [entry for _name, entries in fragments for entry in entries])


def test_gitattributes_keeps_union_merge_for_older_branches():
    assert "/CHANGELOG.md merge=union" in (ROOT / ".gitattributes").read_text()


def test_fragment_edits_after_a_candidate_mark_its_docs_stale(tmp_path) -> None:
    import os

    from brigade.release_cmd import candidate_audit_ops

    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    evidence = candidate_dir / "EVIDENCE.json"
    evidence.write_text("{}", encoding="utf-8")
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    fragment = fragments / "1234.md"
    fragment.write_text("- Old entry.\n", encoding="utf-8")
    (fragments / "README.md").write_text("# Fragments\n", encoding="utf-8")
    built = evidence.stat().st_mtime
    for path in (fragments, fragment, fragments / "README.md"):
        os.utime(path, (built - 10, built - 10))
    candidate = {"path": str(candidate_dir), "target": str(tmp_path)}
    assert candidate_audit_ops._candidate_docs_changed_after_build(candidate) == []

    fragment.write_text("- Edited entry.\n", encoding="utf-8")
    os.utime(fragment, (built + 10, built + 10))
    assert candidate_audit_ops._candidate_docs_changed_after_build(candidate) == ["changelog.d/1234.md"]

    fragment.unlink()
    os.utime(fragments, (built + 10, built + 10))
    assert candidate_audit_ops._candidate_docs_changed_after_build(candidate) == ["changelog.d"]


def test_candidate_notes_keep_wrapped_fragment_continuations(tmp_path) -> None:
    from brigade.release_cmd import candidate

    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "2231.md").write_text(
        "- The connection now retries\n  only when the server explicitly requests it.\n- A second entry.\n",
        encoding="utf-8",
    )
    assert candidate._changelog_fragment_bullets(tmp_path) == [
        "- The connection now retries only when the server explicitly requests it.",
        "- A second entry.",
    ]
