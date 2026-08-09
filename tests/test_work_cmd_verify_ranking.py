"""Rank verify candidates from GraphTrail affected-test impact (#486)."""

from __future__ import annotations

import json

from brigade import work_cmd
from brigade.work_cmd import verify_ranking

from tests.work_cmd_test_helpers import _init_git_repo


def test_verify_plan_degrades_without_graphtrail(tmp_path, capsys):
    """Real degrade path: GraphTrail is unavailable in this VM."""
    _init_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_demo.py").write_text("def test_ok():\n    assert True\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text("VALUE = 1\n")

    assert work_cmd.verify_plan(target=tmp_path, files=["src/demo.py"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    ranking = payload["graph_impact"]
    assert ranking["degraded"] is True
    assert "graphtrail" in ranking["degraded_reason"]
    assert ranking["changed_files"] == ["src/demo.py"]
    assert ranking["candidates"] == []
    assert payload["ranked_candidates"] == []
    assert payload["commands"] == ["PYTHONPATH=src python3 -m pytest -q"]


def test_verify_plan_text_prints_degraded_graph_impact(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "tests").mkdir()
    assert work_cmd.verify_plan(target=tmp_path, files=["src/a.py"], json_output=False) == 0
    out = capsys.readouterr().out
    assert "graph_impact: degraded" in out
    assert "graphtrail" in out


def test_rank_verification_candidates_via_mocked_affected(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    db = tmp_path / ".graphtrail" / "graphtrail.db"
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
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    db = tmp_path / ".graphtrail" / "graphtrail.db"
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
    assert 'brigade work verify run --command "' in payload["suggested_command"]
    assert "tests/test_a.py" in payload["suggested_command"]
    # Default full-suite command remains in the plan; ranking is advisory.
    assert payload["commands"] == ["PYTHONPATH=src python3 -m pytest -q"]


def test_verify_plan_explicit_command_keeps_worker_choice(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "tests").mkdir()
    db = tmp_path / ".graphtrail" / "graphtrail.db"
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
    assert payload["suggested_command"] == "brigade work verify run"
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
