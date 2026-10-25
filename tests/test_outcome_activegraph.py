"""ActiveGraph-inspired outcome extensions: drift oracle + fork/diff.

The drift oracle proves status.json is a reproducible projection of the decision
receipts; fork/diff replays the signal log under different configs without
touching live state. See docs/design/activegraph-inspiration.md.
"""

import json

import pytest

from brigade import outcome, outcome_cmd


def _seed(target, records):
    outcome_cmd.append_records(target, records)


def _helped(artifact_id, kind, n):
    return [
        outcome.OutcomeRecord(
            artifact_id, kind, f"t{i}", "verify", 1, f"ref-{artifact_id}-{i}", f"2026-06-20T0{i}:00:00+00:00"
        )
        for i in range(n)
    ]


def _mixed_legacy_and_delta(artifact_id, kind):
    return [
        outcome.OutcomeRecord(artifact_id, kind, "t1", "verify", 1, "ref1", "2026-06-20T00:00:00+00:00"),
        outcome.OutcomeRecord(
            artifact_id,
            kind,
            "t2",
            "verify",
            1,
            "ref2",
            "2026-06-20T01:00:00+00:00",
            code_graph_delta={
                "status": "ok",
                "summary": "changed_symbols=1",
                "changed_symbol_count": 1,
            },
        ),
    ]


@pytest.mark.parametrize(
    "records",
    [_helped("card-x", "card", 2), _mixed_legacy_and_delta("card-x", "card")],
    ids=["legacy", "mixed-legacy-and-delta"],
)
def test_rebuild_status_reproduces_persisted(tmp_path, capsys, records):
    # A card with two clean verified signals promotes on reconcile --apply, which
    # writes both a decision receipt and a status entry.
    _seed(tmp_path, records)
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True) == 0
    capsys.readouterr()
    assert outcome_cmd.load_status(tmp_path)["card-x"]["status"] == "promoted"

    # Folding the decision receipts must reproduce the persisted status exactly.
    assert outcome_cmd.rebuild_status(target=tmp_path, check=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reproducible"] is True
    assert payload["drift"] == []


def test_rebuild_status_detects_drift(tmp_path, capsys):
    _seed(tmp_path, _helped("card-x", "card", 2))
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True) == 0
    capsys.readouterr()

    # Hand-edit status.json so it no longer matches the transition log.
    status_path = tmp_path / "memory" / "outcome" / "status.json"
    data = json.loads(status_path.read_text())
    data["artifacts"]["card-x"]["status"] = "demoted"
    status_path.write_text(json.dumps(data))

    assert outcome_cmd.rebuild_status(target=tmp_path, check=True, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reproducible"] is False
    assert any(d["artifact_id"] == "card-x" and d["issue"] == "mismatch" for d in payload["drift"])


def test_rebuild_status_reports_receipt_written_before_status(tmp_path, capsys, monkeypatch):
    _seed(tmp_path, _helped("card-x", "card", 2))
    status_path = tmp_path / "memory" / "outcome" / "status.json"
    write_json = outcome_cmd.localio.write_json

    def fail_status_write(path, payload):
        if path == status_path:
            raise OSError("interrupted status write")
        write_json(path, payload)

    monkeypatch.setattr(outcome_cmd.localio, "write_json", fail_status_write)
    with pytest.raises(OSError, match="interrupted status write"):
        outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True)
    assert not status_path.exists()

    assert outcome_cmd.rebuild_status(target=tmp_path, check=True, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert any(item["artifact_id"] == "card-x" and item["issue"] == "missing-from-status" for item in payload["drift"])


def test_outcome_doctor_marks_legacy_status_without_decisions(tmp_path, capsys):
    status_path = tmp_path / "memory" / "outcome" / "status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(json.dumps({"artifacts": {"old-card": {"status": "promoted"}}}))

    assert outcome_cmd.doctor(target=tmp_path, json_output=True) == 0
    projection = json.loads(capsys.readouterr().out)["status_projection"]
    assert {"reproducible", "drift", "legacy", "skipped_receipts"} <= projection.keys()
    assert projection["legacy"] is True
    assert projection["reproducible"] is False
    assert projection["drift"] == []


def test_outcome_doctor_text_reports_status_projection(tmp_path, capsys):
    assert outcome_cmd.doctor(target=tmp_path) == 0
    assert "status_projection: reproducible=True legacy=False" in capsys.readouterr().out


@pytest.mark.parametrize(
    "contents",
    ['{"artifacts":', "[]", "{}", '{"artifacts": []}', '{"artifacts": {"card-x": "promoted"}}'],
    ids=["invalid-json", "non-object", "missing-artifacts", "non-object-artifacts", "non-object-entry"],
)
def test_outcome_doctor_rejects_invalid_status_without_decisions(tmp_path, capsys, contents):
    status_path = tmp_path / "memory" / "outcome" / "status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(contents)

    assert outcome_cmd.doctor(target=tmp_path, json_output=True) == 0
    projection = json.loads(capsys.readouterr().out)["status_projection"]
    assert projection["reproducible"] is False
    assert projection["status_valid"] is False
    assert projection["legacy"] is False
    assert status_path.read_text() == contents

    assert outcome_cmd.doctor(target=tmp_path) == 0
    assert "status_projection: reproducible=False legacy=False" in capsys.readouterr().out


@pytest.mark.parametrize(
    "receipt",
    [
        '{"artifact_id":',
        *[
            json.dumps(
                {
                    "artifact_id": "card-x",
                    "new_status": "promoted",
                    "created_at": "2026-06-20T00:00:00+00:00",
                    field: value,
                }
            )
            for field, value in [
                ("artifact_id", 123),
                ("new_status", ["promoted"]),
                ("created_at", 123),
                ("created_at", "not-a-timestamp"),
                ("artifact_id", " "),
                ("new_status", " "),
            ]
        ],
    ],
    ids=[
        "invalid-json",
        "numeric-id",
        "list-status",
        "numeric-timestamp",
        "invalid-timestamp",
        "blank-id",
        "blank-status",
    ],
)
def test_outcome_doctor_counts_and_names_malformed_decision_receipt(tmp_path, capsys, receipt):
    decisions = tmp_path / "memory" / "outcome" / "decisions"
    decisions.mkdir(parents=True)
    malformed = decisions / "broken.json"
    malformed.write_text(receipt)

    assert outcome_cmd.doctor(target=tmp_path, json_output=True) == 0
    projection = json.loads(capsys.readouterr().out)["status_projection"]
    assert projection["skipped_receipts"] == 1
    assert "broken.json" in projection["skipped_receipt_names"]
    assert projection["reproducible"] is False
    assert projection["rebuilt_count"] == 0
    assert projection["drift"] == []

    assert outcome_cmd.doctor(target=tmp_path) == 0
    assert "skipped_decision_receipts: broken.json" in capsys.readouterr().out


def test_outcome_doctor_bounds_text_drift_and_preserves_json_details(tmp_path, capsys):
    _seed(tmp_path, _helped("card-x", "card", 2))
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True) == 0
    capsys.readouterr()
    status_path = tmp_path / "memory" / "outcome" / "status.json"
    data = json.loads(status_path.read_text())
    data["artifacts"].update({f"old-card-{i:02}": {"status": "promoted"} for i in range(12)})
    status_path.write_text(json.dumps(data))

    assert outcome_cmd.doctor(target=tmp_path) == 0
    text = capsys.readouterr().out
    drift_lines = [line for line in text.splitlines() if "[only-in-status]" in line]
    assert drift_lines == [f"- old-card-{i:02} [only-in-status]" for i in range(10)]
    assert "... and 2 more" in text

    assert outcome_cmd.doctor(target=tmp_path, json_output=True) == 0
    projection = json.loads(capsys.readouterr().out)["status_projection"]
    assert projection["legacy"] is False
    assert projection["reproducible"] is False
    assert projection["drift_count"] == 12
    assert [item["artifact_id"] for item in projection["drift"]] == [f"old-card-{i:02}" for i in range(12)]


def test_rebuild_status_reproduces_promoted_route_policy(tmp_path, capsys, monkeypatch):
    from tests.test_scorecard_reconcile import _stub_execute, seed_registry_skill_scorecard_promotion
    from tests.test_outcome_cmd import _write_registry_skill

    _stub_execute(monkeypatch)
    _write_registry_skill(tmp_path, "skill-x")
    seed_registry_skill_scorecard_promotion(tmp_path, "skill-x")
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True) == 0
    capsys.readouterr()

    assert outcome_cmd.rebuild_status(target=tmp_path, check=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reproducible"] is True
    entry = outcome_cmd.load_status(tmp_path)["skill-x"]
    assert entry["route_policy"]["policy_version"] == "scorecard.v1"


def test_fork_and_diff_reflect_config_sensitivity(tmp_path, capsys):
    # card-x has enough signal to install under any threshold; card-y only
    # installs once the threshold drops to 1.
    _seed(tmp_path, _helped("card-x", "card", 2) + _helped("card-y", "card", 1))

    fork_a = tmp_path / "A.json"
    fork_b = tmp_path / "B.json"
    assert outcome_cmd.fork(target=tmp_path, out=fork_a, config=outcome.ReconcileConfig(), json_output=True) == 0
    capsys.readouterr()
    assert (
        outcome_cmd.fork(
            target=tmp_path, out=fork_b, config=outcome.ReconcileConfig(install_min_helped=1), json_output=True
        )
        == 0
    )
    capsys.readouterr()

    assert outcome_cmd.diff(target=tmp_path, fork_a=fork_a, fork_b=fork_b, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["identical"] is False
    changed = {c["artifact_id"]: c for c in payload["changed"]}
    assert "card-y" in changed  # hold@candidate under A, install@promoted under B
    assert "card-x" not in changed  # installs under both

    # A fork is read-only: the live ledger has no status.json written by fork.
    assert not (tmp_path / "memory" / "outcome" / "status.json").exists()

    # Identical configs diff clean.
    fork_c = tmp_path / "C.json"
    assert outcome_cmd.fork(target=tmp_path, out=fork_c, config=outcome.ReconcileConfig(), json_output=True) == 0
    capsys.readouterr()
    assert outcome_cmd.diff(target=tmp_path, fork_a=fork_a, fork_b=fork_c, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["identical"] is True
