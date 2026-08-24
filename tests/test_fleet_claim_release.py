"""Issue #1141: crash self-lockout recovery for hub-arbitrated claims.

A SIGKILLed run leaves an unexpired hub claim under its own node with a
holder token nobody has. Three ways out: ``brigade fleet claims --release``
(own node, or ``--force``), a same-node supersede on acquire once the local
lease reconcile has found the previous run dead, and ``brigade run
--no-fleet-claim``.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from brigade import fleet_client, fleet_hub

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
DEAD_PID = 99999999


@pytest.fixture()
def hub(tmp_path):
    db = tmp_path / "hub" / "fleet.db"
    token = "test-token-12345"
    server = fleet_hub.make_server("127.0.0.1", 0, db, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", token, db
    server.shutdown()
    server.server_close()


def _post(url: str, token: str, body, path: str = "/claims") -> tuple[int, dict]:
    request = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _claim(action: str = "acquire", node: str = NODE_A, target: str = "repo-a", holder: str = "h1", **extra) -> dict:
    return {"action": action, "node_id": node, "target": target, "holder": holder, **extra}


def _env(monkeypatch, url: str, token: str) -> None:
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)


class TestHubScopes:
    def test_acquire_scope_node_supersedes_own_dead_holder_only(self, hub):
        url, token, _db = hub
        assert _post(url, token, _claim(holder="dead", conductor="chef"))[1]["granted"] is True
        # Default (holder) scope: the unexpired same-node row still wins.
        status, payload = _post(url, token, _claim(holder="h2"))
        assert status == 409 and payload["granted"] is False
        # Node scope from the same node takes it over under the new token.
        status, payload = _post(url, token, _claim(holder="h2", scope="node", conductor="sous"))
        assert status == 200 and payload["granted"] is True
        assert payload["superseded"]["owner_node"] == NODE_A
        assert payload["superseded"]["owner_conductor"] == "chef"
        assert payload["claim"]["owner_conductor"] == "sous"
        # The dead token is fenced out; the new holder owns renew/release.
        assert _post(url, token, _claim("renew", holder="dead"))[0] == 409
        assert _post(url, token, _claim("renew", holder="h2"))[1]["renewed"] is True
        # A node-scoped acquire from another node is still a plain 409.
        status, payload = _post(url, token, _claim(node=NODE_B, holder="hb", scope="node"))
        assert status == 409 and payload["owner"]["owner_node"] == NODE_A
        # Idempotent retry for the new holder reports nothing superseded.
        status, payload = _post(url, token, _claim(holder="h2", scope="node"))
        assert status == 200 and "superseded" not in payload

    def test_release_scope_node_needs_no_token_but_same_owner(self, hub):
        url, token, _db = hub
        assert _post(url, token, _claim(holder="dead", conductor="chef"))[1]["granted"] is True
        body = _claim("release", node=NODE_B, scope="node")
        body.pop("holder")
        status, payload = _post(url, token, body)
        assert status == 409 and payload["released"] is False
        assert payload["owner"]["owner_node"] == NODE_A and NODE_B in payload["error"]
        body = _claim("release", scope="node")
        body.pop("holder")
        status, payload = _post(url, token, body)
        assert status == 200 and payload["released"] is True
        assert payload["claim"]["owner_node"] == NODE_A and payload["claim"]["owner_conductor"] == "chef"
        assert _post(url, token, body) == (200, {"released": False})
        assert _post(url, token, _claim(node=NODE_B, holder="hb"))[1]["granted"] is True

    def test_release_scope_force_releases_any_owner(self, hub):
        url, token, _db = hub
        assert _post(url, token, _claim(holder="dead"))[1]["granted"] is True
        body = _claim("release", node=NODE_B, scope="force")
        body.pop("holder")
        status, payload = _post(url, token, body)
        assert status == 200 and payload["released"] is True and payload["claim"]["owner_node"] == NODE_A
        assert _post(url, token, _claim(node=NODE_B, holder="hb"))[1]["granted"] is True

    def test_scope_validation(self, hub):
        url, token, _db = hub
        assert _post(url, token, _claim(scope="force"))[0] == 400
        assert _post(url, token, _claim("renew", scope="node"))[0] == 400
        assert _post(url, token, _claim("release", scope="bogus"))[0] == 400
        assert _post(url, token, _claim(scope=1))[0] == 400
        # A holder-scoped release still needs its token.
        body = _claim("release")
        body.pop("holder")
        assert _post(url, token, body)[0] == 400
        # A token-less release still needs a real node identity.
        body = _claim("release", node="unknown", scope="force")
        body.pop("holder")
        assert _post(url, token, body)[0] == 400


class TestClient:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "brigade-home"))
        monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: NODE_A)

    def test_release_claim_without_token_by_node_and_force(self, hub, monkeypatch):
        url, token, _db = hub
        _env(monkeypatch, url, token)
        assert fleet_client.acquire_claim("repo-a", node_id=NODE_A).granted is True
        refused = fleet_client.release_claim("repo-a", node_id=NODE_B)
        assert (refused.granted, refused.reason) == (False, "held")
        assert refused.owner is not None and refused.owner["owner_node"] == NODE_A
        released = fleet_client.release_claim("repo-a", node_id=NODE_A)
        assert released.granted is True and released.claim is not None
        assert released.claim["owner_node"] == NODE_A
        assert fleet_client.release_claim("repo-a", node_id=NODE_A).reason == "missing"
        assert fleet_client.acquire_claim("repo-a", node_id=NODE_A).granted is True
        assert fleet_client.release_claim("repo-a", node_id=NODE_B, force=True).granted is True
        assert fleet_client.fetch_claims() == []

    def test_repo_claim_supersedes_dead_same_node_claim_and_logs_once(self, hub, monkeypatch, caplog):
        url, token, _db = hub
        _env(monkeypatch, url, token)
        dead = fleet_client.acquire_claim("repo-a", node_id=NODE_A, conductor="chef").claim
        assert dead is not None
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            with fleet_client.repo_claim("repo-a", conductor="sous", supersede_same_node=True) as decision:
                assert decision.granted is True
                assert decision.superseded is not None and decision.superseded["owner_conductor"] == "chef"
                held = fleet_client.fetch_claims()
                assert [c["owner_conductor"] for c in held] == ["sous"]
        assert fleet_client.fetch_claims() == []
        assert sum("superseded this node's unexpired claim" in r.message for r in caplog.records) == 1

    def test_repo_claim_without_supersede_names_the_way_out(self, hub, monkeypatch):
        url, token, _db = hub
        _env(monkeypatch, url, token)
        assert fleet_client.acquire_claim("repo-a", node_id=NODE_A).granted is True
        with pytest.raises(fleet_client.FleetClaimHeldError) as exc:
            with fleet_client.repo_claim("repo-a"):
                pass  # pragma: no cover - never entered
        message = str(exc.value)
        assert f"claimed by node {NODE_A}" in message
        assert "brigade fleet claims --release repo-a" in message and "--no-fleet-claim" in message
        # Another node's claim keeps the cross-machine wording.
        fleet_client.release_claim("repo-a", node_id=NODE_A)
        assert fleet_client.acquire_claim("repo-a", node_id=NODE_B).granted is True
        with pytest.raises(fleet_client.FleetClaimHeldError, match="another machine or conductor"):
            with fleet_client.repo_claim("repo-a"):
                pass  # pragma: no cover - never entered

    def test_repo_claim_supersede_never_steals_another_node(self, hub, monkeypatch):
        url, token, _db = hub
        _env(monkeypatch, url, token)
        assert fleet_client.acquire_claim("repo-a", node_id=NODE_B).granted is True
        with pytest.raises(fleet_client.FleetClaimHeldError):
            with fleet_client.repo_claim("repo-a", supersede_same_node=True):
                pass  # pragma: no cover - never entered
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [NODE_B]


class TestReleaseCli:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "brigade-home"))
        monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: NODE_A)

    def test_release_own_node_claim_text_and_json(self, hub, monkeypatch, capsys):
        from brigade import cli

        url, token, _db = hub
        _env(monkeypatch, url, token)
        _post(url, token, _claim(holder="dead", conductor="chef"))
        assert cli.main(["fleet", "claims", "--release", "repo-a"]) == 0
        out = capsys.readouterr().out
        assert "released claim on 'repo-a'" in out and NODE_A in out and "chef" in out
        assert fleet_client.fetch_claims() == []
        _post(url, token, _claim(holder="dead2"))
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["released"] is True and payload["forced"] is False
        assert payload["target"] == "repo-a" and payload["node_id"] == NODE_A
        assert payload["claim"]["owner_node"] == NODE_A
        assert fleet_client.fetch_claims() == []

    def test_release_other_node_refused_without_force(self, hub, monkeypatch, capsys):
        from brigade import cli

        url, token, _db = hub
        _env(monkeypatch, url, token)
        _post(url, token, _claim(node=NODE_B, holder="hb"))
        assert cli.main(["fleet", "claims", "--release", "repo-a"]) == 1
        err = capsys.readouterr().err
        assert f"held by node {NODE_B}" in err and "--force" in err
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [NODE_B]
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--json"]) == 1
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["released"] is False and payload["owner"]["owner_node"] == NODE_B
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [NODE_B]
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--force"]) == 0
        assert f"held by node {NODE_B}" in capsys.readouterr().out
        assert fleet_client.fetch_claims() == []

    def test_release_missing_misuse_and_no_hub(self, hub, tmp_path, monkeypatch, capsys):
        from brigade import cli

        url, token, _db = hub
        _env(monkeypatch, url, token)
        assert cli.main(["fleet", "claims", "--release", "repo-a"]) == 0
        assert "no active claim on 'repo-a'" in capsys.readouterr().out
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["released"] is False
        assert cli.main(["fleet", "claims", "--force"]) == 2
        assert "--force requires --release" in capsys.readouterr().err
        monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: "unknown")
        assert cli.main(["fleet", "claims", "--release", "repo-a"]) == 1
        assert "no usable fleet node identity" in capsys.readouterr().err
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: NODE_A)
        assert cli.main(["fleet", "claims", "--release", "repo-a"]) == 1
        assert "fleet hub claim release failed" in capsys.readouterr().err
        monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
        assert cli.main(["fleet", "claims", "--release", "repo-a"]) == 1
        assert "no fleet hub configured" in capsys.readouterr().err


class TestDispatchRecovery:
    """SIGKILL-style: a dead run's lock and its unexpired hub claim, same node."""

    @pytest.fixture()
    def workspace(self, tmp_path, monkeypatch):
        from brigade import node as node_mod

        monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
        ws = tmp_path / "ws"
        (ws / ".brigade").mkdir(parents=True)
        node_mod.ensure_identity(ws)
        roster_path = ws / "roster.toml"
        roster_path.write_text('orchestrator = "chef"\n\n[agents.chef]\ncli = "codex"\nrole = "plan"\n')
        return ws, roster_path

    def _run(self, ws: Path, roster_path: Path, *extra: str) -> int:
        from brigade import cli

        return cli.main(["run", "do something", "--roster", str(roster_path), "--cwd", str(ws), *extra])

    def _plant_dead_run_lock(self, ws: Path) -> Path:
        """A run.lock whose owner pid is gone and whose run never finished."""
        from brigade import runguard

        abandoned = ws / ".brigade" / "runs" / "abandoned"
        abandoned.mkdir(parents=True)
        (abandoned / "run.json").write_text(json.dumps({"schema": "brigade.run.v1", "status": "dispatching"}))
        lock = runguard.lock_path(ws)
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{DEAD_PID}\n")
        (lock / "owner.json").write_text(
            json.dumps(
                {
                    "schema": "brigade.run_lock.v1",
                    "owner_token": "dead-owner",
                    "pid": DEAD_PID,
                    "run_dir": str(abandoned.resolve()),
                    "acquired_at": "2026-08-01T00:00:00+00:00",
                }
            )
        )
        return abandoned

    def test_dead_owner_lock_supersedes_own_stale_claim_without_ttl_wait(self, hub, workspace, monkeypatch, caplog):
        from brigade import aboyeur, node as node_mod

        url, token, _db = hub
        _env(monkeypatch, url, token)
        ws, roster_path = workspace
        identity = node_mod.load_identity(ws)
        assert identity is not None
        # The killed run's claim: this node, full default TTL, a token nobody has.
        status, payload = _post(url, token, _claim(node=identity.node_id, target="ws", holder="dead", conductor="chef"))
        assert status == 200
        dead_claim = payload["claim"]
        abandoned = self._plant_dead_run_lock(ws)
        held_during_run = []
        monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: held_during_run.extend(fleet_client.fetch_claims()) or 0)
        started = time.monotonic()
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            assert self._run(ws, roster_path) == 0
        assert time.monotonic() - started < dead_claim["ttl_seconds"] / 10
        assert [(c["target"], c["owner_node"]) for c in held_during_run] == [("ws", identity.node_id)]
        assert held_during_run[0]["acquired_at"] != dead_claim["acquired_at"]
        assert sum("superseded this node's unexpired claim" in r.message for r in caplog.records) == 1
        # The lease reconcile terminalized the dead run, and the run released its claim on exit.
        assert json.loads((abandoned / "run.json").read_text())["status"] == "failed"
        assert fleet_client.fetch_claims() == []

    def test_own_stale_claim_without_dead_lock_is_refused_with_hint(self, hub, workspace, monkeypatch, capsys):
        from brigade import aboyeur, node as node_mod

        url, token, _db = hub
        _env(monkeypatch, url, token)
        ws, roster_path = workspace
        identity = node_mod.load_identity(ws)
        assert identity is not None
        _post(url, token, _claim(node=identity.node_id, target="ws", holder="dead"))
        dispatched = []
        monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: dispatched.append(1) or 0)
        assert self._run(ws, roster_path) == 2
        err = capsys.readouterr().err
        assert f"claimed by node {identity.node_id}" in err
        assert "brigade fleet claims --release ws" in err and "--no-fleet-claim" in err
        assert dispatched == []
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [identity.node_id]

    def test_dead_owner_lock_never_steals_another_nodes_claim(self, hub, workspace, monkeypatch, capsys):
        from brigade import aboyeur

        url, token, _db = hub
        _env(monkeypatch, url, token)
        ws, roster_path = workspace
        _post(url, token, _claim(node=NODE_B, target="ws", holder="hb"))
        self._plant_dead_run_lock(ws)
        dispatched = []
        monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: dispatched.append(1) or 0)
        assert self._run(ws, roster_path) == 2
        assert f"claimed by node {NODE_B}" in capsys.readouterr().err
        assert dispatched == []
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [NODE_B]

    def test_no_fleet_claim_skips_hub_and_logs_once(self, hub, workspace, monkeypatch, caplog):
        from brigade import aboyeur

        url, token, _db = hub
        _env(monkeypatch, url, token)
        ws, roster_path = workspace
        _post(url, token, _claim(node=NODE_B, target="ws", holder="hb"))
        held_during_run = []
        monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: held_during_run.extend(fleet_client.fetch_claims()) or 0)
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            assert self._run(ws, roster_path, "--no-fleet-claim") == 0
        # Ran despite the other node's claim, and never touched it.
        assert [c["owner_node"] for c in held_during_run] == [NODE_B]
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [NODE_B]
        skipped = [r.message for r in caplog.records if "--no-fleet-claim" in r.message]
        assert len(skipped) == 1 and "ws" in skipped[0]


def test_detached_child_argv_forwards_no_fleet_claim(tmp_path):
    from brigade import roster as roster_mod
    from brigade.cli import run as run_cli

    def args(**overrides):
        base = dict(
            task="do work",
            allow_dirty=False,
            worktree=False,
            show_plan=False,
            verbose=False,
            read_only=False,
            no_code_graph=False,
            no_evidence=False,
            sandbox=None,
            codex_transport=None,
            handoff=False,
            handoff_inbox=None,
            worker=None,
            wait=None,
            keep_going=False,
            scheduler="waves",
            run_budget=None,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    resolution = roster_mod.RosterResolution(path=(tmp_path / "roster.toml").resolve(), source="workspace", shadowed=())
    argv = run_cli._detached_child_argv(
        args(no_fleet_claim=True), run_cwd=tmp_path, roster_resolution=resolution, output_dir=tmp_path / "run"
    )
    assert "--no-fleet-claim" in argv
    argv = run_cli._detached_child_argv(
        args(no_fleet_claim=False), run_cwd=tmp_path, roster_resolution=resolution, output_dir=tmp_path / "run"
    )
    assert "--no-fleet-claim" not in argv


def test_run_lock_reports_reconciled_dead_owner(tmp_path):
    from brigade import runguard

    repo = tmp_path / "repo"
    repo.mkdir()
    abandoned = tmp_path / "abandoned"
    abandoned.mkdir()
    (abandoned / "run.json").write_text(json.dumps({"schema": "brigade.run.v1", "status": "dispatching"}))
    lock = runguard.lock_path(repo)
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{DEAD_PID}\n")
    (lock / "owner.json").write_text(
        json.dumps(
            {"schema": "brigade.run_lock.v1", "owner_token": "dead-owner", "pid": DEAD_PID, "run_dir": str(abandoned)}
        )
    )
    seen: list = []
    with runguard.run_lock(repo, run_dir=tmp_path / "new-run", on_reconcile=seen.append):
        assert len(seen) == 1
    assert seen[0] is not None and seen[0]["owner_token"] == "dead-owner" and seen[0]["pid"] == DEAD_PID
    # A free lock and a live owner never report a reconcile.
    seen.clear()
    with runguard.run_lock(repo, on_reconcile=seen.append):
        pass
    assert seen == []
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")
    with pytest.raises(runguard.RunLockError):
        with runguard.run_lock(repo, on_reconcile=seen.append):
            pass  # pragma: no cover - never entered
    assert seen == []
