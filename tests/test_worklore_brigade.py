# Synthetic rejection fixtures retain private paths to exercise redaction.
# content-guard: allow home-path file
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from brigade import cli
from brigade import worklore_brigade as adapter
from brigade import worklore_brigade_sync as sync
from brigade.cli import fleet as fleet_cli
from tests.support import PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE, assert_private_mode

IDENTITY = "escoffier-labs/brigade"


def _pending_task(**overrides: object) -> dict[str, object]:
    task: dict[str, object] = {
        "id": "abc123",
        "text": "Add Worklore store",
        "status": "pending",
        "priority": "high",
        "acceptance": ["pytest tests/test_worklore_store.py passes"],
        "metadata": {
            "verify_run_id": "run-xyz",
            "receipt_id": "rec-1",
            "run_path": "/var/lib/brigade/targets/brigade/.brigade/runs/run-xyz",
        },
    }
    task.update(overrides)
    return task


def test_normalize_pending_task_strips_paths_and_keeps_acceptance() -> None:
    observation = adapter.normalize_task(
        _pending_task(),
        identity=IDENTITY,
        edges=[{"source": "abc123", "target": "def456", "type": "blocks"}],
    )
    assert observation["external_key"] == "escoffier-labs/brigade#abc123"
    assert observation["link_type"] == "brigade"
    assert observation["title"] == "Add Worklore store"
    assert "description" not in observation
    assert observation["acceptance"] == ["pytest tests/test_worklore_store.py passes"]
    assert observation["external_state"] == "pending"
    assert observation["priority"] == "high"
    assert observation["dependencies"] == [{"type": "blocks", "id": "def456"}]
    assert observation["evidence_refs"] == ["run-xyz", "rec-1"]
    assert observation["source_policy"] == "eligible"
    assert observation["proposed_status"] is None
    dumped = json.dumps(observation)
    assert "/var/lib" not in dumped
    assert "tasks_path" not in observation
    assert "status" not in observation
    assert "transition" not in observation


def test_title_is_trimmed_and_capped_at_240() -> None:
    observation = adapter.normalize_task(
        _pending_task(text=f"  {'x' * 300}  "),
        identity=IDENTITY,
        edges=[],
    )
    assert observation["title"] == "x" * 240
    assert len(observation["title"]) == 240


def test_done_task_proposes_completed_without_transition_fields() -> None:
    observation = adapter.normalize_task(
        {
            "id": "done-1",
            "text": "Finished",
            "status": "done",
            "acceptance": ["shipped"],
            "metadata": {},
        },
        identity=IDENTITY,
        edges=[],
    )
    assert observation["source_policy"] == "completed"
    assert observation["proposed_status"] == "completed"
    assert observation["external_state"] == "done"
    assert "status" not in observation
    assert "transition" not in observation


@pytest.mark.parametrize("status", ["cancelled", "dismissed"])
def test_cancelled_and_dismissed_complete_source_without_claiming_success(status: str) -> None:
    observation = adapter.normalize_task(
        {
            "id": "term-1",
            "text": "Stopped",
            "status": status,
            "acceptance": ["closed out"],
            "metadata": {},
        },
        identity=IDENTITY,
        edges=[],
    )
    assert observation["source_policy"] == "completed"
    assert observation["proposed_status"] is None
    assert observation["external_state"] == status
    assert "status" not in observation
    assert "transition" not in observation


def test_dependencies_keep_outgoing_edges_and_dedupe() -> None:
    observation = adapter.normalize_task(
        _pending_task(),
        identity=IDENTITY,
        edges=[
            {"source": "other", "target": "abc123", "type": "blocks"},
            {"source": "abc123", "target": "def456", "type": "blocks"},
            {"source": "abc123", "target": "def456", "type": "blocks"},
            {"source": "abc123", "target": "ghi789", "type": "parent-child"},
            {"source": "abc123", "target": "def456", "type": "discovered-from"},
        ],
    )
    assert observation["dependencies"] == [
        {"type": "blocks", "id": "def456"},
        {"type": "discovered-from", "id": "def456"},
        {"type": "parent-child", "id": "ghi789"},
    ]


def test_normalize_task_sorts_deduped_dependencies_by_type_and_id() -> None:
    edges = [
        {"source": "abc123", "target": "ghi789", "type": "parent-child"},
        {"source": "abc123", "target": "def456", "type": "discovered-from"},
        {"source": "abc123", "target": "aaa000", "type": "blocks"},
        {"source": "abc123", "target": "def456", "type": "blocks"},
        {"source": "abc123", "target": "ghi789", "type": "parent-child"},
    ]
    reversed_edges = list(reversed(edges))
    first = adapter.normalize_task(
        _pending_task(acceptance=["second-check", "first-check"]),
        identity=IDENTITY,
        edges=edges,
    )
    second = adapter.normalize_task(
        _pending_task(acceptance=["second-check", "first-check"]),
        identity=IDENTITY,
        edges=reversed_edges,
    )
    expected = [
        {"type": "blocks", "id": "aaa000"},
        {"type": "blocks", "id": "def456"},
        {"type": "discovered-from", "id": "def456"},
        {"type": "parent-child", "id": "ghi789"},
    ]
    assert first["dependencies"] == expected
    assert second["dependencies"] == expected
    assert first["acceptance"] == ["second-check", "first-check"]
    assert second["acceptance"] == ["second-check", "first-check"]


def test_malformed_and_path_shaped_dependencies_are_dropped_without_leaking() -> None:
    observation = adapter.normalize_task(
        _pending_task(),
        identity=IDENTITY,
        edges=[
            {"source": "abc123", "target": "/var/lib/secret-task", "type": "blocks"},
            {"source": "abc123", "target": r"C:\Users\operator\task", "type": "blocks"},
            {"source": "abc123", "target": "ok-dep", "type": "/home/operator/type"},
            {"source": "abc123", "target": "bad#id", "type": "blocks"},
            {"source": "abc123", "target": "has/slash", "type": "blocks"},
            {"source": "abc123", "target": "has\\slash", "type": "blocks"},
            {"source": "abc123", "target": "has space", "type": "blocks"},
            {"source": "abc123", "target": "keep-me", "type": "blocks"},
            {"source": "abc123", "type": "blocks"},
            "not-an-edge",
        ],
    )
    dumped = json.dumps(observation)
    assert observation["dependencies"] == [{"type": "blocks", "id": "keep-me"}]
    assert "/var/lib" not in dumped
    assert "/home/" not in dumped
    assert "C:\\Users\\" not in dumped
    assert "secret-task" not in dumped


def test_normalize_rejects_home_paths_and_receipt_bodies() -> None:
    with pytest.raises(adapter.BrigadeObservationError, match="unknown-field"):
        adapter.normalize_task(
            {
                "id": "bad",
                "text": "Nope",
                "status": "pending",
                "acceptance": ["x"],
                "receipt_body": {"stdout": "secret"},
            },
            identity=IDENTITY,
            edges=[],
        )
    with pytest.raises(adapter.BrigadeObservationError, match="private-data"):
        adapter.normalize_task(
            {
                "id": "bad2",
                "text": "See /home/operator/secret",
                "status": "pending",
                "acceptance": ["x"],
            },
            identity=IDENTITY,
            edges=[],
        )
    with pytest.raises(adapter.BrigadeObservationError, match="private-data"):
        adapter.normalize_task(
            {
                "id": "bad3",
                "text": r"notes in C:\Users\operator",
                "status": "pending",
                "acceptance": ["x"],
            },
            identity=IDENTITY,
            edges=[],
        )
    with pytest.raises(adapter.BrigadeObservationError, match="private-data"):
        adapter.normalize_task(
            {
                "id": "bad4",
                "text": "Safe title",
                "status": "pending",
                "acceptance": ["see /home/operator/secret"],
            },
            identity=IDENTITY,
            edges=[],
        )
    with pytest.raises(adapter.BrigadeObservationError, match="private-data"):
        adapter.normalize_task(
            {
                "id": "bad5",
                "text": "Safe title",
                "status": "pending",
                "acceptance": [r"notes in C:\Users\operator"],
            },
            identity=IDENTITY,
            edges=[],
        )


@pytest.mark.parametrize(
    "blocked_key",
    ["receipt_body", "prompt", "transcript", "token", "secret", "password", "tasks_path"],
)
def test_blocked_keys_are_rejected_at_top_level_and_nested(blocked_key: str) -> None:
    with pytest.raises(adapter.BrigadeObservationError, match="unknown-field"):
        adapter.normalize_task(_pending_task(**{blocked_key: "leak"}), identity=IDENTITY, edges=[])
    with pytest.raises(adapter.BrigadeObservationError, match="unknown-field"):
        adapter.normalize_task(
            _pending_task(metadata={"verify_run_id": "run-xyz", blocked_key: "leak"}),
            identity=IDENTITY,
            edges=[],
        )


def test_unselected_metadata_is_dropped_and_never_appears_in_json() -> None:
    observation = adapter.normalize_task(
        _pending_task(
            metadata={
                "verify_run_id": "run-xyz",
                "receipt_id": "rec-1",
                "footprint": {"files": ["/var/lib/brigade/src/brigade/worklore_store.py"]},
                "run_path": "/var/lib/brigade/targets/brigade/.brigade/runs/run-xyz",
                "session_path": "/var/lib/brigade/targets/brigade/.brigade/sessions/s1",
                "files": ["/var/lib/brigade/src/a.py"],
                "symbols": ["brigade.worklore_store._parse_observation"],
            }
        ),
        identity=IDENTITY,
        edges=[],
    )
    dumped = json.dumps(observation)
    assert "footprint" not in dumped
    assert "run_path" not in dumped
    assert "session_path" not in dumped
    assert "files" not in dumped
    assert "symbols" not in dumped
    assert "/var/lib" not in dumped
    assert "receipt_body" not in dumped
    assert observation["evidence_refs"] == ["run-xyz", "rec-1"]


def test_invalid_evidence_refs_are_omitted_without_receipt_bodies() -> None:
    observation = adapter.normalize_task(
        _pending_task(
            metadata={
                "verify_run_id": "/var/lib/brigade/runs/run-xyz",
                "receipt_id": "ok-receipt",
                "receipt_path": "/var/lib/brigade/receipts/rec.json",
            }
        ),
        identity=IDENTITY,
        edges=[],
    )
    dumped = json.dumps(observation)
    assert observation["evidence_refs"] == ["ok-receipt"]
    assert "/var/lib" not in dumped
    assert "receipt_body" not in dumped
    assert "stdout" not in dumped


def test_campaign_qualified_task_ids_are_accepted() -> None:
    observation = adapter.normalize_task(
        _pending_task(id="brigade:20260829-120000-add-store-abc123"),
        identity=IDENTITY,
        edges=[
            {
                "source": "brigade:20260829-120000-add-store-abc123",
                "target": "opsdeck:def456",
                "type": "blocks",
            }
        ],
    )
    assert observation["external_key"] == "escoffier-labs/brigade#brigade:20260829-120000-add-store-abc123"
    assert observation["dependencies"] == [{"type": "blocks", "id": "opsdeck:def456"}]


@pytest.mark.parametrize(
    "task_id",
    ["", "#hash", "has/slash", r"has\slash", "has space", "/abs/path", r"C:\Users\task"],
)
def test_invalid_task_ids_are_rejected(task_id: str) -> None:
    with pytest.raises(adapter.BrigadeObservationError, match="field-bound"):
        adapter.normalize_task(_pending_task(id=task_id), identity=IDENTITY, edges=[])


@pytest.mark.parametrize("identity", ["", "x", "/abs/path", "has space", "#bad"])
def test_invalid_identities_are_rejected(identity: str) -> None:
    with pytest.raises(adapter.BrigadeObservationError, match="field-bound"):
        adapter.normalize_task(_pending_task(), identity=identity, edges=[])


def test_acceptance_follows_live_worklore_bounds() -> None:
    observation = adapter.normalize_task(
        _pending_task(acceptance=["ok"]),
        identity=IDENTITY,
        edges=[],
    )
    assert observation["acceptance"] == ["ok"]
    with pytest.raises(adapter.BrigadeObservationError, match="field-bound"):
        adapter.normalize_task(_pending_task(acceptance=["x"] * 21), identity=IDENTITY, edges=[])
    with pytest.raises(adapter.BrigadeObservationError, match="field-bound"):
        adapter.normalize_task(_pending_task(acceptance=[""]), identity=IDENTITY, edges=[])
    with pytest.raises(adapter.BrigadeObservationError, match="field-bound"):
        adapter.normalize_task(_pending_task(acceptance=["x" * 401]), identity=IDENTITY, edges=[])


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_brigade_fleet(home: Path, targets: list[tuple[str, str]]) -> None:
    rows = ",\n".join(
        f"  {{ path = {_toml_string(path)}, identity = {_toml_string(identity)} }}" for path, identity in targets
    )
    (home / "fleet.toml").write_text(
        f"[fleet.worklore.brigade]\ntargets = [\n{rows}\n]\n",
        encoding="utf-8",
    )


def _assert_bounded_sync_error(
    exc: BaseException,
    *,
    code: str,
    forbidden: list[str],
) -> None:
    message = str(exc)
    assert getattr(exc, "code", None) == code
    assert code in message
    for item in forbidden:
        assert item not in message
    assert "/home/" not in message
    assert "C:\\Users\\" not in message


def test_load_config_requires_explicit_targets(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "fleet.toml").write_text("[fleet.worklore.brigade]\n")
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    with pytest.raises(sync.BrigadeSyncError, match="targets") as excinfo:
        sync.load_brigade_config()
    _assert_bounded_sync_error(excinfo.value, code="field-bound", forbidden=[str(home)])


def test_load_config_reads_explicit_target_tables(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    _write_brigade_fleet(home, [(str(repo), IDENTITY)])
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    config = sync.load_brigade_config()
    assert list(config) == ["targets"]
    assert len(config["targets"]) == 1
    target = config["targets"][0]
    assert isinstance(target, sync.Target)
    assert target.path == repo.resolve()
    assert target.identity == IDENTITY


def test_load_config_keeps_shared_identity_across_different_paths(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    first = tmp_path / "node-a"
    second = tmp_path / "node-b"
    home.mkdir()
    first.mkdir()
    second.mkdir()
    _write_brigade_fleet(home, [(str(first), IDENTITY), (str(second), IDENTITY)])
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    config = sync.load_brigade_config()
    assert [(target.path, target.identity) for target in config["targets"]] == [
        (first.resolve(), IDENTITY),
        (second.resolve(), IDENTITY),
    ]


def test_load_config_rejects_exact_duplicate_path_and_identity(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    _write_brigade_fleet(home, [(str(repo), IDENTITY), (str(repo), IDENTITY)])
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    with pytest.raises(sync.BrigadeSyncError, match="field-bound") as excinfo:
        sync.load_brigade_config()
    _assert_bounded_sync_error(excinfo.value, code="field-bound", forbidden=[str(repo), str(home)])


@pytest.mark.parametrize(
    "writer",
    [
        lambda home, _repo: None,
        lambda home, _repo: (home / "fleet.toml").write_text("fleet = [", encoding="utf-8"),
        lambda home, _repo: (home / "fleet.toml").write_text("[fleet.worklore.brigade]\n", encoding="utf-8"),
        lambda home, _repo: (home / "fleet.toml").write_text(
            "[fleet.worklore.brigade]\ntargets = []\n",
            encoding="utf-8",
        ),
        lambda home, _repo: (home / "fleet.toml").write_text(
            '[fleet.worklore.brigade]\ntargets = "escoffier-labs/brigade"\n',
            encoding="utf-8",
        ),
    ],
)
def test_load_config_missing_and_malformed_toml_are_field_bound(tmp_path, monkeypatch, writer) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    writer(home, repo)
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    with pytest.raises(sync.BrigadeSyncError, match="field-bound") as excinfo:
        sync.load_brigade_config()
    _assert_bounded_sync_error(excinfo.value, code="field-bound", forbidden=[str(home), str(repo)])


@pytest.mark.parametrize(
    "body",
    [
        'targets = ["escoffier-labs/brigade"]\n',
        "targets = [{ identity = " + f'"{IDENTITY}"' + " }]\n",
        "targets = [{ path = 12, identity = " + f'"{IDENTITY}"' + " }]\n",
        "targets = [{ path = " + '""' + f', identity = "{IDENTITY}"' + " }]\n",
        "targets = [{ path = " + f'"/missing-brigade-target", identity = "{IDENTITY}"' + " }]\n",
    ],
)
def test_load_config_malformed_and_missing_paths_are_field_bound(tmp_path, monkeypatch, body: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "fleet.toml").write_text("[fleet.worklore.brigade]\n" + body, encoding="utf-8")
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    with pytest.raises(sync.BrigadeSyncError, match="field-bound") as excinfo:
        sync.load_brigade_config()
    _assert_bounded_sync_error(
        excinfo.value,
        code="field-bound",
        forbidden=[str(home), "/missing-brigade-target"],
    )


def test_load_config_non_directory_path_is_field_bound(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    target_file = tmp_path / "not-a-dir"
    home.mkdir()
    target_file.write_text("nope", encoding="utf-8")
    _write_brigade_fleet(home, [(str(target_file), IDENTITY)])
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    with pytest.raises(sync.BrigadeSyncError, match="field-bound") as excinfo:
        sync.load_brigade_config()
    _assert_bounded_sync_error(excinfo.value, code="field-bound", forbidden=[str(target_file), str(home)])


@pytest.mark.parametrize("identity", ["", "x", "/abs/path", "has space", "#bad"])
def test_load_config_invalid_identity_is_field_bound(tmp_path, monkeypatch, identity: str) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    _write_brigade_fleet(home, [(str(repo), identity)])
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    with pytest.raises(sync.BrigadeSyncError, match="field-bound") as excinfo:
        sync.load_brigade_config()
    leaked = [str(repo), str(home)]
    if identity:
        leaked.append(identity)
    _assert_bounded_sync_error(excinfo.value, code="field-bound", forbidden=leaked)


def test_read_tasks_uses_public_command_and_drops_tasks_path(tmp_path, monkeypatch) -> None:
    target = tmp_path / "repo"
    target.mkdir()
    recorded: dict[str, object] = {}
    payload = {
        "tasks_path": str(target / ".brigade" / "work" / "tasks.json"),
        "ready": [{"id": "abc123"}],
        "tasks": [
            {
                "id": "abc123",
                "text": "Add store",
                "status": "pending",
                "acceptance": ["tests pass"],
                "metadata": {},
            }
        ],
        "edges": [{"source": "abc123", "target": "def456", "type": "blocks"}],
        "extra": "ignore-me",
    }

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    tasks, edges = sync.read_public_tasks(target)
    assert recorded["argv"] == ["brigade", "work", "tasks", "--target", str(target), "--all", "--json"]
    kwargs = recorded["kwargs"]
    assert kwargs["timeout"] == 30
    assert kwargs["text"] is True
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["check"] is False
    assert kwargs.get("shell", False) is False
    assert tasks[0]["id"] == "abc123"
    assert edges[0]["source"] == "abc123"
    dumped = json.dumps({"tasks": tasks, "edges": edges})
    assert "tasks_path" not in dumped
    assert "ready" not in dumped
    assert "extra" not in dumped
    assert str(target / ".brigade" / "work" / "tasks.json") not in dumped


@pytest.mark.parametrize(
    "fake_run",
    [
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            2,
            stdout="token=abc prompt=secret",
            stderr="authorization bearer /home/operator Add store",
        ),
        lambda argv, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 30)),
        lambda argv, **kwargs: (_ for _ in ()).throw(OSError("Permission denied: /home/operator/secret")),
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="not-json", stderr="tasks_path=/tmp/x"),
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="[]", stderr=""),
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout='{"edges":[]}', stderr=""),
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"tasks":{},"edges":[]}',
            stderr="",
        ),
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"tasks":[],"edges":{}}',
            stderr="",
        ),
    ],
)
def test_read_tasks_failures_are_retryable_and_bounded(tmp_path, monkeypatch, fake_run) -> None:
    target = tmp_path / "repo"
    target.mkdir()
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    with pytest.raises(sync.BrigadeSyncError, match="retryable-adapter-failure") as excinfo:
        sync.read_public_tasks(target)
    _assert_bounded_sync_error(
        excinfo.value,
        code="retryable-adapter-failure",
        forbidden=[
            str(target),
            "token",
            "prompt",
            "secret",
            "authorization",
            "bearer",
            "Add store",
            "not-json",
            "stdout",
            "stderr",
            "tasks_path",
            "Permission denied",
            "TimeoutExpired",
        ],
    )


def test_two_nodes_same_identity_share_one_external_key() -> None:
    task = {
        "id": "abc123",
        "text": "Shared",
        "status": "pending",
        "acceptance": ["ok"],
        "metadata": {},
    }
    first = sync.observations_for_target(task, edges=[], identity=IDENTITY)
    second = sync.observations_for_target(task, edges=[], identity=IDENTITY)
    assert first[0]["external_key"] == second[0]["external_key"] == f"{IDENTITY}#abc123"


def test_observations_for_target_normalizes_one_or_many_tasks() -> None:
    first = _pending_task()
    second = _pending_task(id="def456", text="Second")
    edges = [{"source": "abc123", "target": "def456", "type": "blocks"}]
    single = sync.observations_for_target(first, edges=edges, identity=IDENTITY)
    many = sync.observations_for_target([first, second], edges=edges, identity=IDENTITY)
    assert single == [adapter.normalize_task(first, identity=IDENTITY, edges=edges)]
    assert many == [
        adapter.normalize_task(first, identity=IDENTITY, edges=edges),
        adapter.normalize_task(second, identity=IDENTITY, edges=edges),
    ]


def test_read_tasks_uses_resolved_absolute_path_from_relative_cwd(tmp_path, monkeypatch) -> None:
    target = tmp_path / "repo"
    other = tmp_path / "other"
    target.mkdir()
    other.mkdir()
    monkeypatch.chdir(other)
    recorded: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout='{"tasks":[],"edges":[]}', stderr="")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    relative = Path("..") / "repo"
    assert not relative.is_absolute()
    tasks, edges = sync.read_public_tasks(relative)
    resolved = str(target.resolve())
    assert recorded["argv"] == ["brigade", "work", "tasks", "--target", resolved, "--all", "--json"]
    assert Path(recorded["argv"][4]).is_absolute()
    assert recorded["argv"][4] == resolved
    assert tasks == []
    assert edges == []


@pytest.mark.parametrize(
    "payload",
    [
        {"tasks": ["Add store /home/operator/secret"], "edges": []},
        {"tasks": [12], "edges": []},
        {"tasks": [None], "edges": []},
        {"tasks": [], "edges": ["not-an-edge /var/lib/secret-task"]},
        {"tasks": [], "edges": [{"target": "def456", "type": "blocks"}]},
        {"tasks": [], "edges": [{"source": "abc123", "type": "blocks"}]},
        {"tasks": [], "edges": [{"source": "abc123", "target": "def456"}]},
        {"tasks": [], "edges": [{"source": "", "target": "def456", "type": "blocks"}]},
        {"tasks": [], "edges": [{"source": "abc123", "target": "", "type": "blocks"}]},
        {"tasks": [], "edges": [{"source": "abc123", "target": "def456", "type": ""}]},
        {"tasks": [], "edges": [{"source": "   ", "target": "def456", "type": "blocks"}]},
        {"tasks": [], "edges": [{"source": 1, "target": "def456", "type": "blocks"}]},
        {
            "tasks": [{"id": "abc123", "text": "Add store /tmp/secret-task"}],
            "edges": [{"source": "abc123", "target": None, "type": "blocks"}],
        },
    ],
)
def test_read_tasks_malformed_items_are_retryable_and_bounded(tmp_path, monkeypatch, payload) -> None:
    target = tmp_path / "repo"
    target.mkdir()

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    with pytest.raises(sync.BrigadeSyncError, match="retryable-adapter-failure") as excinfo:
        sync.read_public_tasks(target)
    _assert_bounded_sync_error(
        excinfo.value,
        code="retryable-adapter-failure",
        forbidden=[
            str(target),
            "Add store",
            "secret-task",
            "not-an-edge",
            "/tmp/",
            "/var/lib",
            "abc123",
            "def456",
            "blocks",
        ],
    )


@pytest.mark.parametrize("payload", ["not-a-task", 12, None, ("tuple",)])
def test_observations_for_target_rejects_non_mapping_non_list(payload) -> None:
    with pytest.raises(sync.BrigadeSyncError) as excinfo:
        sync.observations_for_target(payload, edges=[], identity=IDENTITY)
    _assert_bounded_sync_error(
        excinfo.value,
        code=excinfo.value.code,
        forbidden=["not-a-task", "tuple", "TypeError"],
    )
    assert excinfo.value.code in {"field-bound", "retryable-adapter-failure"}


UNSAFE_TASKS = [
    _pending_task(text="See /home/operator/secret"),
    {
        "id": "bad",
        "text": "Nope",
        "status": "pending",
        "acceptance": ["x"],
        "receipt_body": {"stdout": "secret-token"},
    },
]
UNSAFE_TASK_LEAKS = [
    "See /home/operator/secret",
    "receipt_body",
    "secret-token",
    "Nope",
]


@pytest.mark.parametrize("task", UNSAFE_TASKS)
def test_observations_for_target_skips_and_counts_unsafe_tasks_without_leaking(task) -> None:
    observations = sync.observations_for_target(task, edges=[], identity=IDENTITY)
    assert observations == []
    assert observations.skipped == 1
    dumped = json.dumps({"observations": list(observations), "skipped": observations.skipped})
    for fragment in UNSAFE_TASK_LEAKS:
        assert fragment not in dumped


def test_observations_for_target_keeps_healthy_tasks_beside_a_skipped_one() -> None:
    observations = sync.observations_for_target(
        [UNSAFE_TASKS[0], _pending_task(id="healthy", text="Add Worklore store")],
        edges=[],
        identity=IDENTITY,
    )
    assert [obs["external_key"] for obs in observations] == [f"{IDENTITY}#healthy"]
    assert observations.skipped == 1


FINGERPRINT_FIELDS = (
    "acceptance",
    "dependencies",
    "evidence_refs",
    "external_key",
    "external_state",
    "external_updated_at",
    "priority",
    "proposed_status",
    "source_policy",
    "title",
)
OTHER_IDENTITY = "other-org/other-repo"


def _public_task(**overrides: object) -> dict[str, object]:
    task: dict[str, object] = {
        "id": "abc123",
        "text": "Shared",
        "status": "pending",
        "priority": "high",
        "acceptance": ["ok"],
        "metadata": {},
    }
    task.update(overrides)
    return task


def _fingerprint(observation: dict[str, object]) -> str:
    payload = {field: observation.get(field) for field in FINGERPRINT_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _batch_key(identity: str, observations: list[dict[str, object]]) -> str:
    fingerprints = sorted(_fingerprint(item) for item in observations)
    digest = hashlib.sha256(
        json.dumps(fingerprints, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:24]
    return f"brigade:{identity}:{digest}"


def _recording_hub(posts: list[dict[str, object]], seen: dict[str, bool] | None = None):
    store = {} if seen is None else seen

    def fake_import_batch(payload):
        posts.append(payload)
        created = 0
        updated = 0
        for obs in payload["observations"]:
            key = str(obs["external_key"])
            if key in store:
                updated += 1
            else:
                store[key] = True
                created += 1
        return {"created": created, "updated": updated, "unchanged": 0}

    return fake_import_batch


def _install_public_reader(monkeypatch, payloads: dict[Path, tuple[list[object], list[object]]]) -> list[object]:
    read_targets: list[object] = []

    def fake_read(target):
        read_targets.append(target)
        path = target.path if isinstance(target, sync.Target) else Path(target)
        return payloads[path]

    monkeypatch.setattr(sync, "read_public_tasks", fake_read)
    return read_targets


def _assert_no_hub_leak(message: str) -> None:
    lowered = message.lower()
    for fragment in (
        "https://",
        "http://",
        "token",
        "authorization",
        "bearer",
        "add store",
        "shared",
        "/work/imports",
        "payload",
        "exception",
        "permission denied",
    ):
        assert fragment not in lowered


def test_sync_reads_every_target_and_does_not_write_local_ledger(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    normalized: list[tuple[str, list[dict[str, object]]]] = []
    node_a = tmp_path / "node-a"
    node_b = tmp_path / "node-b"
    node_a.mkdir()
    node_b.mkdir()
    ledger_a = node_a / ".brigade" / "work" / "tasks.json"
    ledger_b = node_b / ".brigade" / "work" / "tasks.json"
    ledger_a.parent.mkdir(parents=True)
    ledger_b.parent.mkdir(parents=True)
    original_a = '{"tasks":[{"id":"local-a"}]}'
    original_b = '{"tasks":[{"id":"local-b"}]}'
    ledger_a.write_text(original_a, encoding="utf-8")
    ledger_b.write_text(original_b, encoding="utf-8")
    before_a = {path: path.stat().st_mtime_ns for path in node_a.rglob("*")}
    before_b = {path: path.stat().st_mtime_ns for path in node_b.rglob("*")}
    read_targets = _install_public_reader(
        monkeypatch,
        {
            node_a: ([_public_task()], []),
            node_b: ([_public_task()], []),
        },
    )
    real_observations = sync.observations_for_target

    def spy_observations(tasks, *, edges=None, identity):
        result = real_observations(tasks, edges=edges, identity=identity)
        normalized.append((identity, result))
        return result

    monkeypatch.setattr(sync, "observations_for_target", spy_observations)
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    result = sync.sync_brigade(
        targets=[
            sync.Target(path=node_a, identity=IDENTITY),
            sync.Target(path=node_b, identity=IDENTITY),
        ]
    )
    assert [target.path for target in read_targets] == [node_a, node_b]
    assert [identity for identity, _obs in normalized] == [IDENTITY, IDENTITY]
    assert ledger_a.read_text(encoding="utf-8") == original_a
    assert ledger_b.read_text(encoding="utf-8") == original_b
    assert {path: path.stat().st_mtime_ns for path in node_a.rglob("*")} == before_a
    assert {path: path.stat().st_mtime_ns for path in node_b.rglob("*")} == before_b
    assert result["targets"] == 2
    assert result["identities"] == 1


def test_sync_merges_targets_that_share_identity(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    (tmp_path / "node-a").mkdir()
    (tmp_path / "node-b").mkdir()
    _install_public_reader(
        monkeypatch,
        {
            tmp_path / "node-a": ([_public_task()], []),
            tmp_path / "node-b": ([_public_task()], []),
        },
    )
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    result = sync.sync_brigade(
        targets=[
            sync.Target(path=tmp_path / "node-a", identity=IDENTITY),
            sync.Target(path=tmp_path / "node-b", identity=IDENTITY),
        ]
    )
    assert len(posts) == 1
    assert posts[0]["source_type"] == "brigade"
    assert posts[0]["adapter_id"] == f"brigade:{IDENTITY}"
    assert set(posts[0]) == {"adapter_id", "source_type", "idempotency_key", "observations"}
    assert [obs["external_key"] for obs in posts[0]["observations"]] == [f"{IDENTITY}#abc123"]
    assert result["created"] == 1
    assert result["posted"] == 1
    assert result["observation_count"] == 1
    assert result["identities"] == 1


def test_sync_two_nodes_post_same_external_key(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    seen: dict[str, bool] = {}
    (tmp_path / "node-a").mkdir()
    (tmp_path / "node-b").mkdir()
    _install_public_reader(
        monkeypatch,
        {
            tmp_path / "node-a": ([_public_task()], []),
            tmp_path / "node-b": ([_public_task()], []),
        },
    )
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts, seen))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    first = sync.sync_brigade(targets=[sync.Target(path=tmp_path / "node-a", identity=IDENTITY)])
    second = sync.sync_brigade(targets=[sync.Target(path=tmp_path / "node-b", identity=IDENTITY)])
    keys = [obs["external_key"] for payload in posts for obs in payload["observations"]]
    assert keys == [f"{IDENTITY}#abc123", f"{IDENTITY}#abc123"]
    assert posts[0]["idempotency_key"] == posts[1]["idempotency_key"]
    assert posts[0]["idempotency_key"] == _batch_key(IDENTITY, posts[0]["observations"])
    assert first["created"] == 1
    assert second["created"] == 0
    assert second["updated"] == 1
    receipt = tmp_path / "worklore" / "brigade" / "escoffier-labs-brigade.json"
    assert json.loads(receipt.read_text(encoding="utf-8"))["last_error_code"] is None
    assert_private_mode(receipt, PRIVATE_FILE_MODE)


@pytest.mark.parametrize("order", [("node-a", "node-b"), ("node-b", "node-a")])
def test_identity_conflict_fails_closed_before_hub_regardless_of_order(
    tmp_path, monkeypatch, order: tuple[str, str]
) -> None:
    posts: list[dict[str, object]] = []
    first_name, second_name = order
    first = tmp_path / first_name
    second = tmp_path / second_name
    first.mkdir()
    second.mkdir()
    payloads = {
        tmp_path / "node-a": ([_public_task(text="Alpha title")], []),
        tmp_path / "node-b": ([_public_task(text="Beta title")], []),
    }
    _install_public_reader(monkeypatch, payloads)
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.BrigadeSyncError, match="identity-conflict") as excinfo:
        sync.sync_brigade(
            targets=[
                sync.Target(path=first, identity=IDENTITY),
                sync.Target(path=second, identity=IDENTITY),
            ]
        )
    _assert_bounded_sync_error(
        excinfo.value,
        code="identity-conflict",
        forbidden=[
            IDENTITY,
            "abc123",
            f"{IDENTITY}#abc123",
            "Alpha title",
            "Beta title",
            str(first),
            str(second),
            "payload",
            "observations",
        ],
    )
    assert posts == []
    assert not (tmp_path / "worklore" / "brigade").exists()


def test_identity_conflict_blocks_other_identities_before_any_post(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    other = tmp_path / "other"
    node_a = tmp_path / "node-a"
    node_b = tmp_path / "node-b"
    other.mkdir()
    node_a.mkdir()
    node_b.mkdir()
    _install_public_reader(
        monkeypatch,
        {
            other: ([_public_task(id="other1", text="Other work")], []),
            node_a: ([_public_task(text="Alpha title")], []),
            node_b: ([_public_task(text="Beta title")], []),
        },
    )
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.BrigadeSyncError, match="identity-conflict") as excinfo:
        sync.sync_brigade(
            targets=[
                sync.Target(path=other, identity=OTHER_IDENTITY),
                sync.Target(path=node_a, identity=IDENTITY),
                sync.Target(path=node_b, identity=IDENTITY),
            ]
        )
    assert excinfo.value.code == "identity-conflict"
    assert posts == []
    _assert_bounded_sync_error(
        excinfo.value,
        code="identity-conflict",
        forbidden=[IDENTITY, OTHER_IDENTITY, "Alpha title", "Other work", str(other)],
    )


def test_target_and_task_order_do_not_change_batch_or_keys(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    node_a = tmp_path / "node-a"
    node_b = tmp_path / "node-b"
    node_a.mkdir()
    node_b.mkdir()
    first = _public_task(id="zzz9", text="Last")
    second = _public_task(id="aaa1", text="First")
    sequence = [
        {node_a: ([first, second], []), node_b: ([], [])},
        {node_b: ([second, first], []), node_a: ([], [])},
    ]

    def fake_read(target):
        payloads = sequence[len(posts)]
        return payloads[target.path]

    monkeypatch.setattr(sync, "read_public_tasks", fake_read)
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    first_result = sync.sync_brigade(
        targets=[
            sync.Target(path=node_a, identity=IDENTITY),
            sync.Target(path=node_b, identity=IDENTITY),
        ]
    )
    second_result = sync.sync_brigade(
        targets=[
            sync.Target(path=node_b, identity=IDENTITY),
            sync.Target(path=node_a, identity=IDENTITY),
        ]
    )
    expected_obs = sync.observations_for_target([second, first], edges=[], identity=IDENTITY)
    expected_obs = sorted(expected_obs, key=lambda item: str(item["external_key"]))
    expected_key = _batch_key(IDENTITY, expected_obs)
    assert [obs["external_key"] for obs in posts[0]["observations"]] == [
        f"{IDENTITY}#aaa1",
        f"{IDENTITY}#zzz9",
    ]
    assert posts[0]["observations"] == posts[1]["observations"] == expected_obs
    assert posts[0]["idempotency_key"] == posts[1]["idempotency_key"] == expected_key
    assert first_result["observation_count"] == second_result["observation_count"] == 2


@pytest.mark.parametrize("order", [("node-a", "node-b"), ("node-b", "node-a")])
def test_reordered_equivalent_edges_are_identical_regardless_of_target_order(
    tmp_path, monkeypatch, order: tuple[str, str]
) -> None:
    posts: list[dict[str, object]] = []
    first_name, second_name = order
    first = tmp_path / first_name
    second = tmp_path / second_name
    first.mkdir()
    second.mkdir()
    edges_a = [
        {"source": "abc123", "target": "ghi789", "type": "parent-child"},
        {"source": "abc123", "target": "def456", "type": "blocks"},
    ]
    edges_b = [
        {"source": "abc123", "target": "def456", "type": "blocks"},
        {"source": "abc123", "target": "ghi789", "type": "parent-child"},
    ]
    payloads = {
        tmp_path / "node-a": ([_public_task()], edges_a),
        tmp_path / "node-b": ([_public_task()], edges_b),
    }
    _install_public_reader(monkeypatch, payloads)
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    result = sync.sync_brigade(
        targets=[
            sync.Target(path=first, identity=IDENTITY),
            sync.Target(path=second, identity=IDENTITY),
        ]
    )
    expected_deps = [
        {"type": "blocks", "id": "def456"},
        {"type": "parent-child", "id": "ghi789"},
    ]
    assert len(posts) == 1
    assert result["posted"] == 1
    assert result["observation_count"] == 1
    assert result["identities"] == 1
    assert posts[0]["observations"][0]["dependencies"] == expected_deps
    assert posts[0]["idempotency_key"] == _batch_key(IDENTITY, posts[0]["observations"])


def test_sync_splits_501_observations_into_two_batches(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    tasks = [_public_task(id=f"t{index:04d}", text=f"Task {index:04d}") for index in range(1, 502)]
    target = tmp_path / "repo"
    target.mkdir()
    _install_public_reader(monkeypatch, {target: (tasks, [])})
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    result = sync.sync_brigade(targets=[sync.Target(path=target, identity=IDENTITY)])
    observations = sync.observations_for_target(tasks, edges=[], identity=IDENTITY)
    observations = sorted(observations, key=lambda item: str(item["external_key"]))
    assert result["posted"] == 2
    assert result["observation_count"] == 501
    assert [len(post["observations"]) for post in posts] == [500, 1]
    assert posts[0]["observations"] == observations[:500]
    assert posts[1]["observations"] == observations[500:]
    assert posts[0]["idempotency_key"] == _batch_key(IDENTITY, observations[:500])
    assert posts[1]["idempotency_key"] == _batch_key(IDENTITY, observations[500:])
    assert posts[0]["idempotency_key"] != posts[1]["idempotency_key"]


def test_empty_discovery_posts_one_empty_batch_per_identity(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    first = tmp_path / "node-a"
    second = tmp_path / "node-b"
    first.mkdir()
    second.mkdir()
    _install_public_reader(monkeypatch, {first: ([], []), second: ([], [])})
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    result = sync.sync_brigade(
        targets=[
            sync.Target(path=first, identity=IDENTITY),
            sync.Target(path=second, identity=OTHER_IDENTITY),
        ]
    )
    assert result["posted"] == 2
    assert result["observation_count"] == 0
    assert result["identities"] == 2
    assert result["targets"] == 2
    assert [post["observations"] for post in posts] == [[], []]
    assert [post["adapter_id"] for post in posts] == [f"brigade:{IDENTITY}", f"brigade:{OTHER_IDENTITY}"]
    assert posts[0]["idempotency_key"] == _batch_key(IDENTITY, [])
    assert posts[1]["idempotency_key"] == _batch_key(OTHER_IDENTITY, [])


def test_envelope_is_exactly_adapter_fields(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    target = tmp_path / "repo"
    target.mkdir()
    _install_public_reader(monkeypatch, {target: ([_public_task()], [])})
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    sync.sync_brigade(targets=[sync.Target(path=target, identity=IDENTITY)])
    assert set(posts[0]) == {"adapter_id", "source_type", "idempotency_key", "observations"}
    assert posts[0]["source_type"] == "brigade"
    assert posts[0]["adapter_id"] == f"brigade:{IDENTITY}"


def test_fingerprint_covers_every_source_authoritative_field(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    target = tmp_path / "repo"
    target.mkdir()
    baseline = {
        "external_key": f"{IDENTITY}#abc123",
        "link_type": "brigade",
        "title": "Shared",
        "acceptance": ["ok"],
        "external_state": "pending",
        "priority": "high",
        "dependencies": [],
        "evidence_refs": ["run-1"],
        "source_policy": "eligible",
        "proposed_status": None,
        "external_updated_at": "2026-02-01T00:00:00+00:00",
    }
    variants = {
        "external_key": f"{IDENTITY}#other",
        "title": "Changed title",
        "acceptance": ["changed"],
        "external_state": "done",
        "external_updated_at": "2026-03-01T00:00:00+00:00",
        "priority": "low",
        "dependencies": [{"type": "blocks", "id": "other"}],
        "evidence_refs": ["run-other"],
        "source_policy": "completed",
        "proposed_status": "completed",
    }
    queue = [dict(baseline)]
    for field, value in variants.items():
        changed = dict(baseline)
        changed[field] = value
        queue.append(changed)

    def fake_observations(tasks, *, edges=None, identity):
        return [queue.pop(0)]

    monkeypatch.setattr(sync, "read_public_tasks", lambda target: ([], []))
    monkeypatch.setattr(sync, "observations_for_target", fake_observations)
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    for _ in range(len(variants) + 1):
        sync.sync_brigade(targets=[sync.Target(path=target, identity=IDENTITY)])
    baseline_key = posts[0]["idempotency_key"]
    assert baseline_key == _batch_key(IDENTITY, [baseline])
    changed_keys = [post["idempotency_key"] for post in posts[1:]]
    assert all(key != baseline_key for key in changed_keys)
    assert len(set(changed_keys)) == len(variants)


def test_batch_key_uses_sorted_fingerprints(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    target = tmp_path / "repo"
    target.mkdir()
    first = {
        "external_key": f"{IDENTITY}#aaa",
        "title": "Alpha",
        "acceptance": ["ok"],
        "external_state": "pending",
        "priority": "high",
        "dependencies": [],
        "evidence_refs": [],
        "source_policy": "eligible",
        "proposed_status": None,
    }
    second = {
        "external_key": f"{IDENTITY}#zzz",
        "title": "Zeta",
        "acceptance": ["ok"],
        "external_state": "pending",
        "priority": "low",
        "dependencies": [],
        "evidence_refs": [],
        "source_policy": "eligible",
        "proposed_status": None,
    }
    sequence = [[second, first], [first, second]]

    def fake_observations(tasks, *, edges=None, identity):
        return [dict(item) for item in sequence.pop(0)]

    monkeypatch.setattr(sync, "read_public_tasks", lambda target: ([], []))
    monkeypatch.setattr(sync, "observations_for_target", fake_observations)
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    sync.sync_brigade(targets=[sync.Target(path=target, identity=IDENTITY)])
    sync.sync_brigade(targets=[sync.Target(path=target, identity=IDENTITY)])
    expected = _batch_key(IDENTITY, [first, second])
    assert posts[0]["idempotency_key"] == expected
    assert posts[1]["idempotency_key"] == expected
    assert posts[0]["idempotency_key"].startswith(f"brigade:{IDENTITY}:")
    assert len(posts[0]["idempotency_key"].rsplit(":", 1)[1]) == 24
    assert [obs["external_key"] for obs in posts[0]["observations"]] == [
        f"{IDENTITY}#aaa",
        f"{IDENTITY}#zzz",
    ]


def test_result_sums_bounded_hub_counts(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    tasks = [_public_task(id=f"t{index:04d}", text=f"Task {index:04d}") for index in range(1, 502)]
    target = tmp_path / "repo"
    target.mkdir()
    _install_public_reader(monkeypatch, {target: (tasks, [])})

    def leaky_hub(payload):
        posts.append(payload)
        return {
            "created": 2,
            "updated": 3,
            "unchanged": 4,
            "items": [{"title": "Add store", "path": str(target)}],
            "secret": "token=abc",
            "created_text": "1",
        }

    monkeypatch.setattr(sync.worklore_client, "import_batch", leaky_hub)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    result = sync.sync_brigade(targets=[sync.Target(path=target, identity=IDENTITY)])
    assert set(result) == {
        "targets",
        "identities",
        "posted",
        "observation_count",
        "refused",
        "skipped",
        "created",
        "updated",
        "unchanged",
    }
    assert result["refused"] == 0
    assert result == {
        "targets": 1,
        "identities": 1,
        "posted": 2,
        "observation_count": 501,
        "refused": 0,
        "skipped": 0,
        "created": 4,
        "updated": 6,
        "unchanged": 8,
    }
    dumped = json.dumps(result)
    assert "Add store" not in dumped
    assert "token" not in dumped
    assert str(target) not in dumped
    assert "items" not in dumped


def test_hub_counts_ignore_negative_bool_float_and_numeric_strings(tmp_path, monkeypatch) -> None:
    tasks = [_public_task(id=f"t{index:04d}", text=f"Task {index:04d}") for index in range(1, 502)]
    target = tmp_path / "repo"
    target.mkdir()
    _install_public_reader(monkeypatch, {target: (tasks, [])})
    responses = [
        {"created": -2, "updated": True, "unchanged": "3"},
        {"created": 1.5, "updated": 4, "unchanged": False},
    ]

    def typed_hub(_payload):
        return responses.pop(0)

    monkeypatch.setattr(sync.worklore_client, "import_batch", typed_hub)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    result = sync.sync_brigade(targets=[sync.Target(path=target, identity=IDENTITY)])
    assert result["created"] == 0
    assert result["updated"] == 4
    assert result["unchanged"] == 0


def test_receipt_path_fields_and_private_modes(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    target = tmp_path / "repo"
    target.mkdir()
    _install_public_reader(monkeypatch, {target: ([_public_task()], [])})
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    sync.sync_brigade(targets=[sync.Target(path=target, identity=IDENTITY)])
    receipt_path = tmp_path / "worklore" / "brigade" / "escoffier-labs-brigade.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert set(receipt) == {
        "adapter_id",
        "idempotency_key",
        "observation_count",
        "refused",
        "skipped",
        "last_error_code",
        "updated_at",
    }
    assert receipt["refused"] == 0
    assert receipt["skipped"] == 0
    assert receipt["adapter_id"] == f"brigade:{IDENTITY}"
    assert receipt["idempotency_key"] == posts[0]["idempotency_key"]
    assert receipt["observation_count"] == 1
    assert receipt["last_error_code"] is None
    assert receipt["updated_at"].endswith("Z")
    dumped = json.dumps(receipt)
    assert "Shared" not in dumped
    assert "abc123" not in dumped
    assert str(target) not in dumped
    assert "payload" not in dumped
    assert_private_mode(receipt_path, PRIVATE_FILE_MODE)
    assert_private_mode(receipt_path.parent, PRIVATE_DIRECTORY_MODE)


def test_sync_skips_unsafe_task_and_reports_bounded_count(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    target = tmp_path / "repo"
    target.mkdir()
    _install_public_reader(
        monkeypatch,
        {target: ([UNSAFE_TASKS[0], _public_task(id="healthy")], [])},
    )
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    result = sync.sync_brigade(targets=[sync.Target(path=target, identity=IDENTITY)])
    assert result["skipped"] == 1
    assert result["observation_count"] == 1
    assert [obs["external_key"] for obs in posts[0]["observations"]] == [f"{IDENTITY}#healthy"]
    receipt = json.loads((tmp_path / "worklore" / "brigade" / "escoffier-labs-brigade.json").read_text())
    assert receipt["skipped"] == 1
    dumped = json.dumps({"posts": posts, "receipt": receipt})
    for fragment in UNSAFE_TASK_LEAKS:
        assert fragment not in dumped


def test_receipt_skips_are_scoped_to_the_identity(tmp_path, monkeypatch) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _install_public_reader(
        monkeypatch,
        {
            first: ([UNSAFE_TASKS[0]], []),
            second: ([_public_task(id="healthy", text="Healthy work")], []),
        },
    )
    monkeypatch.setattr(sync.worklore_client, "import_batch", lambda _payload: {})
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))

    result = sync.sync_brigade(
        targets=[
            sync.Target(path=first, identity=IDENTITY),
            sync.Target(path=second, identity=OTHER_IDENTITY),
        ]
    )

    first_receipt = json.loads((tmp_path / "worklore" / "brigade" / "escoffier-labs-brigade.json").read_text())
    second_receipt = json.loads((tmp_path / "worklore" / "brigade" / "other-org-other-repo.json").read_text())
    assert result["skipped"] == 1
    assert first_receipt["skipped"] == 1
    assert second_receipt["skipped"] == 0


def test_hub_refusals_do_not_stop_later_brigade_identities(tmp_path, monkeypatch) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    posts: list[dict[str, object]] = []
    _install_public_reader(
        monkeypatch,
        {
            first: ([_public_task()], []),
            second: ([_public_task(id="other-task", text="Other work")], []),
        },
    )

    def fake_import(payload):
        posts.append(payload)
        return {"refused": 1} if payload["adapter_id"] == f"brigade:{IDENTITY}" else {"created": 1}

    monkeypatch.setattr(sync.worklore_client, "import_batch", fake_import)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))

    result = sync.sync_brigade(
        targets=[
            sync.Target(path=first, identity=IDENTITY),
            sync.Target(path=second, identity=OTHER_IDENTITY),
        ]
    )

    assert [post["adapter_id"] for post in posts] == [f"brigade:{IDENTITY}", f"brigade:{OTHER_IDENTITY}"]
    assert result["refused"] == 1
    assert result["skipped"] == 0
    first_receipt = json.loads((tmp_path / "worklore" / "brigade" / "escoffier-labs-brigade.json").read_text())
    second_receipt = json.loads((tmp_path / "worklore" / "brigade" / "other-org-other-repo.json").read_text())
    assert (first_receipt["refused"], first_receipt["last_error_code"]) == (1, "hub-partial")
    assert (second_receipt["refused"], second_receipt["last_error_code"]) == (0, None)


BRIGADE_HUB_REFUSALS = [
    ("fleet hub work failed: HTTP 400: title must be 1 to 240 characters", "hub-rejected"),
    ("fleet hub work failed: HTTP 404", "hub-rejected"),
    ("fleet hub work failed: HTTP 422", "hub-rejected"),
    ("fleet hub work failed: HTTP 401", "hub-unauthorized"),
    ("fleet hub work failed: HTTP 403: adapter is owned by another node", "hub-unauthorized"),
    ("fleet hub work failed: HTTP 409: idempotency key reused with a different payload", "hub-conflict"),
    ("fleet hub work failed: HTTP 500", "hub-unavailable"),
    ("fleet hub work failed: HTTP 503: https://hub.example/work/imports token=abc", "hub-unavailable"),
    ("fleet hub work failed: invalid JSON", "hub-unavailable"),
    ("hub-unavailable", "hub-unavailable"),
]


@pytest.mark.parametrize(("message", "code"), BRIGADE_HUB_REFUSALS)
def test_import_refusals_keep_stable_non_transient_codes(tmp_path, monkeypatch, message: str, code: str) -> None:
    target = tmp_path / "repo"
    target.mkdir()
    _install_public_reader(monkeypatch, {target: ([_public_task()], [])})
    monkeypatch.setattr(
        sync.worklore_client,
        "import_batch",
        lambda payload: (_ for _ in ()).throw(sync.worklore_client.FleetClientError(message)),
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.BrigadeSyncError) as excinfo:
        sync.sync_brigade(targets=[sync.Target(path=target, identity=IDENTITY)])
    _assert_bounded_sync_error(
        excinfo.value,
        code=code,
        forbidden=[IDENTITY, "Shared", "token", "https://hub.example", str(target)],
    )
    _assert_no_hub_leak(str(excinfo.value))
    receipt = json.loads((tmp_path / "worklore" / "brigade" / "escoffier-labs-brigade.json").read_text())
    assert receipt["last_error_code"] == code
    _assert_no_hub_leak(json.dumps(receipt))


def test_receipt_filename_sanitizes_reserved_characters() -> None:
    raw = 'escoffier:labs/brigade\\x<y>"z|a?b*c'
    safe = sync._safe_identity(raw)
    for character in ':/\\<>"|?*':
        assert character not in safe
    assert safe == "escoffier-labs-brigade-x-y--z-a-b-c"


def test_hub_unavailable_is_bounded_and_writes_best_effort_receipt(tmp_path, monkeypatch) -> None:
    target = tmp_path / "repo"
    target.mkdir()
    ledger = target / ".brigade" / "work" / "tasks.json"
    ledger.parent.mkdir(parents=True)
    original = '{"tasks":[{"id":"local-keep"}]}'
    ledger.write_text(original, encoding="utf-8")
    before = ledger.stat().st_mtime_ns
    _install_public_reader(monkeypatch, {target: ([_public_task()], [])})
    monkeypatch.setattr(
        sync.worklore_client,
        "import_batch",
        lambda payload: (_ for _ in ()).throw(
            sync.worklore_client.FleetClientError(
                "fleet hub work failed: HTTP 503: https://hub.example/work/imports token=abc title=Add store"
            )
        ),
    )
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.BrigadeSyncError, match="hub-unavailable") as excinfo:
        sync.sync_brigade(targets=[sync.Target(path=target, identity=IDENTITY)])
    _assert_bounded_sync_error(
        excinfo.value,
        code="hub-unavailable",
        forbidden=[
            IDENTITY,
            "Add store",
            "token",
            "https://hub.example",
            str(target),
            "Shared",
            "abc123",
        ],
    )
    _assert_no_hub_leak(str(excinfo.value))
    assert ledger.read_text(encoding="utf-8") == original
    assert ledger.stat().st_mtime_ns == before
    receipt_path = tmp_path / "worklore" / "brigade" / "escoffier-labs-brigade.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["last_error_code"] == "hub-unavailable"
    assert receipt["adapter_id"] == f"brigade:{IDENTITY}"
    assert "Add store" not in json.dumps(receipt)
    assert "https://hub.example" not in json.dumps(receipt)
    assert_private_mode(receipt_path, PRIVATE_FILE_MODE)
    assert_private_mode(receipt_path.parent, PRIVATE_DIRECTORY_MODE)


def _leaky_receipt_oserror(tmp_path: Path) -> OSError:
    leaky_path = tmp_path / "home" / "operator" / "worklore" / "brigade" / "escoffier-labs-brigade.json"
    return OSError(13, f"Permission denied: '{leaky_path}'")


def test_successful_post_receipt_write_failure_is_bounded_and_replay_safe(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    target = tmp_path / "repo"
    target.mkdir()
    expected_obs = sync.observations_for_target([_public_task()], edges=[], identity=IDENTITY)
    expected_key = _batch_key(IDENTITY, expected_obs)
    leak = _leaky_receipt_oserror(tmp_path)

    def failing_write(*args, **kwargs):
        raise leak

    _install_public_reader(monkeypatch, {target: ([_public_task()], [])})
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setattr(sync, "_write_receipt", failing_write)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.BrigadeSyncError, match="receipt-write-failed") as excinfo:
        sync.sync_brigade(targets=[sync.Target(path=target, identity=IDENTITY)])
    assert excinfo.value.code == "receipt-write-failed"
    assert str(excinfo.value) == "receipt-write-failed: receipt-write-failed"
    message = str(excinfo.value)
    _assert_no_hub_leak(message)
    _assert_bounded_sync_error(
        excinfo.value,
        code="receipt-write-failed",
        forbidden=[IDENTITY, str(tmp_path), "Permission denied", "escoffier-labs-brigade.json"],
    )
    assert leak.strerror not in message
    assert len(posts) == 1
    assert posts[0]["idempotency_key"] == expected_key


def test_hub_unavailable_receipt_write_failure_stays_hub_unavailable(tmp_path, monkeypatch) -> None:
    leak = _leaky_receipt_oserror(tmp_path)
    target = tmp_path / "repo"
    target.mkdir()

    def failing_write(*args, **kwargs):
        raise leak

    _install_public_reader(monkeypatch, {target: ([_public_task()], [])})
    monkeypatch.setattr(
        sync.worklore_client,
        "import_batch",
        lambda payload: (_ for _ in ()).throw(
            sync.worklore_client.FleetClientError(
                "fleet hub work failed: HTTP 503: https://hub.example/work/imports token=abc title=Add store"
            )
        ),
    )
    monkeypatch.setattr(sync, "_write_receipt", failing_write)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.BrigadeSyncError, match="hub-unavailable") as excinfo:
        sync.sync_brigade(targets=[sync.Target(path=target, identity=IDENTITY)])
    assert excinfo.value.code == "hub-unavailable"
    message = str(excinfo.value)
    _assert_no_hub_leak(message)
    assert IDENTITY not in message
    assert str(tmp_path) not in message
    assert "Permission denied" not in message
    assert "OSError" not in message
    assert leak.strerror not in message


def test_sync_rejects_empty_targets_before_hub(tmp_path, monkeypatch) -> None:
    posts: list[dict[str, object]] = []
    monkeypatch.setattr(sync.worklore_client, "import_batch", _recording_hub(posts))
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    with pytest.raises(sync.BrigadeSyncError, match="field-bound") as excinfo:
        sync.sync_brigade(targets=[])
    assert excinfo.value.code == "field-bound"
    assert posts == []


CLI_TARGETS: list[object] = [{"path": "not-a-real-checkout", "identity": IDENTITY}]
CLI_CONFIG = {"targets": CLI_TARGETS}
CLI_COUNT_FIELDS = (
    "targets",
    "identities",
    "posted",
    "observation_count",
    "created",
    "refused",
    "skipped",
    "updated",
    "unchanged",
)
CLI_LEAKY_RESULT = {
    "targets": 1,
    "identities": 1,
    "posted": 1,
    "observation_count": 3,
    "created": 1,
    "updated": 0,
    "unchanged": 2,
    "refused": 0,
    "skipped": 0,
    "title": "Add Worklore store",
    "url": "https://hub.example/work/imports",
    "token": "brigade_secret",
    "observations": [{"title": "Add Worklore store", "item": "hub-item-1"}],
    "identity": IDENTITY,
    "path": "/tmp/not-a-real-checkout",
}


def test_worklore_brigade_sync_is_imported_at_fleet_module_scope() -> None:
    assert hasattr(fleet_cli, "worklore_brigade_sync")
    assert fleet_cli.worklore_brigade_sync is sync


def test_sync_brigade_cli_json(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_sync(targets):
        captured["targets"] = targets
        return dict(CLI_LEAKY_RESULT)

    monkeypatch.setattr(fleet_cli.worklore_brigade_sync, "load_brigade_config", lambda: CLI_CONFIG)
    monkeypatch.setattr(fleet_cli.worklore_brigade_sync, "sync_brigade", fake_sync)
    assert fleet_cli._dispatch_work_sync_brigade(argparse.Namespace(json=True)) == 0
    captured_io = capsys.readouterr()
    assert captured_io.err == ""
    payload = json.loads(captured_io.out)
    assert captured_io.out == json.dumps(payload, indent=2, sort_keys=True) + "\n"
    assert captured["targets"] is CLI_CONFIG["targets"]
    assert set(payload) == set(CLI_COUNT_FIELDS)
    assert all(isinstance(payload[field], int) and payload[field] >= 0 for field in CLI_COUNT_FIELDS)
    assert payload == {
        "created": 1,
        "identities": 1,
        "observation_count": 3,
        "posted": 1,
        "refused": 0,
        "skipped": 0,
        "targets": 1,
        "unchanged": 2,
        "updated": 0,
    }
    _assert_no_hub_leak(captured_io.out)
    assert IDENTITY not in captured_io.out
    assert "Add Worklore store" not in captured_io.out
    assert "not-a-real-checkout" not in captured_io.out
    assert "brigade_secret" not in captured_io.out
    assert "hub-item-1" not in captured_io.out


def test_sync_brigade_cli_default_reports_counts_only(monkeypatch, capsys) -> None:
    monkeypatch.setattr(fleet_cli.worklore_brigade_sync, "load_brigade_config", lambda: CLI_CONFIG)
    monkeypatch.setattr(
        fleet_cli.worklore_brigade_sync,
        "sync_brigade",
        lambda targets: dict(CLI_LEAKY_RESULT),
    )
    assert fleet_cli._dispatch_work_sync_brigade(argparse.Namespace(json=False)) == 0
    captured_io = capsys.readouterr()
    assert captured_io.err == ""
    lines = [line for line in captured_io.out.splitlines() if line.strip()]
    assert len(lines) == 1
    line = lines[0]
    assert "1" in line
    assert "3" in line
    assert "2" in line
    assert "posted" in line.lower()
    assert "observation" in line.lower()
    assert "created" in line.lower()
    assert IDENTITY not in captured_io.out
    assert "Add Worklore store" not in captured_io.out
    assert "not-a-real-checkout" not in captured_io.out
    assert "brigade_secret" not in captured_io.out
    assert "https://" not in captured_io.out
    assert "hub-item-1" not in captured_io.out
    assert "title" not in line.lower()
    _assert_no_hub_leak(captured_io.out)


def test_sync_brigade_cli_surfaces_refusals_after_printing_counts(monkeypatch, capsys) -> None:
    monkeypatch.setattr(fleet_cli.worklore_brigade_sync, "load_brigade_config", lambda: CLI_CONFIG)
    monkeypatch.setattr(
        fleet_cli.worklore_brigade_sync,
        "sync_brigade",
        lambda targets: {
            "targets": 2,
            "identities": 2,
            "posted": 2,
            "observation_count": 2,
            "created": 1,
            "updated": 0,
            "unchanged": 0,
            "refused": 1,
            "skipped": 3,
        },
    )

    assert fleet_cli._dispatch_work_sync_brigade(argparse.Namespace(json=True)) == 1
    assert json.loads(capsys.readouterr().out) == {
        "created": 1,
        "identities": 2,
        "observation_count": 2,
        "posted": 2,
        "refused": 1,
        "skipped": 3,
        "targets": 2,
        "unchanged": 0,
        "updated": 0,
    }
    assert fleet_cli._dispatch_work_sync_brigade(argparse.Namespace(json=False)) == 1
    assert capsys.readouterr().out == (
        "targets 2, identities 2, posted 2, 2 observation(s), created 1, updated 0, unchanged 0, refused 1, skipped 3\n"
    )


@pytest.mark.parametrize(
    "message",
    (
        "Brigade Worklore config is missing targets",
        "targets must be a non-empty list",
    ),
)
def test_sync_brigade_cli_config_error_returns_2(monkeypatch, capsys, message: str) -> None:
    def missing_config() -> dict[str, object]:
        raise sync.BrigadeSyncError(message, code="field-bound")

    monkeypatch.setattr(fleet_cli.worklore_brigade_sync, "load_brigade_config", missing_config)
    assert fleet_cli._dispatch_work_sync_brigade(argparse.Namespace(json=False)) == 2
    captured_io = capsys.readouterr()
    assert captured_io.out == ""
    err_lines = [line for line in captured_io.err.splitlines() if line.strip()]
    assert err_lines == [f"field-bound: {message}"]
    _assert_no_hub_leak(captured_io.err)
    assert IDENTITY not in captured_io.err
    assert "/home/" not in captured_io.err


@pytest.mark.parametrize(
    ("code", "message"),
    (
        ("hub-unavailable", "hub-unavailable"),
        ("hub-rejected", "hub-rejected"),
        ("hub-unauthorized", "hub-unauthorized"),
        ("hub-conflict", "hub-conflict"),
        ("retryable-adapter-failure", "adapter command failed"),
        ("receipt-write-failed", "receipt-write-failed"),
        ("identity-conflict", "identity-conflict"),
    ),
)
def test_sync_brigade_cli_adapter_failures_return_1(monkeypatch, capsys, code: str, message: str) -> None:
    monkeypatch.setattr(fleet_cli.worklore_brigade_sync, "load_brigade_config", lambda: CLI_CONFIG)

    def boom(targets):
        raise sync.BrigadeSyncError(message, code=code)

    monkeypatch.setattr(fleet_cli.worklore_brigade_sync, "sync_brigade", boom)
    assert fleet_cli._dispatch_work_sync_brigade(argparse.Namespace(json=False)) == 1
    captured_io = capsys.readouterr()
    assert captured_io.out == ""
    err_lines = [line for line in captured_io.err.splitlines() if line.strip()]
    assert err_lines == [f"{code}: {message}"]
    _assert_no_hub_leak(captured_io.err)
    assert IDENTITY not in captured_io.err
    assert "/home/" not in captured_io.err
    assert "Add Worklore store" not in captured_io.err
    assert "not-a-real-checkout" not in captured_io.err


def test_fleet_work_parser_accepts_sync_brigade() -> None:
    parser = cli._build_parser()
    parsed = parser.parse_args(["fleet", "work", "sync-brigade", "--json"])
    assert parsed.func is fleet_cli._dispatch_work_sync_brigade
    assert parsed.json is True
    default = parser.parse_args(["fleet", "work", "sync-brigade"])
    assert default.func is fleet_cli._dispatch_work_sync_brigade
    assert default.json is False


def test_a_normalized_task_carries_the_source_revision_the_hub_guard_needs() -> None:
    observation = adapter.normalize_task(
        _pending_task(updated_at="2026-02-01T00:00:00+00:00"), identity=IDENTITY, edges=[]
    )
    assert observation["external_updated_at"] == "2026-02-01T00:00:00+00:00"


def test_a_task_without_a_usable_revision_reports_none_rather_than_a_guess() -> None:
    for value in (None, "", "   ", 17, "x" * 200):
        task = _pending_task()
        if value is not None:
            task["updated_at"] = value
        observation = adapter.normalize_task(task, identity=IDENTITY, edges=[])
        assert "external_updated_at" not in observation


def test_the_source_revision_is_part_of_the_batch_fingerprint() -> None:
    assert "external_updated_at" in sync.FINGERPRINT_FIELDS
