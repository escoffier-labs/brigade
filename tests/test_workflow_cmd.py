import hashlib
import json

from brigade import cli


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _write_bad_json(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json\n")


def _write_verify_receipt(target, run_id, *, started_at, commands, status="completed"):
    command_rows = []
    for index, command in enumerate(commands, start=1):
        if isinstance(command, dict):
            row = dict(command)
            rendered = row.get("command")
        else:
            rendered = command
            row = {"command": command}
        row.setdefault("status", "completed")
        row.setdefault("exit_code", 0)
        row.setdefault("started_at", started_at)
        row.setdefault("completed_at", started_at)
        row.setdefault("stdout_log_path", str(target / f"command-{index}-out.log"))
        row.setdefault("stderr_log_path", str(target / f"command-{index}-err.log"))
        if rendered is None and isinstance(row.get("argv"), list):
            row["command"] = None
        command_rows.append(row)
    _write_json(
        target / ".brigade" / "work" / "verify-runs" / run_id / "receipt.json",
        {
            "run_id": run_id,
            "target": str(target),
            "status": status,
            "started_at": started_at,
            "completed_at": started_at,
            "path": str(target / ".brigade" / "work" / "verify-runs" / run_id),
            "commands": command_rows,
        },
    )


def _write_daily_run(target, run_id, *, started_at, commands_invoked, status="completed"):
    _write_json(
        target / ".brigade" / "daily" / "runs" / run_id / "run.json",
        {
            "run_id": run_id,
            "target": str(target),
            "status": status,
            "started_at": started_at,
            "completed_at": started_at,
            "commands_invoked": commands_invoked,
            "path": str(target / ".brigade" / "daily" / "runs" / run_id),
        },
    )


def _write_workflow_latest(target, candidates):
    _write_json(
        target / ".brigade" / "workflow" / "latest.json",
        {
            "version": 1,
            "generated_at": "2026-06-11T12:00:00+00:00",
            "target": str(target),
            "candidate_count": len(candidates),
            "candidates": candidates,
        },
    )


def _pattern_key(sequence):
    return hashlib.sha256("\n".join(sequence).encode("utf-8")).hexdigest()[:12]


def test_workflow_scan_normalizes_two_verify_receipts_into_one_sequence(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    commands_one = ["python -m pytest tests/test_alpha.py -q", "ruff check src tests"]
    commands_two = [
        {"command": "python   -m   pytest   tests/test_alpha.py   -q", "argv": ["ignored"]},
        {"command": None, "argv": ["ruff", "check", "src", "tests"]},
    ]
    _write_verify_receipt(tmp_path, "verify-one", started_at="2026-06-11T12:00:00+00:00", commands=commands_one)
    _write_verify_receipt(tmp_path, "verify-two", started_at="2026-06-11T13:00:00+00:00", commands=commands_two)

    assert cli.main(["workflow", "scan", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["candidate_count"] == 1
    assert payload["observation_count"] == 2
    assert payload["skipped_bad_json_count"] == 0
    candidate = payload["candidates"][0]
    expected_sequence = ["python -m pytest tests/test_alpha.py -q", "ruff check src tests"]
    expected_key = _pattern_key(expected_sequence)
    assert list(candidate) == [
        "id",
        "pattern_key",
        "sequence",
        "example_commands",
        "occurrence_count",
        "session_count",
        "sources",
        "ends_in_verify_pass",
        "first_seen",
        "last_seen",
        "suggested_runbook_id",
        "evidence",
        "review_risk",
        "suggested_next_command",
    ]
    assert candidate["pattern_key"] == expected_key
    assert candidate["id"] == f"workflow-{expected_key}"
    assert candidate["suggested_runbook_id"] == f"workflow-{expected_key}"
    assert candidate["sequence"] == expected_sequence
    assert candidate["example_commands"] == expected_sequence
    assert candidate["occurrence_count"] == 2
    assert candidate["session_count"] == 2
    assert candidate["sources"] == ["verify"]
    assert candidate["ends_in_verify_pass"] is True
    assert [item["run_id"] for item in candidate["evidence"]] == ["verify-one", "verify-two"]


def test_workflow_scan_default_min_count_excludes_singletons_and_min_count_one_includes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    _write_verify_receipt(
        tmp_path,
        "verify-one",
        started_at="2026-06-11T12:00:00+00:00",
        commands=["python -m pytest tests/test_alpha.py -q"],
    )

    assert cli.main(["workflow", "scan", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 0
    assert payload["observation_count"] == 1

    assert cli.main(["workflow", "scan", "--target", str(tmp_path), "--min-count", "1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["occurrence_count"] == 1


def test_workflow_scan_mines_one_daily_observation_from_full_commands_invoked(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    commands = [
        {"command": "brigade context build", "exit_code": 0},
        {"command": "brigade center report build", "exit_code": 1},
    ]
    _write_daily_run(tmp_path, "daily-one", started_at="2026-06-11T12:00:00+00:00", commands_invoked=commands)
    _write_daily_run(tmp_path, "daily-two", started_at="2026-06-11T13:00:00+00:00", commands_invoked=commands)

    assert cli.main(["workflow", "scan", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["observation_count"] == 2
    assert payload["candidate_count"] == 1
    candidate = payload["candidates"][0]
    assert candidate["sources"] == ["daily"]
    assert candidate["sequence"] == ["brigade context build", "brigade center report build"]
    assert candidate["ends_in_verify_pass"] is False
    assert candidate["evidence"][0]["command_count"] == 2


def test_workflow_import_dedupe_stays_stable_after_count_drift(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    command = "python -m pytest tests/test_alpha.py -q"
    _write_verify_receipt(tmp_path, "verify-one", started_at="2026-06-11T12:00:00+00:00", commands=[command])

    assert cli.main(["workflow", "scan", "--target", str(tmp_path), "--min-count", "1", "--import-candidates"]) == 0
    capsys.readouterr()
    imports_path = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    first = json.loads(imports_path.read_text().splitlines()[0])
    first_metadata = first["metadata"]
    first_fingerprint = first_metadata["source_fingerprint"]

    _write_verify_receipt(tmp_path, "verify-two", started_at="2026-06-12T12:00:00+00:00", commands=[command])
    assert cli.main(["workflow", "scan", "--target", str(tmp_path), "--import-candidates"]) == 0
    out = capsys.readouterr().out

    assert "imports_added: 0" in out
    assert "imports_skipped: 1" in out
    imports = [json.loads(line) for line in imports_path.read_text().splitlines()]
    assert len(imports) == 1
    latest = json.loads((tmp_path / ".brigade" / "workflow" / "latest.json").read_text())
    candidate = latest["candidates"][0]
    assert first_metadata["source_item_key"] == candidate["id"]
    assert first_metadata["source_fingerprint"] == first_fingerprint
    assert (
        first_metadata["source_fingerprint"]
        == hashlib.sha256(
            json.dumps(
                {
                    "id": candidate["id"],
                    "sequence": candidate["sequence"],
                    "suggested_runbook_id": candidate["suggested_runbook_id"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
    )
    assert candidate["occurrence_count"] == 2


def test_workflow_scan_dry_run_writes_nothing_with_parseable_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    _write_verify_receipt(
        tmp_path,
        "verify-one",
        started_at="2026-06-11T12:00:00+00:00",
        commands=["python -m pytest tests/test_alpha.py -q"],
    )

    assert cli.main(["workflow", "scan", "--target", str(tmp_path), "--min-count", "1", "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["candidate_count"] == 1
    assert payload["output"]["dry_run"] is True
    assert not (tmp_path / ".brigade" / "workflow" / "latest.json").exists()
    assert not (tmp_path / ".brigade" / "workflow" / "latest.md").exists()
    assert not (tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").exists()


def test_workflow_scan_reports_skipped_bad_json_count(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    _write_bad_json(tmp_path / ".brigade" / "work" / "verify-runs" / "bad" / "receipt.json")
    _write_verify_receipt(
        tmp_path,
        "verify-one",
        started_at="2026-06-11T12:00:00+00:00",
        commands=["python -m pytest tests/test_alpha.py -q"],
    )

    assert cli.main(["workflow", "scan", "--target", str(tmp_path), "--min-count", "1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["receipt_counts"] == {"daily": 0, "verify": 1}
    assert payload["skipped_bad_json_count"] == 1


def test_workflow_show_reads_latest_scan(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    _write_verify_receipt(
        tmp_path,
        "verify-one",
        started_at="2026-06-11T12:00:00+00:00",
        commands=["python -m pytest tests/test_alpha.py -q"],
    )
    assert cli.main(["workflow", "scan", "--target", str(tmp_path), "--min-count", "1", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(["workflow", "show", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["sources"] == ["verify"]


def test_workflow_propose_runbook_writes_policy_valid_runbook_from_candidate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    _write_workflow_latest(
        tmp_path,
        [
            {
                "id": "workflow-alpha123",
                "pattern_key": "alpha123",
                "sequence": ["rm -rf /tmp/nope"],
                "example_commands": ["python -m pytest tests/test_alpha.py -q", "ruff check src tests"],
                "occurrence_count": 2,
                "session_count": 2,
                "sources": ["verify"],
                "ends_in_verify_pass": True,
                "first_seen": "2026-06-11T12:00:00+00:00",
                "last_seen": "2026-06-11T13:00:00+00:00",
                "suggested_runbook_id": "workflow-alpha123",
                "evidence": [],
                "review_risk": "low",
                "suggested_next_command": "brigade workflow propose-runbook workflow-alpha123",
            }
        ],
    )

    assert cli.main(["workflow", "propose-runbook", "workflow-alpha123", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    runbook_path = payload["runbook_path"]
    runbook = json.loads(
        (tmp_path / ".brigade" / "workflow" / "workshop" / "workflow-alpha123" / "runbook.json").read_text()
    )

    assert payload["candidate"]["id"] == "workflow-alpha123"
    assert payload["plan"]["policy_valid"] is True
    assert runbook == {
        "id": "workflow-alpha123",
        "description": "Runbook proposed from workflow scan candidate workflow-alpha123, sources: verify.",
        "approved": False,
        "allowed_commands": ["python", "ruff"],
        "pins": [],
        "steps": [
            {
                "id": "step-1",
                "run": "python -m pytest tests/test_alpha.py -q",
                "timeout_seconds": 600,
            },
            {"id": "step-2", "run": "ruff check src tests", "timeout_seconds": 600},
        ],
    }

    assert cli.main(["runbook", "plan", runbook_path, "--target", str(tmp_path), "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["policy_valid"] is True
    assert [step["run"] for step in plan["steps"]] == [
        "python -m pytest tests/test_alpha.py -q",
        "ruff check src tests",
    ]


def test_workflow_propose_runbook_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    _write_workflow_latest(
        tmp_path,
        [
            {
                "id": "workflow-beta123",
                "suggested_runbook_id": "workflow-beta-runbook",
                "sources": ["daily"],
                "example_commands": ["python -m pytest tests/test_docs.py -q"],
            }
        ],
    )

    assert (
        cli.main(["workflow", "propose-runbook", "workflow-beta", "--target", str(tmp_path), "--dry-run", "--json"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["dry_run"] is True
    assert payload["candidate"]["id"] == "workflow-beta123"
    assert payload["runbook"]["id"] == "workflow-beta-runbook"
    assert payload["plan"]["policy_valid"] is True
    assert not (tmp_path / ".brigade" / "workflow" / "workshop" / "workflow-beta-runbook" / "runbook.json").exists()


def test_workflow_propose_runbook_unknown_prefix_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    _write_workflow_latest(
        tmp_path,
        [{"id": "workflow-alpha123", "suggested_runbook_id": "workflow-alpha123", "example_commands": ["python -V"]}],
    )

    assert cli.main(["workflow", "propose-runbook", "workflow-missing", "--target", str(tmp_path)]) == 2

    assert "workflow candidate not found: workflow-missing" in capsys.readouterr().err


def test_workflow_propose_runbook_ambiguous_prefix_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    _write_workflow_latest(
        tmp_path,
        [
            {"id": "workflow-dupe111", "suggested_runbook_id": "workflow-dupe111", "example_commands": ["python -V"]},
            {
                "id": "workflow-dupe222",
                "suggested_runbook_id": "workflow-dupe222",
                "example_commands": ["python -m pytest -q"],
            },
        ],
    )

    assert cli.main(["workflow", "propose-runbook", "workflow-dupe", "--target", str(tmp_path)]) == 2

    assert "workflow candidate id is ambiguous: workflow-dupe" in capsys.readouterr().err


def test_workflow_scan_templates_volatile_tokens_into_stable_pattern(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    commands_one = [
        "brigade work verify show 20260611-120000-work-verify-a1b2c3",
        "cat /tmp/claude-scratch/run-20260611-120000-abcdef123456/out.log",
        "git log --since 2026-06-11 --format=%H deadbeefcafe1234",
    ]
    commands_two = [
        "brigade work verify show 20260612-090100-work-verify-9f8e7d",
        "cat /tmp/other-scratch/run-20260612-090100-fedcba654321/out.log",
        "git log --since 2026-06-12 --format=%H 1234cafedeadbeef",
    ]
    _write_verify_receipt(tmp_path, "verify-one", started_at="2026-06-11T12:00:00+00:00", commands=commands_one)
    _write_verify_receipt(tmp_path, "verify-two", started_at="2026-06-12T09:01:00+00:00", commands=commands_two)

    assert cli.main(["workflow", "scan", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    expected_sequence = [
        "brigade work verify show <run-id>",
        "cat <path>",
        "git log --since <date> --format=%H <hex>",
    ]
    assert payload["candidate_count"] == 1
    candidate = payload["candidates"][0]
    assert candidate["sequence"] == expected_sequence
    assert candidate["pattern_key"] == _pattern_key(expected_sequence)
    assert candidate["id"] == f"workflow-{_pattern_key(expected_sequence)}"
    assert candidate["occurrence_count"] == 2
    assert candidate["example_commands"] == commands_two


def test_workflow_scan_review_risk_follows_runbook_denylist(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    risky = ["git reset --hard origin/main", "python -m pytest -q"]
    safe = ["python -m pytest -q", "ruff check src tests"]
    _write_verify_receipt(tmp_path, "risky-one", started_at="2026-06-11T12:00:00+00:00", commands=risky)
    _write_verify_receipt(tmp_path, "risky-two", started_at="2026-06-11T13:00:00+00:00", commands=risky)
    _write_verify_receipt(tmp_path, "safe-one", started_at="2026-06-11T14:00:00+00:00", commands=safe)
    _write_verify_receipt(tmp_path, "safe-two", started_at="2026-06-11T15:00:00+00:00", commands=safe)

    assert cli.main(["workflow", "scan", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    risk_by_sequence = {tuple(item["sequence"]): item["review_risk"] for item in payload["candidates"]}
    assert risk_by_sequence[tuple(risky)] == "high"
    assert risk_by_sequence[tuple(safe)] == "normal"


def test_workflow_propose_runbook_rejects_policy_invalid_candidate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    _write_workflow_latest(
        tmp_path,
        [
            {
                "id": "workflow-danger01",
                "pattern_key": "danger01",
                "sequence": ["rm -rf <path>"],
                "example_commands": ["rm -rf build"],
                "occurrence_count": 2,
                "session_count": 2,
                "sources": ["verify"],
                "ends_in_verify_pass": True,
                "first_seen": "2026-06-11T12:00:00+00:00",
                "last_seen": "2026-06-11T13:00:00+00:00",
                "suggested_runbook_id": "workflow-danger01",
                "evidence": [],
                "review_risk": "high",
                "suggested_next_command": "brigade workflow propose-runbook workflow-danger01",
            }
        ],
    )

    rc = cli.main(["workflow", "propose-runbook", "workflow-danger01", "--target", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "policy" in captured.err
    runbook_path = tmp_path / ".brigade" / "workflow" / "workshop" / "workflow-danger01" / "runbook.json"
    assert not runbook_path.exists()
