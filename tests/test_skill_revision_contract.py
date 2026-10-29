"""Proposal identity and contract boundaries."""

import json

import pytest

from brigade import cli, skills_cmd
from tests.test_skills_cmd import _write_skill


def contract():
    return {
        "schema_version": 1,
        "reason": "Report verification honestly",
        "addressed_failure": "A timeout was reported as passing",
        "change_category": "behavior",
        "baseline_fingerprint": None,
        "expected_observable_behavior": [{"id": "timeout", "description": "Report timeout as incomplete"}],
    }


@pytest.mark.parametrize("minimal_metadata", [False, True])
def test_reviewed_candidate_identity_survives_acceptance(tmp_path, capsys, minimal_metadata):
    from brigade.skills_cmd import registry

    source = _write_skill(tmp_path / "source")
    if minimal_metadata:
        (source / "skill.json").write_text('{"id":"security-review"}')
    assert skills_cmd.inbox_add(target=tmp_path, source=source, behavioral_contract=contract(), json_output=True) == 0
    proposal = json.loads(capsys.readouterr().out)
    assert skills_cmd.inbox_accept(target=tmp_path, proposal_id=proposal["proposal_id"]) == 0
    with registry._held_state_root(tmp_path, writable=False) as anchor:
        accepted = registry._baseline_identity(anchor, "security-review")
    assert accepted == proposal["candidate_identity"]


@pytest.mark.parametrize(
    "field,value", [("reason", "Changed after review"), ("baseline_fingerprint", "a" * 64), ("removed", None)]
)
def test_accept_refuses_separately_changed_contract(tmp_path, capsys, field, value):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.inbox_add(target=tmp_path, source=source, behavioral_contract=contract(), json_output=True) == 0
    proposal = json.loads(capsys.readouterr().out)
    path = tmp_path / ".brigade/skills/inbox" / proposal["proposal_id"] / "proposal.json"
    if field == "removed":
        proposal["behavioral_contract"] = None
    else:
        proposal["behavioral_contract"][field] = value
    path.write_text(json.dumps(proposal))
    assert skills_cmd.inbox_accept(target=tmp_path, proposal_id=proposal["proposal_id"]) == 2
    assert not (tmp_path / ".brigade/skills/registry/security-review").exists()


@pytest.mark.parametrize("selector", [[], {}])
def test_cli_and_report_refuse_nonstring_binding_selector(tmp_path, selector):
    from brigade import fleet_skills_review as review
    from tests.test_fleet_skills_review import seeded

    conn, _, _ = seeded(tmp_path, bindings=True)
    node, encoded = conn.execute("SELECT node_id, report FROM fleet_skill_reports LIMIT 1").fetchone()
    report = json.loads(encoded)
    binding = report["copies"][0]["binding"]
    binding["source_selector"] = selector
    with pytest.raises(SystemExit) as exc:
        cli.main(
            ["skills", "install", "registry:example", "--target", "mcp", "--expected-binding-json", json.dumps(binding)]
        )
    assert exc.value.code == 2
    with pytest.raises(review.SkillsReportError, match="source selector"):
        review.store_report(conn, node, report)
    assert conn.execute("SELECT report FROM fleet_skill_reports WHERE node_id=?", (node,)).fetchone()[0] == encoded
    conn.close()


def test_contract_cli_roundtrip_and_accepted_metadata(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert (
        cli.main(
            [
                "skills",
                "inbox",
                "add",
                str(source),
                "--target",
                str(tmp_path),
                "--behavioral-contract-json",
                json.dumps(contract()),
                "--json",
            ]
        )
        == 0
    )
    proposal = json.loads(capsys.readouterr().out)
    assert proposal["behavioral_contract"] == contract()
    assert skills_cmd.inbox_accept(target=tmp_path, proposal_id=proposal["proposal_id"]) == 0
    meta = json.loads((tmp_path / ".brigade/skills/registry/security-review/skill.json").read_text())
    assert meta["behavioral_contract"] == contract()


@pytest.mark.parametrize("baseline", [None, "a" * 64])
def test_embedded_contract_is_effective_and_validated(tmp_path, capsys, baseline):
    source = _write_skill(tmp_path / "source")
    path = source / "skill.json"
    metadata = json.loads(path.read_text())
    embedded = contract()
    embedded["baseline_fingerprint"] = baseline
    metadata["behavioral_contract"] = embedded
    path.write_text(json.dumps(metadata))
    result = skills_cmd.inbox_add(target=tmp_path, source=source, json_output=True)
    if baseline is not None:
        assert result == 2
        assert not (tmp_path / ".brigade/skills/inbox").exists()
    else:
        assert result == 0
        proposal = json.loads(capsys.readouterr().out)
        assert proposal["behavioral_contract"] == embedded
        assert proposal["contract_status"] == "complete"


def test_null_proposal_contract_cannot_hide_embedded_contract(tmp_path, capsys):
    from brigade.localio import canonical_json_digest

    source = _write_skill(tmp_path / "source")
    assert skills_cmd.inbox_add(target=tmp_path, source=source, behavioral_contract=contract(), json_output=True) == 0
    proposal = json.loads(capsys.readouterr().out)
    path = tmp_path / ".brigade/skills/inbox" / proposal["proposal_id"] / "proposal.json"
    proposal.update(behavioral_contract=None, contract_identity=canonical_json_digest(None))
    path.write_text(json.dumps(proposal))
    assert skills_cmd.inbox_accept(target=tmp_path, proposal_id=proposal["proposal_id"]) == 2
    assert not (tmp_path / ".brigade/skills/registry/security-review").exists()


@pytest.mark.parametrize("filename", ["SKILL.md", "helper.py", "skill.json"])
def test_accept_refuses_candidate_mutation(tmp_path, capsys, filename):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.inbox_add(target=tmp_path, source=source, json_output=True) == 0
    proposal = json.loads(capsys.readouterr().out)
    path = tmp_path / ".brigade/skills/inbox" / proposal["proposal_id"] / "skill" / filename
    path.write_text("{}" if filename == "skill.json" else "changed")
    assert skills_cmd.inbox_accept(target=tmp_path, proposal_id=proposal["proposal_id"]) == 2
    assert "candidate changed" in capsys.readouterr().err


def test_accept_refuses_baseline_mutation(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source) == 0
    capsys.readouterr()
    assert skills_cmd.inbox_add(target=tmp_path, source=source, json_output=True) == 0
    proposal = json.loads(capsys.readouterr().out)
    (tmp_path / ".brigade/skills/registry/security-review/helper.py").write_text("changed")
    assert skills_cmd.inbox_accept(target=tmp_path, proposal_id=proposal["proposal_id"], force=True) == 2
    assert "baseline changed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("reason", ""),
        ("change_category", "anything"),
        ("baseline_fingerprint", "bad"),
        ("expected_observable_behavior", []),
    ],
)
def test_invalid_contract_refuses_before_write(tmp_path, capsys, field, value):
    source = _write_skill(tmp_path / "source")
    raw = contract()
    raw[field] = value
    assert skills_cmd.inbox_add(target=tmp_path, source=source, behavioral_contract=raw) == 2
    assert not (tmp_path / ".brigade/skills/inbox").exists()


@pytest.mark.parametrize("action", ["install", "uninstall", "rollback"])
def test_mutation_precondition_refuses_changed_copy(tmp_path, capsys, action):
    from brigade.skills_cmd.install import _review_binding

    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source) == 0
    assert skills_cmd.install(workspace=tmp_path, skill="registry:security-review", harness="mcp") == 0
    assert skills_cmd.install(workspace=tmp_path, skill="registry:security-review", harness="mcp", force=True) == 0
    binding = _review_binding(tmp_path, "security-review", "mcp", "registry:security-review")
    assert binding is not None
    dest = skills_cmd._install_dir(tmp_path, "mcp", "security-review")
    (dest / "helper.py").write_text("changed since review")
    capsys.readouterr()
    kwargs = dict(workspace=tmp_path, skill="security-review", harness="mcp", expected_binding=binding)
    if action == "install":
        kwargs.update(skill="registry:security-review", force=True)
    assert getattr(skills_cmd, action)(**kwargs) == 2
    assert "review-stale" in capsys.readouterr().err
    assert (dest / "helper.py").read_text() == "changed since review"


@pytest.mark.parametrize("changed", ["source-metadata", "rollback"])
def test_precondition_binds_source_metadata_and_exact_rollback(tmp_path, capsys, changed):
    from brigade.skills_cmd.install import _review_binding

    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source) == 0
    assert skills_cmd.install(workspace=tmp_path, skill="registry:security-review", harness="mcp") == 0
    assert skills_cmd.install(workspace=tmp_path, skill="registry:security-review", harness="mcp", force=True) == 0
    binding = _review_binding(tmp_path, "security-review", "mcp", "registry:security-review")
    assert binding is not None
    if changed == "source-metadata":
        path = tmp_path / ".brigade/skills/registry/security-review/skill.json"
        metadata = json.loads(path.read_text())
        metadata["required_tools"].append("python")
        path.write_text(json.dumps(metadata))
        action = "install"
    else:
        receipt = json.loads((tmp_path / ".brigade/skills/installs/security-review-mcp.json").read_text())
        from pathlib import Path

        path = Path(receipt["rollback_snapshot"]) / "helper.py"
        path.write_text("changed snapshot")
        action = "rollback"
    capsys.readouterr()
    kwargs = dict(
        workspace=tmp_path,
        skill="registry:security-review" if action == "install" else "security-review",
        harness="mcp",
        expected_binding=binding,
    )
    if action == "install":
        kwargs["force"] = True
    assert getattr(skills_cmd, action)(**kwargs) == 2
    assert "review-stale" in capsys.readouterr().err
    assert path.exists()


def test_accept_binds_nested_metadata_file(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    (source / "helpers").mkdir()
    (source / "helpers/skill.json").write_text('{"mode":"safe"}')
    assert skills_cmd.inbox_add(target=tmp_path, source=source, json_output=True) == 0
    proposal = json.loads(capsys.readouterr().out)
    path = tmp_path / ".brigade/skills/inbox" / proposal["proposal_id"] / "skill/helpers/skill.json"
    path.write_text('{"mode":"changed"}')
    assert skills_cmd.inbox_accept(target=tmp_path, proposal_id=proposal["proposal_id"]) == 2
    assert "candidate changed" in capsys.readouterr().err


def test_cli_rejects_nonobject_expected_binding(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "skills",
                "install",
                "registry:example",
                "--workspace",
                str(tmp_path),
                "--target",
                "mcp",
                "--expected-binding-json",
                "[]",
            ]
        )
    assert exc.value.code == 2
    assert not (tmp_path / ".brigade").exists()


def test_added_invalid_metadata_changes_candidate_identity(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    (source / "skill.json").unlink()
    assert skills_cmd.inbox_add(target=tmp_path, source=source, json_output=True) == 0
    proposal = json.loads(capsys.readouterr().out)
    path = tmp_path / ".brigade/skills/inbox" / proposal["proposal_id"] / "skill/skill.json"
    path.write_bytes(b"invalid metadata")
    assert skills_cmd.inbox_accept(target=tmp_path, proposal_id=proposal["proposal_id"]) == 2
    assert "candidate changed" in capsys.readouterr().err


@pytest.fixture(autouse=True)
def machine_identity():
    from brigade import fleet_client, node

    path = node.node_path(fleet_client.home_identity_target())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'node_id = "11111111-1111-4111-8111-111111111111"\nhostname = "example-worker"\nroles = ["worker"]\nplatform = "linux"\n'
    )


def test_mutation_binding_includes_machine_identity(tmp_path):
    from brigade.skills_cmd.install import _review_binding

    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source) == 0
    assert skills_cmd.install(workspace=tmp_path, skill="registry:security-review", harness="mcp") == 0
    binding = _review_binding(tmp_path, "security-review", "mcp", "registry:security-review")
    assert binding["node"] == "11111111-1111-4111-8111-111111111111"


def test_target_binding_is_rendered_package_used_by_install(tmp_path):
    from brigade.skills_cmd.install import _review_binding

    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source) == 0
    assert skills_cmd.install(workspace=tmp_path, skill="registry:security-review", harness="codex") == 0
    binding = _review_binding(tmp_path, "security-review", "codex", "registry:security-review")
    assert binding["target"] == binding["installed"]
    assert binding["source"] != binding["target"]
