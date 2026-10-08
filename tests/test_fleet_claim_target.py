"""Repo claim key derivation (#1639): never the home identity directory name."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from brigade import fleet_client
from brigade import node as node_mod
from brigade.fleet_claim_target import ClaimTargetError

NODE_A = "11111111-1111-4111-8111-111111111111"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _make_home(root: Path, name: str, monkeypatch=None) -> Path:
    """A fake user home holding only the machine identity (no repo node.toml)."""
    home = root / name
    (home / "repos").mkdir(parents=True)
    identity = node_mod.NodeIdentity(node_id=NODE_A, hostname="fleet-test", roles=(), platform="test")
    path = node_mod.node_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(node_mod._format_node_toml(identity), encoding="utf-8")
    if monkeypatch is not None:
        monkeypatch.setenv("BRIGADE_HOME", str(home / ".brigade"))
    return home


def _make_repo(parent: Path, name: str, origin: str | None) -> Path:
    repo = parent / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Example Dev")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    if origin is not None:
        _git(repo, "remote", "add", "origin", origin)
    return repo


def test_repo_under_home_with_only_the_home_identity_gets_its_own_key(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    repo = _make_repo(home / "repos", "brigade", "git@github.com:escoffier-labs/brigade.git")
    assert fleet_client.find_workspace_for_path(repo) == home
    key = fleet_client.resolve_claim_target(repo)
    assert key == "escoffier-labs/brigade"
    assert key != home.name
    (repo / "src").mkdir()
    assert fleet_client.resolve_claim_target(repo / "src") == key


def test_unrelated_repos_under_one_home_get_different_keys(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    one = _make_repo(home / "repos", "one", "https://github.com/acme/one.git")
    two = _make_repo(home / "repos", "two", "https://github.com/acme/two.git")
    assert fleet_client.resolve_claim_target(one) != fleet_client.resolve_claim_target(two)


def test_same_repo_cloned_under_different_home_names_shares_a_key(tmp_path, monkeypatch):
    home_a = _make_home(tmp_path, "alice", monkeypatch)
    here = _make_repo(home_a / "repos", "brigade", "https://github.com/escoffier-labs/brigade.git")
    key_here = fleet_client.resolve_claim_target(here)
    # The second machine: a different home name, so its identity dir differs.
    home_b = _make_home(tmp_path, "bob", monkeypatch)
    there = _make_repo(home_b / "src", "brigade-clone", "git@github.com:escoffier-labs/brigade.git")
    assert fleet_client.resolve_claim_target(there) == key_here == "escoffier-labs/brigade"


def test_repo_without_a_remote_falls_back_to_the_git_toplevel_name(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    repo = _make_repo(home / "repos", "scratch", None)
    nested = repo / "pkg" / "deep"
    nested.mkdir(parents=True)
    assert fleet_client.resolve_claim_target(nested) == "scratch"


def test_linked_worktrees_key_per_worktree_and_the_main_checkout_keeps_the_repo_key(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    main = _make_repo(home / "repos", "brigade", "https://github.com/escoffier-labs/brigade.git")
    wt_a = home / "worktrees" / "a"
    wt_b = home / "worktrees" / "b"
    _git(main, "worktree", "add", "-q", "--detach", str(wt_a))
    _git(main, "worktree", "add", "-q", "--detach", str(wt_b))
    assert fleet_client.resolve_claim_target(main) == "escoffier-labs/brigade"
    assert fleet_client.resolve_claim_target(wt_a) == "escoffier-labs/brigade@a"
    assert fleet_client.resolve_claim_target(wt_b) == "escoffier-labs/brigade@b"


def test_repo_with_its_own_node_toml_keeps_the_workspace_name(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    repo = _make_repo(home / "repos", "ws", "https://github.com/acme/other-name.git")
    node_mod.ensure_identity(repo)
    assert fleet_client.resolve_claim_target(repo) == "ws"


def test_directory_outside_git_uses_its_own_name_not_the_home_name(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    plain = home / "notes" / "today"
    plain.mkdir(parents=True)
    assert fleet_client.resolve_claim_target(plain) == "today"


def test_git_failure_degrades_to_the_directory_name(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    repo = _make_repo(home / "repos", "scratch", "https://github.com/acme/scratch.git")

    def boom(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("brigade.fleet_claim_target.subprocess.run", boom)
    assert fleet_client.resolve_claim_target(repo) == "scratch"


def test_release_path_frees_a_claim_taken_under_the_run_path_resolver(tmp_path, monkeypatch, capsys):
    """Acquire and ``claims --release --path`` share one resolver (#1639)."""
    import threading

    from brigade import cli, fleet_hub

    db = tmp_path / "hub" / "fleet.db"
    server = fleet_hub.make_server("127.0.0.1", 0, db, "test-token-12345", allow_admin_writes=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", f"http://127.0.0.1:{server.server_address[1]}")
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "test-token-12345")
        home = _make_home(tmp_path, "homeA", monkeypatch)
        repo = _make_repo(home / "repos", "brigade", "https://github.com/escoffier-labs/brigade.git")
        target = fleet_client.resolve_claim_target(repo)
        assert target == "escoffier-labs/brigade"
        assert fleet_client.acquire_claim(target).granted
        assert [c["target"] for c in fleet_client.fetch_claims()] == [target]
        assert cli.main(["fleet", "claims", "--release", str(repo), "--path", "--json"]) == 0
        assert '"released": true' in capsys.readouterr().out
        assert fleet_client.fetch_claims() == []
    finally:
        server.shutdown()
        server.server_close()


def test_hosts_other_than_github_keep_their_host_so_equal_paths_do_not_collide(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    gh = _make_repo(home / "repos", "gh", "https://github.com/acme/repo.git")
    gl = _make_repo(home / "repos", "gl", "https://gitlab.com/acme/repo.git")
    sub = _make_repo(home / "repos", "sub", "git@gitlab.com:group/subgroup/repo.git")
    assert fleet_client.resolve_claim_target(gh) == "acme/repo"
    assert fleet_client.resolve_claim_target(gl) == "gitlab.com/acme/repo"
    assert fleet_client.resolve_claim_target(sub) == "gitlab.com/group/subgroup/repo"


def _boom(exc):
    def run(*args, **kwargs):
        raise exc

    return run


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired(cmd=["git"], timeout=5), PermissionError("denied")])
def test_transient_git_failure_raises_instead_of_changing_the_key(tmp_path, monkeypatch, exc):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    repo = _make_repo(home / "repos", "worker", "https://github.com/acme/repo.git")
    assert fleet_client.resolve_claim_target(repo) == "acme/repo"
    monkeypatch.setattr("brigade.fleet_claim_target.subprocess.run", _boom(exc))
    with pytest.raises(ClaimTargetError):
        fleet_client.resolve_claim_target(repo)


def test_transient_failure_reading_the_remote_raises_instead_of_using_the_directory_name(tmp_path, monkeypatch):
    from brigade import fleet_session_presence

    home = _make_home(tmp_path, "homeA", monkeypatch)
    repo = _make_repo(home / "repos", "worker", "https://github.com/acme/repo.git")
    monkeypatch.setattr(
        fleet_session_presence,
        "repository_identity",
        lambda target: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd=["git"], timeout=5)),
    )
    with pytest.raises(ClaimTargetError):
        fleet_client.resolve_claim_target(repo)


def test_a_directory_that_is_not_a_git_repo_is_not_an_error(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    plain = home / "notes"
    plain.mkdir()
    assert fleet_client.resolve_claim_target(plain) == "notes"


def test_release_path_reports_an_unresolvable_target_instead_of_a_traceback(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _make_home(tmp_path, "homeA", monkeypatch)
    repo = _make_repo(home / "repos", "worker", "https://github.com/acme/repo.git")
    monkeypatch.setattr(
        "brigade.fleet_claim_target.subprocess.run", _boom(subprocess.TimeoutExpired(cmd=["git"], timeout=5))
    )
    assert cli.main(["fleet", "claims", "--release", str(repo), "--path"]) == 1
    assert "cannot determine the claim target" in capsys.readouterr().err
