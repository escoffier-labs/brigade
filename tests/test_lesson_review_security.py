"""Security regressions for automatic lesson admission and publication."""

import json

import pytest

from brigade import lesson_activity as activity, lesson_admission as admission, lesson_review as review
from tests.test_lesson_review import event, proposal, seat, scope as lesson_scope


@pytest.fixture
def scope(tmp_path, monkeypatch):
    return lesson_scope.__wrapped__(tmp_path, monkeypatch)


def test_authenticated_receipt_still_requires_subject_binding(scope, monkeypatch):
    event(scope)
    event(scope)
    candidate = proposal(scope)
    path = scope / candidate["evidence"]
    receipt = json.loads(path.read_text())
    receipt.pop("subject_binding")
    from brigade import localio, receipt_signing

    receipt["digests"] = {"receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"})}
    receipt_signing.stamp_workspace_signature(scope, receipt)
    path.write_text(json.dumps(receipt))
    seat(monkeypatch, candidate)
    assert review.run(scope, now=111)["status"] == "rejected"
    assert not (scope / "memory/cards").exists()


def test_correction_cannot_use_single_character(scope):
    candidate = proposal(scope)
    candidate.update(kind="user-correction", quote="a")
    events = [{"id": 2, "kind": "UserPromptSubmit", "text": "a"}]
    with pytest.raises(ValueError, match="quote"):
        admission.admit(scope, candidate, events, {})


def test_correction_must_bind_content(scope):
    candidate = proposal(scope)
    candidate.update(kind="user-correction", content="Unrelated content about a different task.")
    events = [{"id": 2, "kind": "UserPromptSubmit", "text": candidate["quote"]}]
    with pytest.raises(ValueError, match="content"):
        admission.admit(scope, candidate, events, {})


def test_rejections_back_off_then_quarantine(scope, monkeypatch):
    event(scope)
    event(scope)
    calls = seat(monkeypatch, {"kind": "invalid"})
    assert review.run(scope, now=111)["status"] == "rejected"
    assert review.run(scope, now=112)["status"] == "backoff"
    assert len(calls) == 2
    review.run(scope, now=1000)
    assert review.run(scope, now=3000)["status"] == "quarantined"
    state = activity.load(scope)
    assert state["reviewed"] == state["sequence"]
    assert "invalid" not in json.dumps(state["quarantine"])
    event(scope, now=3001)
    event(scope, now=3001)
    seat(monkeypatch)
    assert review.run(scope, now=3012)["status"] == "committed"


def test_prompt_hook_does_not_write_workspace(scope, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cache = tmp_path.parent / (tmp_path.name + "-cache")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    before = sorted(str(p.relative_to(scope)) for p in scope.rglob("*"))
    activity.record_event(scope, "UserPromptSubmit", {"prompt": "Please use atomic writes for publication."}, now=99)
    assert sorted(str(p.relative_to(scope)) for p in scope.rglob("*")) == before
    event(scope)
    assert any(e["kind"] == "UserPromptSubmit" for e in activity.load(scope)["events"])


def test_runs_symlink_is_refused(scope, monkeypatch, tmp_path):
    event(scope)
    event(scope)
    outside = tmp_path / "outside"
    outside.mkdir()
    (scope / activity.ROOT / "runs").symlink_to(outside, target_is_directory=True)
    seat(monkeypatch)
    assert review.run(scope, now=111)["status"] in {"rejected", "retry"}
    assert not list(outside.iterdir())


def test_failure_survives_twenty_tool_calls(scope):
    activity.record_event(scope, "PostToolUseFailure", {"error": "Publication failed before replacement."}, now=99)
    for _ in range(20):
        activity.record_event(scope, "PreToolUse", {"tool_input": "read source"}, now=100)
        event(scope)
    assert any(e["kind"] == "PostToolUseFailure" for e in activity.load(scope)["events"])


@pytest.mark.parametrize(
    "damage", ["unregistered", "content", "subject", "same-verifier", "old", "same-tree", "no-failure", "not-verify"]
)
def test_receipt_binding_and_failure_sequence(scope, monkeypatch, damage):
    event(scope)
    event(scope)
    candidate = proposal(scope)
    path = scope / candidate["evidence"]
    receipt = json.loads(path.read_text())
    if damage == "unregistered":
        (scope / "verify/manifests/lesson.json").unlink()
    elif damage == "content":
        receipt["subject_binding"]["content_fingerprint"] = "sha256:" + "0" * 64
    elif damage == "subject":
        receipt["subject_binding"]["artifact_id"] = "different-card"
    elif damage == "same-verifier":
        receipt["subject_binding"]["verifier_identity"]["session_id"] = "example-session"
    elif damage == "old":
        receipt["completed_at"] = "1970-01-01T00:00:01Z"
    elif damage == "same-tree":
        receipt["tree_fingerprint"] = "b" * 40
    elif damage == "no-failure":
        candidate.pop("failure_evidence")
    else:
        state = activity.load(scope)
        for observation in state["events"]:
            observation.pop("verify_command", None)
        activity.save(scope, state)
    from brigade import localio, receipt_signing

    receipt["digests"] = {"receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"})}
    receipt_signing.stamp_workspace_signature(scope, receipt)
    path.write_text(json.dumps(receipt))
    seat(monkeypatch, candidate)
    assert review.run(scope, now=111)["status"] == "rejected"
    assert not (scope / "memory/cards").exists()


def test_only_own_lesson_is_delivered_to_review(scope, monkeypatch):
    event(scope)
    event(scope)
    candidate = proposal(scope)
    inbox = scope / ".claude/memory-handoffs"
    inbox.mkdir(parents=True)
    unrelated = inbox / "unrelated.md"
    unrelated.write_bytes(b"\xff invalid handoff")
    attacker = inbox / "unreviewed.md"
    unreviewed = review._draft_text(
        scope,
        {
            "lesson": {**candidate, "target_card": "unreviewed.md"},
            "run_id": "unreviewed",
            "digest": "0" * 64,
            "inbox": ".claude/memory-handoffs",
        },
    )
    attacker.write_text(unreviewed)
    seat(monkeypatch, candidate)
    assert review.run(scope, now=111)["status"] == "committed"
    assert unrelated.read_bytes() == b"\xff invalid handoff"
    assert attacker.read_text() == unreviewed
    assert len(list((inbox / "processed").glob("*.md"))) == 1
    drafts = list((scope / "memory/handoff-inbox").glob("*.md"))
    assert len(drafts) == 1
    from brigade import ingest

    assert candidate["quote"] not in ingest.parse(drafts[0])["durable facts"]
    metadata = list((scope / activity.ROOT / "runs").glob("*/review.json"))[0].read_text()
    assert candidate["quote"] not in metadata
    assert not (scope / "memory/cards").exists()


def test_stuck_pending_quarantines_without_consuming_new_events(scope, monkeypatch):
    event(scope)
    event(scope)
    seat(monkeypatch, proposal(scope))

    def fail(*args, **kwargs):
        raise ValueError("publication differs")

    monkeypatch.setattr(review, "_publish", fail)
    assert review.run(scope, now=111)["status"] == "rejected"
    event(scope, now=120)
    event(scope, now=120)
    assert review.run(scope, now=1000)["status"] == "rejected"
    assert review.run(scope, now=3000)["status"] == "quarantined"
    state = activity.load(scope)
    assert state["reviewed"] == 2
    assert state["sequence"] == 4
    assert "pending" not in state


def test_secondary_filter_allows_transient_retry_guidance(scope):
    quote = "Retry transient 503s with bounded exponential backoff."
    candidate = proposal(scope)
    candidate.update(kind="user-correction", quote=quote, content=candidate["content"] + "\n" + quote, evidence=quote)
    result = admission.admit(scope, candidate, [{"id": 2, "kind": "UserPromptSubmit", "text": quote}], {})
    assert result["kind"] == "user-correction"
    assert "quote" not in result
    assert result["evidence"] is None


def test_disable_works_with_invalid_config(scope, capsys):
    from brigade import cli

    assert cli.main(["memory", "lessons", "disable", "--target", str(scope)]) == 0
    assert activity.config(scope)["seat"] == "fake"
    assert activity.config(scope)["threshold"] == 2
    (scope / ".brigade/lesson-review.json").write_text("invalid json")
    assert cli.main(["memory", "lessons", "disable", "--target", str(scope)]) == 0
    assert activity.config(scope)["enabled"] is False


@pytest.mark.parametrize("component", ["runs", "state.json", "latest.json"])
def test_lesson_writes_refuse_links(scope, monkeypatch, tmp_path, component):
    event(scope)
    event(scope)
    outside = tmp_path / "untouched"
    if component == "runs":
        outside.mkdir()
    else:
        outside.write_text("unchanged")
    linked = scope / activity.ROOT / component
    if linked.exists():
        linked.unlink()
    linked.symlink_to(outside, target_is_directory=component == "runs")
    seat(monkeypatch)
    assert review.run(scope, now=111)["status"] in {"retry", "rejected"}
    if component == "runs":
        assert not list(outside.iterdir())
    else:
        assert outside.read_text() == "unchanged"


def test_scoped_ingest_refuses_digest_and_origin_mismatch(scope):
    from brigade import ingest

    event(scope)
    event(scope)
    candidate = proposal(scope)
    admitted = admission.admit(scope, candidate, activity.load(scope)["events"], {})
    pending = {"lesson": admitted, "run_id": "lesson-example", "digest": "a" * 64, "inbox": ".claude/memory-handoffs"}
    text = review._draft_text(scope, pending)
    path = scope / pending["inbox"] / "example.md"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    with pytest.raises(ingest.ChangedHandoffError):
        ingest.ingest_lesson(source=scope, path=path, expected_digest="0" * 64, admitted_origin=activity.ORIGIN)
    from brigade.handoff_ingest_rules import sha256_hex

    with pytest.raises(ValueError, match="origin"):
        ingest.ingest_lesson(
            source=scope, path=path, expected_digest=sha256_hex(text.encode()), admitted_origin="untrusted"
        )
    assert path.read_text() == text
    assert not (scope / "memory").exists()
