"""ActiveGraph-inspired outcome extensions: drift oracle + fork/diff.

The drift oracle proves status.json is a reproducible projection of the decision
receipts; fork/diff replays the signal log under different configs without
touching live state. See docs/design/activegraph-inspiration.md.
"""

import json

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


def test_rebuild_status_reproduces_persisted(tmp_path, capsys):
    # A card with two clean verified signals promotes on reconcile --apply, which
    # writes both a decision receipt and a status entry.
    _seed(tmp_path, _helped("card-x", "card", 2))
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


def test_fork_and_diff_reflect_config_sensitivity(tmp_path, capsys):
    # skill-x has enough signal to install under any threshold; skill-y only
    # installs once the threshold drops to 1.
    _seed(tmp_path, _helped("skill-x", "skill", 2) + _helped("skill-y", "skill", 1))

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
    assert "skill-y" in changed  # hold@candidate under A, install@promoted under B
    assert "skill-x" not in changed  # installs under both

    # A fork is read-only: the live ledger has no status.json written by fork.
    assert not (tmp_path / "memory" / "outcome" / "status.json").exists()

    # Identical configs diff clean.
    fork_c = tmp_path / "C.json"
    assert outcome_cmd.fork(target=tmp_path, out=fork_c, config=outcome.ReconcileConfig(), json_output=True) == 0
    capsys.readouterr()
    assert outcome_cmd.diff(target=tmp_path, fork_a=fork_a, fork_b=fork_c, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["identical"] is True
