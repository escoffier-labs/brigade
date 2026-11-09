"""Contracts for shadow-mode Linux test selection: fail-open selection, escape accounting, evaluation."""

import json

import pytest

from scripts import ci_shadow_evaluate as evaluate
from scripts import ci_shadow_report as report
from scripts import ci_test_selection as selector


def write(repo, name, text=""):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    write(tmp_path, "src/brigade/__init__.py")
    write(tmp_path, "src/brigade/a.py", "X = 1\n")
    write(tmp_path, "src/brigade/b.py", "from brigade import a\n")
    write(tmp_path, "src/brigade/c.py", "def run():\n    from brigade import a\n    return a.X\n")
    write(tmp_path, "src/brigade/d.py", "from brigade import c\n")
    write(tmp_path, "tests/conftest.py")
    write(tmp_path, "tests/test_a.py", "from brigade import a\n")
    write(tmp_path, "tests/test_b.py", "import brigade.b\n")
    write(tmp_path, "tests/test_c.py", "from brigade.c import run\n")
    write(tmp_path, "tests/test_d.py", "from brigade import d\n")
    write(tmp_path, "tests/test_other.py", "def test_x():\n    pass\n")
    write(tmp_path, "tests/legacy_style_test.py", "def test_x():\n    pass\n")
    write(tmp_path, "docs/guide.md", "text")
    write(tmp_path, "tests/test_reads_docs.py", "PATH = 'docs/guide.md'\n")
    return tmp_path


def test_selective_plan_carries_a_reason_per_file_and_the_lazy_cost(repo):
    plan = selector.select(repo, ["src/brigade/a.py"])
    assert plan["mode"] == "selective"
    assert plan["selected"] == {
        "tests/test_a.py": ["imports"],
        "tests/test_b.py": ["closure:0"],
        "tests/test_c.py": ["closure:1"],
        "tests/test_d.py": ["closure:1"],
    }
    # Narrower policies drop only closures that need more deferred imports than they allow.
    assert selector.policy_files(plan, "import-time") == {"tests/test_a.py", "tests/test_b.py"}
    assert selector.policy_files(plan, "lazy-1") == set(plan["selected"])
    assert selector.policy_files(plan, "full-closure") == set(plan["selected"])


def test_two_deferred_imports_only_reach_the_unbounded_policy(repo):
    write(repo, "src/brigade/e.py", "def go():\n    from brigade import d\n")
    write(repo, "tests/test_e.py", "from brigade import e\n")
    plan = selector.select(repo, ["src/brigade/a.py"])
    assert plan["selected"]["tests/test_e.py"] == ["closure:2"]
    assert "tests/test_e.py" not in selector.policy_files(plan, "lazy-1")
    assert "tests/test_e.py" in selector.policy_files(plan, "full-closure")


def test_changed_tests_docs_references_and_both_pytest_filename_patterns(repo):
    plan = selector.select(repo, ["tests/legacy_style_test.py", "docs/guide.md"])
    assert plan["mode"] == "selective"
    assert plan["selected"]["tests/legacy_style_test.py"] == ["changed"]
    assert plan["selected"]["tests/test_reads_docs.py"] == ["references:docs/guide.md"]
    assert plan["test_files_total"] == 7


def test_whole_tree_invariant_tests_are_always_selected(repo):
    write(repo, "tests/test_module_size_ratchet.py")
    plan = selector.select(repo, ["src/brigade/a.py"])
    assert plan["selected"]["tests/test_module_size_ratchet.py"] == ["invariant"]


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("tests/conftest.py", "conftest"),
        ("tests/sub/conftest.py", "conftest"),
        ("pyproject.toml", "shared-infra"),
        (".github/workflows/ci.yml", "shared-infra"),
        ("scripts/ci_pytest_shard.py", "shared-infra"),
        ("scripts/ci_test_selection.py", "shared-infra"),
        ("scripts/ci_selection_core.py", "shared-infra"),
        ("src/brigade/__init__.py", "shared-infra"),
        ("tests/fixtures/data.json", "test-support"),
        ("tests/support.py", "test-support"),
        ("src/brigade/templates/profile.json", "src-data"),
        ("engines/hub/src/main.rs", "unmapped-path"),
        ("registry.json", "unmapped-path"),
    ],
)
def test_run_everything_triggers_select_every_test(repo, path, reason):
    plan = selector.select(repo, [path, "src/brigade/a.py"])
    assert plan["mode"] == "full"
    assert [t["reason"] for t in plan["full_triggers"]] == [reason]
    assert set(plan["selected"]) == {
        "tests/test_a.py",
        "tests/test_b.py",
        "tests/test_c.py",
        "tests/test_d.py",
        "tests/test_other.py",
        "tests/legacy_style_test.py",
        "tests/test_reads_docs.py",
    }
    assert all(why == ["full"] for why in plan["selected"].values())
    # Full mode runs everything under every policy, so it can never produce an escape.
    assert selector.policy_files(plan, "import-time") == set(plan["selected"])


def test_empty_diff_and_selector_failure_fail_open(repo, tmp_path_factory, monkeypatch):
    assert selector.select(repo, [])["mode"] == "full"
    write(repo, "tests/test_broken.py", "def (:\n")
    assert selector.select(repo, ["src/brigade/a.py"])["full_triggers"] == [
        {"path": "*", "reason": "unparseable-source"}
    ]
    # A data file that merely ends in .py under tests/ may be unparseable without forcing a full run.
    (repo / "tests/test_broken.py").unlink()
    write(repo, "tests/fixtures/broken_fixture.py", "def (:\n")
    assert selector.select(repo, ["src/brigade/a.py"])["mode"] == "selective"
    nowhere = tmp_path_factory.mktemp("empty")
    plan = selector.select(nowhere, ["src/brigade/a.py"])
    assert plan["mode"] == "full"
    assert plan["full_triggers"] == [{"path": "*", "reason": "no-tests-discovered"}]
    monkeypatch.setattr(selector, "discover_tests", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
    failed = selector.select(repo, ["src/brigade/a.py"])
    assert failed["mode"] == "full"
    assert failed["full_triggers"][0]["reason"] == "selector-error:OSError"
    assert "denied" in failed["error"]


def test_dynamic_import_with_a_computed_name_reaches_the_modules_the_file_names(repo):
    write(repo, "src/brigade/lazy.py", "def load(n):\n    return __import__(f'brigade.{n}')\nNAMES = ['a']\n")
    write(repo, "tests/test_lazy.py", "from brigade import lazy\n")
    plan = selector.select(repo, ["src/brigade/a.py"])
    assert plan["selected"]["tests/test_lazy.py"] == ["closure:1"]


def test_cli_entry_main_writes_a_plan_even_when_git_fails(tmp_path, repo):
    output = tmp_path / "out" / "plan.json"
    assert selector.main(["--repo", str(repo), "--base", "no-such-ref", "--output", str(output)]) == 0
    plan = json.loads(output.read_text())
    assert plan["mode"] == "full"
    assert plan["full_triggers"][0]["reason"].startswith("diff-error")


def junit(path, cases):
    body = "".join(
        f'<testcase classname="c" name="{name}" file="{file}" time="{time}">'
        + ("<failure message='x'>trace</failure>" if bad else "")
        + "</testcase>"
        for file, name, time, bad in cases
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>', encoding="utf-8")


def selective_plan(tmp_path):
    plan = {
        "mode": "selective",
        "test_files_total": 4,
        "changed": ["src/brigade/a.py"],
        "full_triggers": [],
        "selected": {"tests/test_a.py": ["imports"], "tests/test_b.py": ["closure:1"]},
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    return path


def test_report_names_failures_outside_each_policy_as_escapes(tmp_path):
    junit(
        tmp_path / "j" / "shadow-junit-0.xml",
        [
            ("tests/test_a.py", "t1", 1.0, False),
            ("tests/test_b.py", "t2", 2.0, True),
            ("tests/test_z.py", "t3", 4.0, True),
        ],
    )
    result = report.build_report(selective_plan(tmp_path), tmp_path / "j", 1, "failure")
    assert result["status"] == "ok"
    assert result["failing_files"] == ["tests/test_b.py", "tests/test_z.py"]
    lazy = result["policies"]["lazy-1"]
    assert lazy["escape_files"] == ["tests/test_z.py"]
    assert lazy["escape_nodeids"] == ["tests/test_z.py::c::t3"]
    assert lazy["selected_seconds"] == 3.0
    # test_b needs one deferred import, so the import-time policy also misses it.
    assert result["policies"]["import-time"]["escape_files"] == ["tests/test_b.py", "tests/test_z.py"]
    assert result["total_test_seconds"] == 7.0


def test_report_is_incomplete_rather_than_clean_when_evidence_is_missing(tmp_path):
    junit(tmp_path / "j" / "shadow-junit-0.xml", [("tests/test_a.py", "t1", 1.0, False)])
    missing_shard = report.build_report(selective_plan(tmp_path), tmp_path / "j", 4, "failure")
    assert missing_shard["status"] == "incomplete"
    assert any("expected 4 junit files" in e for e in missing_shard["errors"])
    assert any("shows no failing test" in e for e in missing_shard["errors"])
    no_plan = report.build_report(tmp_path / "absent.json", tmp_path / "j", 1, "success")
    assert no_plan["status"] == "incomplete"
    assert no_plan["policies"] == {}
    (tmp_path / "j" / "bad.xml").write_text("<testsuites><oops")
    assert any(
        "unreadable" in e for e in report.build_report(selective_plan(tmp_path), tmp_path / "j", 1, "success")["errors"]
    )


def test_report_counts_a_failure_without_a_file_attribute_as_incomplete(tmp_path):
    path = tmp_path / "j" / "shadow-junit-0.xml"
    path.parent.mkdir()
    path.write_text(
        '<testsuites><testsuite><testcase name="t" time="1"><error message="m"/></testcase></testsuite></testsuites>'
    )
    result = report.build_report(selective_plan(tmp_path), tmp_path / "j", 1, "failure")
    assert result["unattributed_failures"] == 1
    assert result["status"] == "incomplete"


def test_report_main_never_fails_and_writes_an_error_artifact(tmp_path, monkeypatch):
    output = tmp_path / "out" / "shadow-selection.json"
    monkeypatch.setattr(report, "build_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert report.main(["--junit-dir", str(tmp_path), "--output", str(output)]) == 0
    data = json.loads(output.read_text())
    assert data["status"] == "error"
    assert "boom" in data["errors"][0]


def run_record(status="ok", mode="selective", failing=(), escapes=(), fraction=0.1, seconds=10, total=100):
    return {
        "status": status,
        "mode": mode,
        "full_reasons": ["unmapped-path"] if mode == "full" else [],
        "full_paths": ["engines/x"] if mode == "full" else [],
        "failing_files": list(failing),
        "total_test_seconds": total,
        "errors": [] if status == "ok" else ["expected 4 junit files"],
        "run_id": "1",
        "policies": {
            "lazy-1": {
                "selected_fraction": 1.0 if mode == "full" else fraction,
                "selected_seconds": total if mode == "full" else seconds,
                "escape_files": list(escapes),
                "escape_nodeids": [f"{e}::c::t" for e in escapes],
            }
        },
    }


def test_evaluator_counts_escapes_per_failing_selective_run_and_excludes_incomplete_runs(tmp_path):
    runs = [
        run_record(failing=["tests/test_a.py"]),
        run_record(failing=["tests/test_b.py"], escapes=["tests/test_b.py"]),
        run_record(mode="full", failing=["tests/test_c.py"]),
        run_record(),
        run_record(status="incomplete", failing=["tests/test_d.py"], escapes=["tests/test_d.py"]),
    ]
    for i, record in enumerate(runs):
        write(tmp_path, f"{i}/shadow-selection.json", json.dumps(record))
    write(tmp_path, "9/shadow-selection.json", "{not json")
    result = evaluate.evaluate(evaluate.load_runs(tmp_path))
    assert (result["runs"], result["ok"], result["incomplete"], result["error"]) == (6, 4, 1, 1)
    assert result["full_mode_runs"] == 1
    assert result["failing_runs"] == 3
    assert result["selective_failing_runs"] == 2
    policy = result["policies"]["lazy-1"]
    assert policy["escape_runs"] == 1
    assert policy["escape_rate_per_failing_selective_run"] == 0.5
    # 3 selective runs at 10s plus one full run at 100s, of 400s total.
    assert policy["selected_seconds_share"] == pytest.approx(130 / 400)
    assert "escapes: 1 runs" in evaluate.render(result)
