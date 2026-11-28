"""Rank verify candidates from GraphTrail affected-test impact (#486)."""

from __future__ import annotations

import json
from pathlib import Path
import shlex

import pytest

from brigade import work_cmd
from brigade.work_cmd import verify_ranking

from tests.work_cmd_test_helpers import _init_git_repo


def test_verify_plan_degrades_without_graphtrail(tmp_path, capsys):
    """Real degrade path: GraphTrail is unavailable in this VM."""
    _init_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\ndependencies=['pytest']\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_demo.py").write_text("def test_ok():\n    assert True\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text("VALUE = 1\n")

    assert work_cmd.verify_plan(target=tmp_path, files=["src/demo.py"], json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    ranking = payload["graph_impact"]
    assert ranking["degraded"] is True
    assert "graphtrail" in ranking["degraded_reason"]
    assert ranking["changed_files"] == ["src/demo.py"]
    assert ranking["candidates"] == []
    assert payload["ranked_candidates"] == []
    assert payload["commands"] == []
    assert "focused test selector" in payload["blockers"][0]


def test_verify_plan_text_prints_degraded_graph_impact(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "tests").mkdir()
    assert work_cmd.verify_plan(target=tmp_path, files=["src/a.py"], json_output=False) == 1
    out = capsys.readouterr().out
    assert "graph_impact: degraded" in out
    assert "graphtrail" in out


def test_rank_verification_candidates_via_mocked_affected(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\ndependencies=['pytest']\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    db = tmp_path / ".brigade" / "code-graph" / "code-graph.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"fake-graphtrail-index")

    monkeypatch.setattr(verify_ranking.component_bins, "resolve", lambda _name: "/fake/graphtrail")

    def fake_affected(target, binary, db_path, files, depth):
        assert binary == "/fake/graphtrail"
        assert db_path == db
        assert list(files) == ["src/pkg/mod.py"]
        assert depth == verify_ranking.DEFAULT_AFFECTED_DEPTH
        return {
            "attribution": "tests statically attributed through incoming call edges",
            "depth": 3,
            "changed_files": ["src/pkg/mod.py"],
            "missing_files": [],
            "affected_tests": [
                {
                    "file_path": "tests/test_mod.py",
                    "min_hops": 2,
                    "via": ["test_mod_helper", "test_mod_main"],
                },
                {
                    "file_path": "tests/test_mod_edge.py",
                    "min_hops": 1,
                    "via": ["test_edge"],
                },
                {
                    "file_path": "tests/test_far.py",
                    "min_hops": 4,
                    "via": ["test_far"],
                },
            ],
            "impacted_files": [{"file_path": "src/pkg/other.py", "min_hops": 1, "via": ["helper"]}],
            "truncated": False,
        }

    monkeypatch.setattr(verify_ranking, "_run_graphtrail_affected", fake_affected)

    ranking = verify_ranking.rank_verification_candidates(tmp_path, files=["src/pkg/mod.py"])
    assert ranking["degraded"] is False
    assert ranking["changed_files"] == ["src/pkg/mod.py"]
    candidates = ranking["candidates"]
    assert [item["test_path"] for item in candidates] == [
        "tests/test_mod_edge.py",
        "tests/test_mod.py",
        "tests/test_far.py",
    ]
    assert candidates[0]["confidence"]["band"] == "high"
    assert candidates[0]["confidence"]["score"] == 0.8
    assert candidates[0]["evidence"]["via"] == ["test_edge"]
    assert candidates[0]["command"] == "PYTHONPATH=src python3 -m pytest -q tests/test_mod_edge.py"
    assert candidates[1]["confidence"]["band"] == "medium"
    assert candidates[2]["confidence"]["band"] == "low"
    assert ranking["suggested_command"] == (
        "PYTHONPATH=src python3 -m pytest -q tests/test_mod_edge.py tests/test_mod.py tests/test_far.py"
    )


def test_verify_plan_enriched_suggests_ranked_command(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\ndependencies=['pytest']\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    db = tmp_path / ".brigade" / "code-graph" / "code-graph.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"fake-graphtrail-index")

    monkeypatch.setattr(verify_ranking.component_bins, "resolve", lambda _name: "/fake/graphtrail")

    def fake_affected(target, binary, db_path, files, depth):
        return {
            "attribution": "lower bound note",
            "depth": 3,
            "changed_files": list(files),
            "missing_files": [],
            "affected_tests": [
                {"file_path": "tests/test_a.py", "min_hops": 1, "via": ["test_a"]},
            ],
            "impacted_files": [],
            "truncated": False,
        }

    monkeypatch.setattr(verify_ranking, "_run_graphtrail_affected", fake_affected)

    assert work_cmd.verify_plan(target=tmp_path, files=["src/a.py"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["suggested_from_graph_impact"] is True
    assert payload["ranked_candidates"][0]["test_path"] == "tests/test_a.py"
    assert shlex.split(payload["suggested_command"]) == [
        "brigade",
        "work",
        "verify",
        "run",
        f"--target={tmp_path.resolve()}",
        "--command=PYTHONPATH=src python3 -m pytest -q tests/test_a.py",
    ]
    assert "tests/test_a.py" in payload["suggested_command"]
    assert payload["commands"] == ["PYTHONPATH=src python3 -m pytest -q tests/test_a.py"]


def test_verify_plan_explicit_command_keeps_worker_choice(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "tests").mkdir()
    db = tmp_path / ".brigade" / "code-graph" / "code-graph.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"fake-index")
    monkeypatch.setattr(verify_ranking.component_bins, "resolve", lambda _name: "/fake/graphtrail")

    def fake_affected(target, binary, db_path, files, depth):
        return {
            "changed_files": ["src/a.py"],
            "missing_files": [],
            "affected_tests": [{"file_path": "tests/test_a.py", "min_hops": 1, "via": ["t"]}],
            "impacted_files": [],
        }

    monkeypatch.setattr(verify_ranking, "_run_graphtrail_affected", fake_affected)

    assert (
        work_cmd.verify_plan(
            target=tmp_path,
            commands=["python3 -m pytest -q tests/custom.py"],
            files=["src/a.py"],
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["commands"] == ["python3 -m pytest -q tests/custom.py"]
    assert shlex.split(payload["suggested_command"]) == [
        "brigade",
        "work",
        "verify",
        "run",
        f"--target={tmp_path.resolve()}",
        "--command=python3 -m pytest -q tests/custom.py",
    ]
    assert "suggested_from_graph_impact" not in payload
    assert payload["ranked_candidates"][0]["test_path"] == "tests/test_a.py"


def test_confidence_for_hops_bands():
    assert verify_ranking.confidence_for_hops(0) == {"score": 1.0, "band": "high", "min_hops": 0}
    assert verify_ranking.confidence_for_hops(1)["band"] == "high"
    assert verify_ranking.confidence_for_hops(2)["band"] == "medium"
    assert verify_ranking.confidence_for_hops(4)["band"] == "low"
    assert verify_ranking.confidence_for_hops(4)["score"] == 0.2


def test_collect_changed_files_from_git(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "tracked.py").write_text("x = 1\n")
    import subprocess

    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "add"],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    (tmp_path / "tracked.py").write_text("x = 2\n")
    assert verify_ranking.collect_changed_files(tmp_path) == ["tracked.py"]
    assert verify_ranking.collect_changed_files(tmp_path, files=["src/a.py", "src/a.py"]) == ["src/a.py"]


def test_rank_verification_candidates_deduplicates_duplicate_paths(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\ndependencies=['pytest']\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    db = tmp_path / ".brigade" / "code-graph" / "code-graph.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"fake-graphtrail-index")

    monkeypatch.setattr(verify_ranking.component_bins, "resolve", lambda _name: "/fake/graphtrail")

    def fake_affected(target, binary, db_path, files, depth):
        return {
            "changed_files": ["src/pkg/mod.py"],
            "missing_files": [],
            "affected_tests": [
                {
                    "file_path": "tests/test_mod.py",
                    "min_hops": 3,
                    "via": ["far_helper"],
                },
                {
                    "file_path": "tests/test_mod.py",
                    "min_hops": 1,
                    "via": ["near_helper"],
                },
                {
                    "path": "tests/test_mod.py",
                    "min_hops": 2,
                    "via": ["mid_helper"],
                },
                {
                    "file_path": "tests/test_other.py",
                    "min_hops": 1,
                    "via": ["other"],
                },
            ],
        }

    monkeypatch.setattr(verify_ranking, "_run_graphtrail_affected", fake_affected)

    ranking = verify_ranking.rank_verification_candidates(tmp_path, files=["src/pkg/mod.py"])
    candidates = ranking["candidates"]
    assert [item["test_path"] for item in candidates] == [
        "tests/test_mod.py",
        "tests/test_other.py",
    ]
    assert candidates[0]["confidence"]["min_hops"] == 1
    assert candidates[0]["evidence"]["via"] == ["near_helper"]
    assert ranking["suggested_command"] == ("PYTHONPATH=src python3 -m pytest -q tests/test_mod.py tests/test_other.py")


def test_rank_verification_candidates_npm_keeps_advisory_paths_only(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    (tmp_path / "package.json").write_text('{"name":"demo","scripts":{"test":"node --test"}}')
    (tmp_path / "src").mkdir()
    db = tmp_path / ".brigade" / "code-graph" / "code-graph.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"fake-graphtrail-index")

    monkeypatch.setattr(verify_ranking.component_bins, "resolve", lambda _name: "/fake/graphtrail")

    def fake_affected(target, binary, db_path, files, depth):
        return {
            "changed_files": ["src/mod.js"],
            "missing_files": [],
            "affected_tests": [
                {"file_path": "src/mod.test.js", "min_hops": 1, "via": ["test_mod"]},
            ],
        }

    monkeypatch.setattr(verify_ranking, "_run_graphtrail_affected", fake_affected)

    ranking = verify_ranking.rank_verification_candidates(tmp_path, files=["src/mod.js"])
    assert ranking["candidates"][0]["test_path"] == "src/mod.test.js"
    assert ranking["candidates"][0]["command"] == "npm test -- src/mod.test.js"
    assert "pytest" not in ranking["candidates"][0]["command"]
    assert ranking["suggested_command"] is None


def test_verify_plan_npm_lists_candidates_without_a_default(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "package.json").write_text('{"name":"demo","scripts":{"test":"node --test"}}')
    (tmp_path / "src").mkdir()
    db = tmp_path / ".brigade" / "code-graph" / "code-graph.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"fake-graphtrail-index")

    monkeypatch.setattr(verify_ranking.component_bins, "resolve", lambda _name: "/fake/graphtrail")

    def fake_affected(target, binary, db_path, files, depth):
        return {
            "changed_files": ["src/mod.js"],
            "missing_files": [],
            "affected_tests": [
                {"file_path": "src/mod.test.js", "min_hops": 1, "via": ["test_mod"]},
            ],
        }

    monkeypatch.setattr(verify_ranking, "_run_graphtrail_affected", fake_affected)

    assert work_cmd.verify_plan(target=tmp_path, files=["src/mod.js"], json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["commands"] == []
    assert shlex.split(payload["suggested_command"]) == [
        "brigade",
        "work",
        "verify",
        "run",
        f"--target={tmp_path.resolve()}",
        "--command=<focused test command>",
    ]
    assert "suggested_from_graph_impact" not in payload
    assert payload["ranked_candidates"][0]["test_path"] == "src/mod.test.js"
    assert payload["ranked_candidates"][0]["command"] == "npm test -- src/mod.test.js"
    assert "pytest" not in payload["ranked_candidates"][0]["command"]


def test_cli_verify_plan_passes_files(tmp_path, monkeypatch, capsys):
    from brigade import cli

    _init_git_repo(tmp_path)
    (tmp_path / "tests").mkdir()
    seen: dict[str, object] = {}

    def fake_verify_plan(**kwargs):
        seen.update(kwargs)
        print(json.dumps({"ok": True, "blockers": []}))
        return 0

    monkeypatch.setattr(work_cmd, "verify_plan", fake_verify_plan)
    rc = cli.main(
        [
            "work",
            "verify",
            "plan",
            "--target",
            str(tmp_path),
            "--file",
            "src/a.py",
            "--file",
            "src/b.py",
            "--json",
        ]
    )
    assert rc == 0
    assert seen["files"] == ["src/a.py", "src/b.py"]
    assert seen["json_output"] is True


def test_command_prefix_does_not_guess_pytest_from_tests_directory(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "widget.test.js").write_text("")
    (tmp_path / "package.json").write_text("{}")

    assert verify_ranking._verification_command_prefix(tmp_path) is None


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("pyproject.toml", "[tool.pytest.ini_options]\n"),
        ("pyproject.toml", '[project]\ndependencies = ["pytest>=9"]\n'),
        ("pytest.ini", ""),
        ("setup.cfg", "[tool:pytest]\n"),
        ("tox.ini", "[pytest]\n"),
        ("conftest.py", ""),
        ("tests/conftest.py", ""),
    ],
)
def test_command_prefix_detects_each_pytest_evidence_file(tmp_path, path, content):
    evidence = tmp_path / path
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text(content)

    assert verify_ranking._verification_command_prefix(tmp_path) == "python3 -m pytest -q"


def test_command_prefix_detects_brigadestack_layout():
    repository = Path(__file__).resolve().parents[1]

    assert verify_ranking._verification_command_prefix(repository) == "PYTHONPATH=src python3 -m pytest -q"


@pytest.mark.parametrize(
    ("path", "template"),
    [
        ("pyproject.toml", '[project]\ndependencies = ["{dependency}>=9"]\n'),
        ("pyproject.toml", '[project.optional-dependencies]\ntest = ["{dependency}[extra]>=9"]\n'),
        ("pyproject.toml", '[dependency-groups]\ntest = [{{include-group = "base"}}, "{dependency}>=9"]\n'),
        ("pyproject.toml", '[tool.poetry.dependencies]\n"{dependency}" = "^9"\n'),
        ("pyproject.toml", '[tool.poetry.group.test.dependencies]\n"{dependency}" = {{version = "^9"}}\n'),
        ("pyproject.toml", '[tool.pdm.dev-dependencies]\ntest = ["{dependency}>=9"]\n'),
        ("requirements-dev.txt", '# Development tools\n{dependency}[extra]>=9 ; python_version >= "3.11"\n'),
    ],
)
@pytest.mark.parametrize("dependency", ["pytest", "PyTeSt", "pytest-cov", "pytest_custom", "pytest2"])
def test_dependency_evidence_matches_only_pytest_without_tests_directory(tmp_path, path, template, dependency):
    (tmp_path / path).write_text(template.format(dependency=dependency))

    assert not (tmp_path / "tests").exists()
    expected = "python3 -m pytest -q" if dependency.casefold() == "pytest" else None
    assert verify_ranking._verification_command_prefix(tmp_path) == expected


@pytest.mark.parametrize("path", ["pyproject.toml", "setup.cfg", "tox.ini", "requirements-dev.txt"])
def test_ranking_ignores_undecodable_configuration(tmp_path, monkeypatch, path):
    (tmp_path / path).write_bytes(b"\xff\xfe")
    db = tmp_path / ".brigade" / "code-graph" / "code-graph.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"fake-index")
    monkeypatch.setattr(verify_ranking.component_bins, "resolve", lambda _name: "/fake/code-graph")

    ranking = verify_ranking.rank_verification_candidates(
        tmp_path,
        files=["module.py"],
        affected_runner=lambda *args: {
            "changed_files": ["module.py"],
            "affected_tests": [{"file_path": "test_module.py", "min_hops": 1}],
        },
    )

    assert ranking["degraded"] is False
    assert ranking["suggested_command"] is None
    assert ranking["candidates"][0]["command"] == "test_module.py"


def test_dependency_detection_does_not_execute_or_follow_declarations(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ntest = [{include-group = "test"}]\n')
    (tmp_path / "requirements-dev.txt").write_text("-r extra-requirements.txt\n-e .\n")
    (tmp_path / "extra-requirements.txt").write_text("pytest\n")
    (tmp_path / "setup.py").write_text('raise RuntimeError("must not execute")\n')

    def unexpected_run(*args, **kwargs):
        pytest.fail("dependency detection must only read static evidence")

    monkeypatch.setattr(verify_ranking.proc, "run", unexpected_run)
    assert verify_ranking._verification_command_prefix(tmp_path) is None
