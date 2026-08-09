"""Parallel-safe ready partition into dispatch waves (#778)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from brigade import work_cmd
from brigade.work_cmd import footprint as footprint_mod
from brigade.work_cmd import partition as partition_mod

from tests.work_cmd_test_helpers import _init_git_repo


def _add(target: Path, text: str, **kwargs):
    task, created = work_cmd._add_task(target, text, **kwargs)
    assert created
    return task


def _set_pending_footprint(target: Path, task_id: str, *, files: list[str], symbol_ids: list[str] | None = None):
    """Stamp a refined footprint while keeping the task pending (ready-eligible)."""
    ledger = work_cmd._read_task_ledger(target)
    task = next(item for item in ledger["tasks"] if item["id"] == task_id)
    footprint_mod.set_task_footprint(
        task,
        footprint_mod.normalize_footprint(
            {
                "files": files,
                "symbol_ids": symbol_ids or [],
                "snapshot_hash": "",
            },
            phase=footprint_mod.PHASE_REFINED,
        ),
    )
    work_cmd._write_task_ledger(target, ledger)


def test_disjoint_footprints_share_one_wave(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 14, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)

    a = _add(tmp_path, "Edit alpha")
    b = _add(tmp_path, "Edit beta")
    _set_pending_footprint(tmp_path, a["id"], files=["src/alpha.py"])
    _set_pending_footprint(tmp_path, b["id"], files=["src/beta.py"])

    assert work_cmd.ready(target=tmp_path, parallel_safe=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["parallel_safe"] is True
    assert payload["wave_count"] == 1
    assert payload["waves"][0]["task_ids"] == [a["id"], b["id"]]
    assert payload["waves"][0]["exclusive"] is False
    assert payload["partition_mode"] == partition_mod.MODE_FILE_OVERLAP
    assert payload["partition_degraded"] is True
    assert "graphtrail" in payload["partition_degraded_reason"]


def test_overlapping_footprints_serialize_across_waves(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 14, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)

    a = _add(tmp_path, "Touch shared ledger")
    b = _add(tmp_path, "Also touch shared ledger")
    c = _add(tmp_path, "Unrelated helpers")
    _set_pending_footprint(tmp_path, a["id"], files=["src/brigade/work_cmd/ledger.py", "src/shared.py"])
    _set_pending_footprint(tmp_path, b["id"], files=["src/shared.py", "tests/test_shared.py"])
    _set_pending_footprint(tmp_path, c["id"], files=["src/brigade/work_cmd/helpers.py"])

    assert work_cmd.ready(target=tmp_path, parallel_safe=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["wave_count"] == 2
    # Greedy earliest-fit: a+c share wave 0 (no overlap); b alone in wave 1.
    assert payload["waves"][0]["task_ids"] == [a["id"], c["id"]]
    assert payload["waves"][1]["task_ids"] == [b["id"]]


def test_empty_footprint_wave_is_exclusive(tmp_path, monkeypatch, capsys):
    """Real degrade path leaves filing footprints empty — exclusive waves."""
    _init_git_repo(tmp_path)
    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 14, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)

    empty_a = _add(tmp_path, "Unknown footprint A", metadata={"symbol_ids": ["pkg.unknown"]})
    empty_b = _add(tmp_path, "Unknown footprint B")
    known = _add(tmp_path, "Known files")
    _set_pending_footprint(tmp_path, known["id"], files=["src/known.py"])

    # Filing without GraphTrail yields empty predicted footprints (real VM path).
    assert empty_a["metadata"]["footprint"]["files"] == []
    assert empty_a["metadata"]["footprint"]["degraded"] is True

    assert work_cmd.ready(target=tmp_path, parallel_safe=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["partition_degraded"] is True
    assert payload["wave_count"] == 3
    waves_by_id = {wave["task_ids"][0]: wave for wave in payload["waves"]}
    assert waves_by_id[empty_a["id"]]["exclusive"] is True
    assert waves_by_id[empty_b["id"]]["exclusive"] is True
    assert waves_by_id[known["id"]]["exclusive"] is False
    # Empties never cohabit with each other or with known work.
    for wave in payload["waves"]:
        assert len(wave["task_ids"]) == 1


def test_ready_without_parallel_safe_omits_wave_fields(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 14, 0, 0, tzinfo=timezone.utc),
    )
    _add(tmp_path, "Plain ready")
    assert work_cmd.ready(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "waves" not in payload
    assert "parallel_safe" not in payload
    assert "partition_mode" not in payload


def test_symbol_impact_refinement_detects_indirect_overlap(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 14, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)

    db = tmp_path / ".graphtrail" / "graphtrail.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"fake-graphtrail-index")
    monkeypatch.setattr(partition_mod.component_bins, "resolve", lambda _name: "/fake/graphtrail")

    def fake_impact(target, binary, db_path, query):
        assert binary == "/fake/graphtrail"
        impacts = {
            "pkg.alpha": {"related_files": ["src/shared_core.py"]},
            "pkg.beta": {"related_files": ["src/shared_core.py", "src/beta_only.py"]},
        }
        return impacts.get(query, {"related_files": []})

    a = _add(tmp_path, "Symbol alpha")
    b = _add(tmp_path, "Symbol beta")
    _set_pending_footprint(tmp_path, a["id"], files=["src/alpha.py"], symbol_ids=["pkg.alpha"])
    _set_pending_footprint(tmp_path, b["id"], files=["src/beta.py"], symbol_ids=["pkg.beta"])

    # File sets alone are disjoint; symbol impact reveals shared_core.py.
    payload = work_cmd._readiness_payload(tmp_path, parallel_safe=True, impact_runner=fake_impact)
    assert payload["partition_mode"] == partition_mod.MODE_FILE_OVERLAP_SYMBOL_IMPACT
    assert payload["partition_degraded"] is False
    assert payload["wave_count"] == 2
    assert payload["waves"][0]["task_ids"] == [a["id"]]
    assert payload["waves"][1]["task_ids"] == [b["id"]]


def test_footprints_overlap_helper_empty_is_exclusive():
    assert partition_mod.footprints_overlap(
        {"files": [], "symbol_ids": []},
        {"files": ["a.py"], "symbol_ids": []},
    )
    assert partition_mod.footprints_overlap(
        {"files": [], "symbol_ids": []},
        {"files": [], "symbol_ids": []},
    )
    assert not partition_mod.footprints_overlap(
        {"files": ["a.py"], "symbol_ids": []},
        {"files": ["b.py"], "symbol_ids": []},
    )
    assert partition_mod.footprints_overlap(
        {"files": ["a.py", "shared.py"], "symbol_ids": []},
        {"files": ["shared.py"], "symbol_ids": []},
    )


def test_parallel_safe_human_output_lists_waves(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 14, 0, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "Human wave")
    _set_pending_footprint(tmp_path, task["id"], files=["src/human.py"])
    assert work_cmd.ready(target=tmp_path, parallel_safe=True) == 0
    out = capsys.readouterr().out
    assert "waves: 1" in out
    assert "partition_mode: file_overlap" in out
    assert "partition_degraded:" in out
    assert "wave 0:" in out
