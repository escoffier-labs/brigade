import datetime as dt
import json
import os

import pytest

from brigade import outcome_ledger, outcome_ledger_plan as writer

from tests.test_outcome_ledger import ROOT, SKILL_DIR, SUBJECT, T, WORSE, World, owner_pr


NOW = dt.datetime(2026, 11, 10, 6, 17, tzinfo=dt.timezone.utc)
MAIN = "a" * 40
FP = "b" * 64


def candidate():
    return {
        "current": {"fingerprint": FP, "commit": MAIN},
        "status": "candidate",
        "approved": [{"fingerprint": "0" * 64}],
        "decision": {"code": "awaiting_owner", "decided_at": NOW.isoformat()},
        "trials": [{"fingerprint": FP, "outcome": "helped"}],
        "restore": None,
    }


def payload():
    return {"covers": {"main_sha": MAIN}, "subjects": {"skill:note": candidate()}}


def test_equal_ignores_clock_and_unrelated_main_commits():
    before, after = payload(), payload()
    after["covers"]["main_sha"] = "c" * 40
    after["subjects"]["skill:note"]["current"]["commit"] = "c" * 40
    after["subjects"]["skill:note"]["decision"]["decided_at"] = "later"
    assert writer.same_decisions(before, after)
    after["subjects"]["skill:note"]["trials"].append({"fingerprint": FP, "outcome": "hurt"})
    assert not writer.same_decisions(before, after)


def test_auto_branch_skips_queue_and_can_reuse_closed_branch():
    assert writer.branch_action(writer.AUTO_BRANCH, ["OPEN"], queued=True) == "queued"
    assert writer.branch_action(writer.AUTO_BRANCH, ["OPEN"], queued=False) == "update"
    assert writer.branch_action(writer.AUTO_BRANCH, ["CLOSED"], queued=False) == "create"


def test_approval_branch_never_reopens_rejected_or_merged_fingerprint():
    branch = writer.approval_branch("skill:note", FP)
    assert branch == "ledger/approve/note-bbbbbbbbbbbb"
    assert writer.branch_action(branch, ["CLOSED"], queued=False) == "closed"
    assert writer.branch_action(branch, ["MERGED"], queued=False) == "closed"
    assert writer.branch_action(branch, ["OPEN"], queued=False) == "update"
    assert writer.branch_action(branch, ["OPEN"], queued=True) == "queued"
    assert writer.branch_action(branch, [], queued=False) == "create"


@pytest.mark.parametrize("key,fp", [("skill:../note", FP), ("skill:note", "branch"), ("bad", FP)])
def test_invalid_branch_names_fail_closed(key, fp):
    with pytest.raises(outcome_ledger.LedgerError):
        writer.approval_branch(key, fp)


def test_verify_errors_must_all_be_retryable():
    stale = "skill:note: ledger is stale, commit aaaaaaa has an attest verdict still running"
    assert writer.retryable_errors([stale])
    assert writer.retryable_errors(["cutoff_at is older than the allowed ledger age"])
    assert not writer.retryable_errors([])
    assert not writer.retryable_errors([stale, "ledger differs from the recomputed ledger"])


@pytest.fixture()
def writer_script():
    import importlib.util

    path = ROOT / "scripts/ledger_decide.py"
    spec = importlib.util.spec_from_file_location("ledger_decide_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def seeded(tmp_path):
    world = World(tmp_path / "repo")
    raw = world.build(
        prev_raw=None, covers=world.c0, approved=(world.approved_row(world.c0),), cutoff=T % (21, "00:01:00Z")
    )
    world.commit({outcome_ledger.LEDGER_PATH: raw.decode()}, T % (21, "00:02:00Z"))
    return world


WRITER_NOW = dt.datetime(2026, 11, 21, 6, 17, tzinfo=dt.timezone.utc)


def test_release_bootstrap_counts_manifest_introduction(tmp_path, writer_script):
    world = World(tmp_path / "repo")
    world.git("tag", "v0.5.0", world.c0)
    manifest = "verify/manifests/skill-note.json"
    text = world.repo.show(world.c0, manifest).decode()
    before = world.commit({manifest: None}, T % (21, "01:00:00Z"))
    introduced = world.commit(
        {manifest: text, "scripts/skill_contract_check.py": "# contract\n"}, T % (21, "02:00:00Z")
    )
    world.gh.passed(introduced, done=T % (21, "02:05:00Z"))
    artifact = tmp_path / "artifact"
    writer_script.compute(world.ev, introduced, artifact, WRITER_NOW)
    plan, previous, head = writer_script.verify_artifact(world.ev, introduced, artifact, WRITER_NOW)
    body = plan.proposed["subjects"]["skill:note"]
    assert previous is None
    assert plan.state == "update"
    assert body["since_commit"] == before
    assert body["approved"] == [world.approved_row(world.c0)]
    assert [row["sha"] for row in body["trials"]] == [introduced]
    assert world.repo.changed_paths(introduced, head) == [outcome_ledger.LEDGER_PATH]


def test_equal_plan_tracks_no_new_decision_on_unrelated_main_commit(seeded, writer_script, tmp_path, monkeypatch):
    main = seeded.commit({"README.md": "unrelated\n"}, T % (21, "06:00:00Z"))
    artifact = tmp_path / "artifact"
    writer_script.compute(seeded.ev, main, artifact, WRITER_NOW)
    plan, _previous, _ = writer_script.verify_artifact(seeded.ev, main, artifact, WRITER_NOW)
    assert plan.state == "equal"
    monkeypatch.setattr(writer_script.Publisher, "auto", lambda *_: pytest.fail("equal plan tried to publish"))
    monkeypatch.setattr(writer_script.Publisher, "_decision", lambda *_: ("create", None))
    monkeypatch.setattr(writer_script.Publisher, "_gh", lambda *_: pytest.fail("no obsolete PR to close"))
    writer_script.publish(seeded.ev, main, artifact, WRITER_NOW)


def test_pending_evidence_leaves_no_ledger_payload(seeded, writer_script, tmp_path, monkeypatch, capsys):
    main = seeded.touch_skill_json(1, T % (21, "06:00:00Z"))
    artifact = tmp_path / "artifact"
    writer_script.compute(seeded.ev, main, artifact, WRITER_NOW)
    assert sorted(path.name for path in artifact.iterdir()) == ["plan.json"]
    monkeypatch.setattr(writer_script, "Publisher", lambda *_: pytest.fail("pending plan tried to publish"))
    writer_script.publish(seeded.ev, main, artifact, WRITER_NOW)
    assert "inside the grace window" in capsys.readouterr().out


def test_mid_batch_commit_waits_for_grace_then_counts_as_missing(seeded, writer_script, tmp_path):
    middle = seeded.touch_skill_json(1, T % (21, "05:00:00Z"))
    tip = seeded.touch_skill_json(2, T % (21, "06:00:00Z"))
    seeded.gh.passed(tip, done=T % (21, "06:10:00Z"))
    pending = writer.main_plan(seeded.ev, tip, WRITER_NOW)
    assert pending.state == "pending"
    assert pending.proposed is None
    assert middle[:12] in pending.reason

    later = WRITER_NOW + dt.timedelta(hours=2)
    artifact = tmp_path / "artifact"
    writer_script.compute(seeded.ev, tip, artifact, later)
    plan, _, _ = writer_script.verify_artifact(seeded.ev, tip, artifact, later)
    assert plan.proposed["covers"]["main_sha"] == tip
    rows = plan.proposed["subjects"]["skill:note"]["trials"]
    assert [(row["sha"], row["outcome"], row["reason"]) for row in rows] == [
        (middle, "hurt", "missing"),
        (tip, "helped", None),
    ]


def test_publish_defers_when_an_attest_rerun_starts_after_compute(seeded, writer_script, tmp_path, monkeypatch):
    main = seeded.touch_skill_json(1, T % (21, "06:00:00Z"))
    run = seeded.gh.passed(main, done=T % (21, "06:10:00Z"))
    artifact = tmp_path / "artifact"
    writer_script.compute(seeded.ev, main, artifact, WRITER_NOW)
    run.update(status="in_progress", run_attempt=2)
    monkeypatch.setattr(writer_script, "Publisher", lambda *_: pytest.fail("pending plan tried to publish"))
    with pytest.raises(writer_script.RetryPublication, match="still running"):
        writer_script.publish(seeded.ev, main, artifact, WRITER_NOW + dt.timedelta(minutes=1))


def test_artifact_from_older_main_is_stale(seeded, writer_script, tmp_path):
    main = seeded.repo.resolve("main")
    artifact = tmp_path / "artifact"
    writer_script.compute(seeded.ev, main, artifact, WRITER_NOW)
    newer = seeded.commit({"README.md": "advanced\n"}, T % (21, "06:19:00Z"))
    with pytest.raises(writer_script.RetryPublication, match="main advanced"):
        writer_script.verify_artifact(seeded.ev, newer, artifact, WRITER_NOW)


def test_queued_automatic_pull_has_no_side_effects(seeded, writer_script, monkeypatch, capsys):
    publisher = writer_script.Publisher(seeded.ev, seeded.c0)
    monkeypatch.setattr(publisher, "_decision", lambda _: ("queued", {"number": 5}))
    monkeypatch.setattr(publisher, "_push", lambda *_: pytest.fail("queued branch was pushed"))
    monkeypatch.setattr(publisher, "_gh", lambda *_: pytest.fail("queued pull request was changed"))
    publisher.auto(seeded.c0)
    assert "in the merge queue" in capsys.readouterr().out


@pytest.mark.parametrize("existing", [False, True], ids=["create", "update"])
def test_automatic_pull_pushes_dispatches_and_enables_auto_merge(seeded, writer_script, monkeypatch, existing):
    publisher = writer_script.Publisher(seeded.ev, seeded.c0)
    calls = []
    pull = {"number": 6} if existing else None
    monkeypatch.setattr(publisher, "_decision", lambda _: ("update" if existing else "create", pull))
    monkeypatch.setattr(publisher, "_push", lambda *args: calls.append(("push", *args)))
    monkeypatch.setattr(publisher, "_open", lambda *args: calls.append(("open", args[0])) or {"number": 6})
    monkeypatch.setattr(publisher, "_gh", lambda *args: calls.append(args))
    publisher.auto(seeded.c0)
    assert calls == [
        ("push", writer.AUTO_BRANCH, seeded.c0, pull),
        *([] if existing else [("open", writer.AUTO_BRANCH)]),
        ("workflow", "run", "ci.yml", "--repo", seeded.ev.slug, "--ref", writer.AUTO_BRANCH),
        ("pr", "merge", "6", "--repo", seeded.ev.slug, "--auto", "--squash", "--match-head-commit", seeded.c0),
    ]


def qualifying(world, skill_text=WORSE):
    first = world.commit({SUBJECT: skill_text}, T % (21, "03:00:00Z"))
    second = world.touch_skill_json(1, T % (21, "04:00:00Z"))
    world.gh.passed(first, done=T % (21, "03:10:00Z"))
    world.gh.passed(second, done=T % (21, "04:10:00Z"))
    assert writer.main_plan(world.ev, second, WRITER_NOW).state == "update"
    plan = writer.main_plan(world.ev, second, WRITER_NOW)
    raw = outcome_ledger.canonical_bytes(plan.proposed)
    main = world.commit({outcome_ledger.LEDGER_PATH: raw.decode()}, T % (21, "06:18:00Z"))
    updated = writer.main_plan(world.ev, main, WRITER_NOW + dt.timedelta(minutes=2))
    assert updated.state == "equal"
    return main, raw, updated.proposed


def test_approval_gets_real_pr_number_without_enabling_auto_merge(seeded, writer_script, monkeypatch):
    main, raw, proposed = qualifying(seeded)
    [approval] = writer.approvals(proposed)
    publisher = writer_script.Publisher(seeded.ev, main)
    pushes = []
    bodies = []
    dispatches = []
    monkeypatch.setattr(publisher, "_decision", lambda _: ("create", None))
    monkeypatch.setattr(publisher, "_push", lambda branch, sha, _pull: pushes.append((branch, sha)))
    monkeypatch.setattr(publisher, "_open", lambda branch, title, body: bodies.append(body) or {"number": 13})
    monkeypatch.setattr(publisher, "_dispatch", dispatches.append)
    publisher.approval(raw, proposed, approval, WRITER_NOW + dt.timedelta(minutes=2))

    assert len(pushes) == 2
    assert all(branch == approval.branch for branch, _ in pushes)
    final = pushes[-1][1]
    final_raw = seeded.repo.show(final, outcome_ledger.LEDGER_PATH)
    row = outcome_ledger.parse_ledger(final_raw)["subjects"]["skill:note"]["approved"][-1]
    assert row["pr"] == 13 and row["fingerprint"] == approval.fingerprint
    assert "## Bundle diff" in bodies[0] and "## Attest trials" in bodies[0] and "/actions/runs/" in bodies[0]
    assert dispatches == [approval.branch]
    owner_pr(seeded, head=final, commit=final, number=13)
    assert seeded.verify(final_raw, raw, head=final, pr=13, now=WRITER_NOW + dt.timedelta(minutes=3)) == []


def test_rejected_approval_stays_closed(seeded, writer_script, monkeypatch, capsys):
    main, raw, proposed = qualifying(seeded)
    [approval] = writer.approvals(proposed)
    publisher = writer_script.Publisher(seeded.ev, main)
    monkeypatch.setattr(publisher, "_decision", lambda _: ("closed", None))
    monkeypatch.setattr(publisher, "_push", lambda *_: pytest.fail("rejected approval branch was pushed"))
    monkeypatch.setattr(publisher, "_gh", lambda *_: pytest.fail("rejected approval was reopened"))
    publisher.approval(raw, proposed, approval, WRITER_NOW)
    assert "is closed" in capsys.readouterr().out


def test_rollback_artifact_restores_the_bundle_and_rejects_changed_bytes(seeded, writer_script, tmp_path):
    main = seeded.commit({SUBJECT: WORSE, f"{SKILL_DIR}/new.txt": "new file\n"}, T % (21, "04:00:00Z"))
    seeded.gh.failed(main, done=T % (21, "04:10:00Z"))
    artifact = tmp_path / "artifact"
    writer_script.compute(seeded.ev, main, artifact, WRITER_NOW)
    plan, _previous, head = writer_script.verify_artifact(seeded.ev, main, artifact, WRITER_NOW)
    assert plan.proposed["subjects"]["skill:note"]["decision"]["action"] == "rollback"
    assert seeded.repo.snapshot(head, SKILL_DIR) == seeded.repo.snapshot(seeded.c0, SKILL_DIR)
    assert (artifact / SUBJECT).read_bytes() == seeded.repo.show(seeded.c0, SUBJECT)
    (artifact / SUBJECT).write_bytes(b"unexpected\n")
    with pytest.raises(writer_script.PublicationError, match="different copy"):
        writer_script.verify_artifact(seeded.ev, main, artifact, WRITER_NOW)


def test_cli_reads_main_and_links_open_approval(seeded, monkeypatch, capsys):
    from brigade import ci_attestation
    from brigade.cli import main

    commit, _, proposed = qualifying(seeded)
    [approval] = writer.approvals(proposed)
    owner = seeded.ev.slug.split("/", 1)[0]
    monkeypatch.setattr(ci_attestation, "repo_slug", lambda _: seeded.ev.slug)

    def gh(endpoint):
        if endpoint.endswith("/git/ref/heads/main"):
            return {"object": {"sha": commit}}
        assert "/pulls?" in endpoint and "state=open" in endpoint
        return [
            {
                "number": 13,
                "state": "open",
                "head": {"sha": commit, "ref": approval.branch, "label": f"{owner}:{approval.branch}"},
            }
        ]

    monkeypatch.setattr(ci_attestation, "default_gh_api", gh)
    assert main(["outcome", "ledger", "--target", str(seeded.root), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["main"] == commit
    assert result["ledger"]["subjects"]["skill:note"]["status"] == "candidate"
    assert len(result["ledger"]["subjects"]["skill:note"]["trials"]) == 2
    assert result["open_approval_prs"][approval.branch].endswith("/pull/13")


def test_cli_dry_run_only_reads_main(seeded, monkeypatch, capsys):
    from brigade import ci_attestation
    from brigade.cli import main

    commit = seeded.repo.resolve("main")
    monkeypatch.setattr(ci_attestation, "repo_slug", lambda _: seeded.ev.slug)

    def gh(endpoint):
        if endpoint.endswith("/git/ref/heads/main"):
            return {"object": {"sha": commit}}
        if endpoint == f"repos/{seeded.ev.slug}":
            return {"id": seeded.ev.repo_id}
        return seeded.gh(endpoint)

    monkeypatch.setattr(ci_attestation, "default_gh_api", gh)
    before = seeded.repo.show(commit, outcome_ledger.LEDGER_PATH)
    assert main(["outcome", "ledger", "--target", str(seeded.root), "--plan", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "equal"
    assert seeded.repo.resolve("HEAD") == commit
    assert (seeded.root / outcome_ledger.LEDGER_PATH).read_bytes() == before


@pytest.mark.parametrize("origin", [None, "https://github.com/example/ledger-fork.git"])
def test_work_brief_summarizes_committed_ledger_only(seeded, monkeypatch, capsys, origin):
    from brigade import ci_attestation
    from brigade.work_cmd.session import briefing

    qualifying(seeded)
    if origin:
        seeded.git("remote", "add", "origin", origin)
    (seeded.root / outcome_ledger.LEDGER_PATH).write_text("uncommitted content\n", encoding="utf-8")
    monkeypatch.setattr(ci_attestation, "default_gh_api", lambda *_: pytest.fail("work brief looked up GitHub"))
    assert briefing.brief(target=seeded.root) == 0
    [line] = [line for line in capsys.readouterr().out.splitlines() if line.startswith("ledger:")]
    assert line.startswith("ledger: note candidate (2 helped), 1 awaiting owner (https://github.com/")
    slug = "example/ledger-fork" if origin else writer.REPO_SLUG
    assert f"https://github.com/{slug}/pulls?" in line
    assert "ledger%2Fapprove%2Fnote-" in line


def test_existing_current_approval_recovers_a_missing_ci_dispatch(seeded, writer_script, monkeypatch):
    main, raw, proposed = qualifying(seeded)
    [approval] = writer.approvals(proposed)
    publisher = writer_script.Publisher(seeded.ev, main)
    final = writer.with_approval(seeded.ev, raw, proposed, approval, 13)
    head = writer_script.candidate_commit(
        seeded.repo, main, outcome_ledger.canonical_bytes(final), {}, title="approval"
    )
    monkeypatch.setattr(publisher, "_decision", lambda _: ("update", {"number": 13, "head": {"sha": head}}))
    monkeypatch.setattr(publisher, "_push", lambda *_: pytest.fail("current approval head was replaced"))
    dispatched = []
    monkeypatch.setattr(publisher, "_dispatch", dispatched.append)
    api_calls = []

    def gh(endpoint):
        api_calls.append(endpoint)
        return {"total_count": 0}

    monkeypatch.setattr(writer_script.ci_attestation, "default_gh_api", gh)
    publisher.approval(raw, proposed, approval, WRITER_NOW + dt.timedelta(minutes=3))
    assert len(api_calls) == 1 and f"head_sha={head}" in api_calls[0]
    assert dispatched == [approval.branch]


@pytest.mark.parametrize("contents", [None, "{broken"], ids=["absent", "malformed"])
def test_existing_approval_rebuilds_unreadable_head(seeded, writer_script, monkeypatch, contents):
    main, raw, proposed = qualifying(seeded)
    [approval] = writer.approvals(proposed)
    head = seeded.commit({outcome_ledger.LEDGER_PATH: contents}, T % (21, "06:19:00Z"))
    pull = {"number": 13, "head": {"sha": head}}
    publisher = writer_script.Publisher(seeded.ev, main)
    monkeypatch.setattr(publisher, "_decision", lambda _: ("update", pull))
    pushes = []
    calls = []
    monkeypatch.setattr(publisher, "_push", lambda *args: pushes.append(args))
    monkeypatch.setattr(publisher, "_gh", lambda *args, **kwargs: calls.append(args))
    assert not publisher.approval(raw, proposed, approval, WRITER_NOW + dt.timedelta(minutes=2))
    [(branch, new_head, old_pull)] = pushes
    assert branch == approval.branch and old_pull == pull
    candidate = outcome_ledger.parse_ledger(seeded.repo.show(new_head, outcome_ledger.LEDGER_PATH))
    assert candidate["subjects"]["skill:note"]["approved"][-1]["pr"] == 13
    assert [call[:2] for call in calls] == [("api", "--method"), ("workflow", "run")]


def test_existing_approval_retries_missing_commit(seeded, writer_script):
    publisher = writer_script.Publisher(seeded.ev, seeded.c0)
    approval = writer.Approval("skill:note", FP)
    with pytest.raises(writer_script.RetryPublication, match="head commit is missing locally"):
        publisher._refresh_approval({"head": {"sha": MAIN}}, b"", approval, WRITER_NOW)


def test_approval_body_identifies_truncated_diff(seeded, writer_script):
    main, _, proposed = qualifying(seeded, skill_text=WORSE + ("long line\n" * 2500) + "end of diff\n")
    [approval] = writer.approvals(proposed)
    body = writer_script.approval_body(seeded.ev, proposed, approval)
    link = f"https://github.com/{seeded.ev.slug}/compare/{seeded.c0}...{main}"
    assert f"Diff truncated. See the [full comparison]({link})." in body
    assert f"Changed files\n\n    {SUBJECT}\n" in body
    assert f"fingerprint `{approval.fingerprint}`" in body
    assert "end of diff" not in body


def test_publish_equal_closes_auto_and_opens_approval(seeded, writer_script, tmp_path, monkeypatch):
    main, _, proposed = qualifying(seeded)
    [approval] = writer.approvals(proposed)
    now = WRITER_NOW + dt.timedelta(minutes=2)
    artifact = tmp_path / "plan"
    writer_script.compute(seeded.ev, main, artifact, now)
    publisher = writer_script.Publisher
    auto = {"number": 9}
    monkeypatch.setattr(
        publisher, "_decision", lambda _, branch: ("update", auto) if branch == writer.AUTO_BRANCH else ("create", None)
    )
    monkeypatch.setattr(publisher, "_check_main", lambda self: None)
    monkeypatch.setattr(
        publisher,
        "_open",
        lambda _, branch, *_args: {"number": 13} if branch == approval.branch else pytest.fail("unexpected PR"),
    )
    calls = []
    pushes = []
    monkeypatch.setattr(publisher, "_gh", lambda _, *args: calls.append(args))
    monkeypatch.setattr(publisher, "_push", lambda _, *args: pushes.append(args))
    writer_script.publish(seeded.ev, main, artifact, now)
    assert calls == [
        (
            "pr",
            "close",
            "9",
            "--repo",
            seeded.ev.slug,
            "--comment",
            "Closing this automatic proposal because the recomputed ledger matches main.",
        ),
        ("workflow", "run", "ci.yml", "--repo", seeded.ev.slug, "--ref", approval.branch),
    ]
    assert [branch for branch, _, _ in pushes] == [approval.branch, approval.branch]
    raw = seeded.repo.show(pushes[-1][1], outcome_ledger.LEDGER_PATH)
    assert outcome_ledger.parse_ledger(raw)["subjects"][approval.key]["approved"][-1]["pr"] == 13


def test_publish_creates_one_approval_and_skips_invalid_subject(seeded, writer_script, monkeypatch, tmp_path, capsys):
    main, raw, proposed = qualifying(seeded)
    body = proposed["subjects"]["skill:note"]
    proposed["subjects"].update({"skill:Bad.Name": body, "skill:other": body})
    plan = writer.Plan(main, "equal", "test plan", proposed)
    monkeypatch.setattr(writer_script, "verify_artifact", lambda *_: (plan, raw, None))
    publisher = writer_script.Publisher
    monkeypatch.setattr(publisher, "close_obsolete_auto", lambda _: None)
    monkeypatch.setattr(publisher, "_decision", lambda *_: ("create", None))
    opened = []
    monkeypatch.setattr(publisher, "_open", lambda _, branch, *_args: opened.append(branch) or {"number": 13})
    monkeypatch.setattr(publisher, "_push", lambda *_: None)
    monkeypatch.setattr(publisher, "_dispatch", lambda *_: None)
    writer_script.publish(seeded.ev, main, tmp_path, WRITER_NOW)
    assert opened == [writer.approval_branch("skill:note", body["current"]["fingerprint"])]
    assert "LedgerError: subject 'skill:Bad.Name' cannot be encoded" in capsys.readouterr().out


@pytest.mark.parametrize("damage", ["extra", "metadata", "missing"])
def test_verify_artifact_rejects_changed_file_set_or_plan(seeded, writer_script, tmp_path, damage):
    main = seeded.commit({SUBJECT: WORSE}, T % (21, "04:00:00Z"))
    seeded.gh.failed(main, done=T % (21, "04:10:00Z"))
    artifact = tmp_path / "artifact"
    writer_script.compute(seeded.ev, main, artifact, WRITER_NOW)
    message = "artifact files disagree"
    if damage == "extra":
        (artifact / "extra").touch()
    elif damage == "metadata":
        (artifact / "plan.json").write_text(json.dumps({"main": main, "state": "equal"}))
        message = "plan state disagrees"
    else:
        (artifact / SUBJECT).unlink()
    with pytest.raises(writer_script.PublicationError, match=message):
        writer_script.verify_artifact(seeded.ev, main, artifact, WRITER_NOW)


@pytest.mark.parametrize("race", [None, "queue", "lease"])
def test_push_real_lease_and_queue_recheck(seeded, writer_script, tmp_path, monkeypatch, race):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    remote = tmp_path / "origin.git"
    seeded.git("init", "--bare", str(remote))
    seeded.git("remote", "add", "origin", str(remote))
    main = seeded.repo.resolve("HEAD")
    old = seeded.c0
    ref = f"refs/heads/{writer.AUTO_BRANCH}"
    seeded.git("push", "origin", f"{main}:refs/heads/main", f"{old}:{ref}")
    publisher = writer_script.Publisher(seeded.ev, main)
    pull = {"number": 9, "head": {"sha": old}, "node_id": "test-node"}
    monkeypatch.setattr(publisher, "_ref", lambda branch: main if branch == "main" else old)
    monkeypatch.setattr(writer, "branch_prs", lambda *_: [{**pull, "state": "open"}])
    queue_checks = []

    def graphql(*args):
        assert args[:2] == ("api", "graphql")
        queue_checks.append(args)
        if race == "lease" and len(queue_checks) == 2:
            seeded.git("push", "origin", f"{main}:{ref}")
        row = {"state": "OPEN", "headRefOid": old, "isInMergeQueue": race == "queue" and len(queue_checks) == 2}
        return json.dumps({"data": {"node": row}}).encode()

    monkeypatch.setattr(publisher, "_gh", graphql)
    action, live_pull = publisher._decision(writer.AUTO_BRANCH)
    assert action == "update"
    if race == "queue":
        with pytest.raises(writer_script.RetryPublication, match="in the merge queue"):
            publisher._push(writer.AUTO_BRANCH, main, live_pull)
    elif race == "lease":
        # The candidate would rewind the concurrently advanced branch without the lease.
        with pytest.raises(writer_script.PublicationError, match="stale info"):
            publisher._push(writer.AUTO_BRANCH, old, live_pull)
    else:
        publisher._push(writer.AUTO_BRANCH, main, live_pull)
    assert len(queue_checks) == 2
    assert seeded.git("ls-remote", "origin", ref).split()[0] == (old if race == "queue" else main)
