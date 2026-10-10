"""Repo claim key derivation (#1639): never the home identity directory name."""

from __future__ import annotations

import os
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


@pytest.fixture
def journal_events(monkeypatch):
    """Capture the transport boundary while appending real journal events."""
    from brigade import run_journal

    events = []

    def report(event, **kwargs):
        events.append(event)
        return True

    monkeypatch.setattr(fleet_client, "report_event", report)
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "https://hub.example.invalid")

    def append(repo: Path):
        journal = repo / ".brigade" / "runs" / "example-run" / "events" / "lifecycle.jsonl"
        run_journal.append_event(
            journal,
            run_id="example-run",
            event_type="run.created",
            payload={},
            idempotency_key="created",
            expected_previous_sequence=0,
        )
        return events[-1]

    return append


def test_journal_events_without_a_hub_skip_claim_key_resolution(tmp_path, monkeypatch, caplog):
    from brigade import fleet_claim_target, run_journal

    home = _make_home(tmp_path, "homeA", monkeypatch)
    repo = _make_repo(home / "repos", "one", "https://github.com/acme/one.git")
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise ClaimTargetError("temporary git failure")

    monkeypatch.setattr(fleet_claim_target, "_git", fail)
    journal = repo / ".brigade" / "runs" / "example-run" / "events" / "lifecycle.jsonl"
    event = run_journal.append_event(
        journal,
        run_id="example-run",
        event_type="run.created",
        payload={},
        idempotency_key="created",
        expected_previous_sequence=0,
    )
    assert event.sequence == 1
    assert calls == []
    assert "repo-key-fallback" not in caplog.text


def test_journal_events_from_unrelated_repos_under_one_home_have_distinct_keys(tmp_path, monkeypatch, journal_events):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    one = _make_repo(home / "repos", "one", "https://github.com/acme/one.git")
    two = _make_repo(home / "repos", "two", "https://github.com/acme/two.git")
    assert journal_events(one)["repo"] == "acme/one"
    assert journal_events(two)["repo"] == "acme/two"


@pytest.mark.parametrize("home_identity", [True, False], ids=["home-workspace", "no-workspace"])
def test_non_git_journal_events_keep_the_workspace_fallback(tmp_path, monkeypatch, journal_events, home_identity):
    home = _make_home(tmp_path, "homeA", monkeypatch) if home_identity else tmp_path / "homeA"
    if not home_identity:
        monkeypatch.setenv("BRIGADE_HOME", str(home / ".brigade"))
    root = home / "repos" / "notes"
    root.mkdir(parents=True)
    assert journal_events(root)["repo"] == ("homeA" if home_identity else None)


@pytest.mark.parametrize("local_identity", [True, False], ids=["repo-workspace", "home-identity"])
def test_git_journal_events_match_claims_from_the_project_root(tmp_path, monkeypatch, journal_events, local_identity):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    root = _make_repo(home / "repos", "checkout", "https://github.com/acme/project.git")
    if local_identity:
        identity = node_mod.NodeIdentity(node_id=NODE_A, hostname="fleet-test", roles=(), platform="test")
        path = node_mod.node_path(root)
        path.parent.mkdir(parents=True)
        path.write_text(node_mod._format_node_toml(identity), encoding="utf-8")
    event = journal_events(root)
    assert event["repo"] == ("checkout" if local_identity else "acme/project")
    assert event["repo"] == fleet_client.resolve_claim_target(root)


def test_journal_events_from_clones_under_different_homes_have_the_same_key(tmp_path, monkeypatch, journal_events):
    home_a = _make_home(tmp_path, "homeA", monkeypatch)
    one = _make_repo(home_a / "repos", "one", "https://github.com/acme/project.git")
    assert journal_events(one)["repo"] == "acme/project"
    home_b = _make_home(tmp_path, "homeB", monkeypatch)
    two = _make_repo(home_b / "repos", "two", "git@github.com:acme/project.git")
    assert journal_events(two)["repo"] == "acme/project"


def test_journal_events_fall_back_on_transient_git_failure_and_recover(tmp_path, monkeypatch, journal_events, caplog):
    from brigade import fleet_claim_target

    home = _make_home(tmp_path, "homeA", monkeypatch)
    repo = _make_repo(home / "repos", "one", "https://github.com/acme/one.git")
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise ClaimTargetError("temporary git failure")

        patch.setattr(fleet_claim_target, "_git", fail)
        assert journal_events(repo)["repo"] == "homeA"
    assert "repo-key-fallback" in caplog.text
    # A new journal entry must resolve again, rather than caching the fallback.
    assert journal_events(repo / "nested")["repo"] == "acme/one"


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


def _fail_remote_reads(monkeypatch, state: dict, outcome):
    """Stub only the ``git ls-remote --get-url`` read; every other git call is real.

    ``outcome`` is a CompletedProcess to return or an exception to raise.
    Clearing ``state["fail"]`` lets the real git run again (recovery).
    """
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if state["fail"] and list(cmd[:2]) == ["git", "ls-remote"]:
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("brigade.fleet_claim_target.subprocess.run", run)


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess(["git"], 128, stdout="", stderr="fatal: unable to read config: I/O error"),
        subprocess.TimeoutExpired(cmd=["git"], timeout=5),
    ],
    ids=["exit-128", "timeout"],
)
def test_failed_remote_read_raises_and_the_key_recovers_once_git_does(tmp_path, monkeypatch, outcome):
    """A failed remote read must never become the toplevel-name key, and
    must not be remembered after git recovers."""
    home = _make_home(tmp_path, "homeA", monkeypatch)
    repo = _make_repo(home / "repos", "worker", "https://github.com/acme/repo.git")
    state = {"fail": True}
    _fail_remote_reads(monkeypatch, state, outcome)
    with pytest.raises(ClaimTargetError):
        fleet_client.resolve_claim_target(repo)
    state["fail"] = False
    assert fleet_client.resolve_claim_target(repo) == "acme/repo"


def test_no_origin_remote_is_a_legitimate_fallback_to_the_toplevel_name(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    repo = _make_repo(home / "repos", "worker", None)
    assert fleet_client.resolve_claim_target(repo) == "worker"
    # A remote that is not called origin does not count: git echoes the name back.
    other = _make_repo(home / "repos", "other", None)
    _git(other, "remote", "add", "upstream", "https://github.com/acme/upstream.git")
    assert fleet_client.resolve_claim_target(other) == "other"


def test_insteadof_aliases_are_applied_so_machines_agree_on_the_key(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    aliased = _make_repo(home / "repos", "aliased", "gh:acme/repo")
    _git(aliased, "config", "url.https://github.com/.insteadOf", "gh:")
    plain = _make_repo(home / "repos", "plain", "https://github.com/acme/repo.git")
    assert fleet_client.resolve_claim_target(aliased) == "acme/repo"
    assert fleet_client.resolve_claim_target(plain) == "acme/repo"
    # Without the rewrite the alias is just another host, never the github repo.
    bare = _make_repo(home / "repos", "bare", "gh:acme/repo")
    assert fleet_client.resolve_claim_target(bare) != "acme/repo"


def test_an_unparseable_origin_still_keys_on_the_remote_not_the_checkout_name(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    one = _make_repo(home / "repos", "one", "/srv/git/Shared-Repo.git")
    two = _make_repo(home / "repos", "two", "/srv/git/Shared-Repo.git")
    other = _make_repo(home / "repos", "three", "/srv/git/Other-Repo.git")
    assert fleet_client.resolve_claim_target(one) == "/srv/git/Shared-Repo"
    assert fleet_client.resolve_claim_target(two) == fleet_client.resolve_claim_target(one)
    assert fleet_client.resolve_claim_target(other) == "/srv/git/Other-Repo"


def test_remote_credentials_never_reach_the_key(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    repo = _make_repo(home / "repos", "worker", "https://user:s3cret-token@Example.COM/Group/Repo.git")
    key = fleet_client.resolve_claim_target(repo)
    assert key == "example.com/Group/Repo"
    assert "s3cret" not in key and "user" not in key


def test_missing_git_fails_closed_inside_a_checkout_but_not_outside_one(tmp_path, monkeypatch):
    home = _make_home(tmp_path, "homeA", monkeypatch)
    repo = _make_repo(home / "repos", "worker", "https://github.com/acme/repo.git")
    nested = repo / "pkg"
    nested.mkdir()
    plain = home / "notes"
    plain.mkdir()

    def inside_sandbox_checkout(cwd: Path) -> bool:
        # Bound the walk to the sandbox: the host's own tmp dir may be a checkout.
        return any((p / ".git").exists() for p in (cwd, *cwd.parents) if p.is_relative_to(tmp_path))

    monkeypatch.setattr("brigade.fleet_claim_target._inside_checkout", inside_sandbox_checkout)
    monkeypatch.setattr("brigade.fleet_claim_target.subprocess.run", _boom(FileNotFoundError("git")))
    with pytest.raises(ClaimTargetError):
        fleet_client.resolve_claim_target(repo)
    with pytest.raises(ClaimTargetError):
        fleet_client.resolve_claim_target(nested)
    assert fleet_client.resolve_claim_target(plain) == "notes"


def _ceiling_layout(tmp_path, monkeypatch):
    """A fake checkout at ``outer`` with a plain ``sub/child`` below it, git missing."""
    home = _make_home(tmp_path, "homeA", monkeypatch)
    outer = home / "outer"
    (outer / ".git").mkdir(parents=True)
    child = outer / "sub" / "child"
    child.mkdir(parents=True)
    monkeypatch.setattr("brigade.fleet_claim_target.subprocess.run", _boom(FileNotFoundError("git")))
    return outer, child


def test_missing_git_walk_honors_git_ceiling_directories(tmp_path, monkeypatch):
    """With git installed a child under a ceiling is outside git; the fallback must agree."""
    outer, child = _ceiling_layout(tmp_path, monkeypatch)
    # The sandbox itself is always a ceiling so the host's own checkouts never matter.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(ClaimTargetError):
        fleet_client.resolve_claim_target(child)
    sub = str(outer / "sub")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", os.pathsep.join([str(tmp_path), sub]))
    assert fleet_client.resolve_claim_target(child) == "child"
    # An empty entry means "do not resolve symlinks for the rest", not "ignore the rest".
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", os.pathsep.join([str(tmp_path), "", sub]))
    assert fleet_client.resolve_claim_target(child) == "child"


def test_missing_git_walk_never_hides_the_checkout_at_the_ceiling_itself(tmp_path, monkeypatch):
    outer, _child = _ceiling_layout(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", os.pathsep.join([str(tmp_path), str(outer)]))
    with pytest.raises(ClaimTargetError):
        fleet_client.resolve_claim_target(outer)


def test_missing_git_walk_stops_at_a_filesystem_boundary(tmp_path, monkeypatch):
    outer, child = _ceiling_layout(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.delenv("GIT_DISCOVERY_ACROSS_FILESYSTEM", raising=False)
    monkeypatch.setattr(
        "brigade.fleet_claim_target._device", lambda path: 2 if path.resolve() == outer.resolve() else 1
    )
    assert fleet_client.resolve_claim_target(child) == "child"
    monkeypatch.setenv("GIT_DISCOVERY_ACROSS_FILESYSTEM", "1")
    with pytest.raises(ClaimTargetError):
        fleet_client.resolve_claim_target(child)


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
