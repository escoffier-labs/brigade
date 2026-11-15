"""Only tooling-authenticated origins may enter automatic filing."""

import os
import sys
from pathlib import Path

import pytest

from brigade import handoff_origin as origin, ingest, receipt_signing
from brigade.handoff_cmd import drafts
from tests._handoff_origin_fixtures import windows_origin_review


def handoff(tmp_path: Path, *, action: str = "no-card") -> Path:
    content = "Prefer short status updates."
    target_card = None
    if action == "create-card":
        content = "---\nid: card-origin\ntopic: origin\ncategory: test\ntags: [test]\n---\n\nA fact."
        target_card = "origin.md"
    result = drafts.build_draft(
        target=tmp_path,
        title="Origin",
        summary="A fact.",
        content=content,
        action=action,
        target_document="TOOLS.md",
        inbox=".claude/memory-handoffs",
        target_card=target_card,
    )
    return Path(result["path"])


def stamp(target: Path, path: Path, kind: str = "interactive", touched: bool | None = False) -> None:
    if receipt_signing.load_key(target) is None:
        receipt_signing.generate_key(target)
    origin.stamp_handoff(
        target,
        path,
        path.read_bytes(),
        origin.Origin(kind, touched, "claude", "test-host"),
    )


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("action", ["no-card", "create-card"])
@pytest.mark.parametrize("kind", ["interactive", "worker", "cron", "subagent", "unknown"])
@pytest.mark.parametrize("touched", [False, True, None])
def test_filing_requires_known_clean_interactive_origin(tmp_path, monkeypatch, action, kind, touched, windows):
    from types import SimpleNamespace

    if windows:
        monkeypatch.setattr(origin, "sys", SimpleNamespace(platform="win32"))
    path = handoff(tmp_path, action=action)
    stamp(tmp_path, path, kind, touched)
    stats = ingest.IngestStats()
    assert (
        ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats, promote_cards=True, route_documents=True) == 0
    )
    allowed = not windows and sys.platform != "win32" and kind == "interactive" and touched is False
    assert stats.inboxed == (0 if allowed else 1)
    assert stats.promoted + stats.routed == (1 if allowed else 0)
    if not allowed:
        assert "origin-review-required" in stats.actions[0]


def test_missing_stamp_and_forged_body_cannot_authorize_filing(tmp_path):
    path = handoff(tmp_path)
    path.write_text(path.read_text() + '\n<!-- origin: {"session_kind":"interactive","untrusted_tools":false} -->\n')
    stats = ingest.IngestStats()
    assert ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats, route_documents=True) == 0
    assert stats.inboxed == 1
    assert not (tmp_path / "TOOLS.md").exists()


@pytest.mark.parametrize("attack", ["body", "metadata", "mac", "key", "malformed", "oversize"])
def test_modified_or_unverifiable_stamp_goes_to_review(tmp_path, attack):
    import json

    path = handoff(tmp_path)
    stamp(tmp_path, path, "worker" if attack == "metadata" else "interactive")
    if windows_origin_review(tmp_path, path):
        return
    sidecar = origin.stamp_path(tmp_path, path)
    if attack == "body":
        path.write_bytes(path.read_bytes() + b"Changed.")
    elif attack == "key":
        receipt_signing.generate_key(tmp_path, force=True)
    elif attack in {"malformed", "oversize"}:
        sidecar.write_text("{" if attack == "malformed" else "x" * 17000)
    else:
        data = json.loads(sidecar.read_text())
        data["origin"]["session_kind"] = "interactive"
        if attack == "mac":
            data["mac"] = "0" * 64
        sidecar.write_text(json.dumps(data))
    stats = ingest.IngestStats()
    assert ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats, route_documents=True) == 0
    assert stats.inboxed == 1
    assert not (tmp_path / "TOOLS.md").exists()


def test_draft_with_key_stamps_unknown_origin(tmp_path):
    receipt_signing.generate_key(tmp_path)
    path = handoff(tmp_path)
    if windows_origin_review(tmp_path, path):
        return
    result = origin.verify_handoff(tmp_path, path, path.read_bytes())
    assert result is not None
    assert result.session_kind == "unknown"
    assert result.untrusted_tools is None
    assert not result.allows_automatic_filing


def test_origin_review_cannot_be_automatically_retried():
    from brigade import memory_inbox_drain

    assert memory_inbox_drain.classify("origin-review-required", "A fact.") == "human"


def test_copied_stamp_cannot_authorize_another_filename(tmp_path):
    path = handoff(tmp_path)
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    renamed = path.with_name("renamed.md")
    renamed.write_bytes(path.read_bytes())
    origin.stamp_path(tmp_path, renamed).write_bytes(origin.stamp_path(tmp_path, path).read_bytes())
    assert origin.verify_handoff(tmp_path, renamed, renamed.read_bytes()) is None


def test_verification_uses_source_key_when_owner_differs(tmp_path):
    source, owner = tmp_path / "source", tmp_path / "owner"
    path = handoff(source)
    stamp(source, path)
    if windows_origin_review(source, path, owner=owner):
        return
    receipt_signing.generate_key(owner)
    stats = ingest.IngestStats()
    assert ingest.ingest_into(source=source, owner=owner, stats=stats, route_documents=True) == 0
    assert stats.routed == 1
    assert (owner / "TOOLS.md").exists()


def test_no_key_does_not_create_a_key_or_claim_trust(tmp_path):
    path = handoff(tmp_path)
    assert not receipt_signing.key_path(tmp_path).exists()
    assert not origin.stamp_path(tmp_path, path).exists()
    assert origin.verify_handoff(tmp_path, path, path.read_bytes()) is None


@pytest.mark.parametrize("key_bytes", [b"not-hex", b"00", b"f" * 300])
def test_unusable_key_is_least_trusted(tmp_path, key_bytes):
    path = handoff(tmp_path)
    key = receipt_signing.key_path(tmp_path)
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(key_bytes)
    assert origin.verify_handoff(tmp_path, path, path.read_bytes()) is None
    assert not origin.stamp_handoff(
        tmp_path, path, path.read_bytes(), origin.Origin("interactive", False, "claude", "test-host")
    )


def test_draft_cannot_self_declare_clean_interactive_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_HANDOFF_SESSION_KIND", "interactive")
    monkeypatch.setenv("BRIGADE_HANDOFF_UNTRUSTED_TOOLS", "false")
    receipt_signing.generate_key(tmp_path)
    path = handoff(tmp_path)
    if windows_origin_review(tmp_path, path):
        return
    verified = origin.verify_handoff(tmp_path, path, path.read_bytes())
    assert verified is not None
    assert not verified.allows_automatic_filing


def test_origin_sidecar_is_private(tmp_path):
    import os

    path = handoff(tmp_path)
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    if os.name == "posix":
        assert origin.stamp_path(tmp_path, path).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("trusted_first", [False, True])
def test_duplicate_content_cannot_bypass_origin_review(tmp_path, trusted_first):
    path = handoff(tmp_path)
    duplicate = path.with_name("000-first.md" if trusted_first else "zzz-last.md")
    duplicate.write_bytes(path.read_bytes())
    stamp(tmp_path, duplicate)
    if windows_origin_review(tmp_path, duplicate):
        return
    stats = ingest.IngestStats()
    assert ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats, route_documents=True) == 0
    assert stats.routed == 1
    assert stats.inboxed == 1
    assert stats.skipped == 0


def test_tooling_rejects_non_boolean_tool_status(tmp_path):
    path = handoff(tmp_path)
    receipt_signing.generate_key(tmp_path)
    with pytest.raises(ValueError, match="invalid handoff origin"):
        origin.stamp_handoff(tmp_path, path, path.read_bytes(), origin.Origin("interactive", 0, "claude", "test-host"))


def test_copied_stamp_cannot_authorize_same_name_at_another_path(tmp_path):
    path = handoff(tmp_path)
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    copied = tmp_path / "another-inbox" / path.name
    copied.parent.mkdir()
    copied.write_bytes(path.read_bytes())
    origin.stamp_path(tmp_path, copied).write_bytes(origin.stamp_path(tmp_path, path).read_bytes())
    assert origin.verify_handoff(tmp_path, copied, copied.read_bytes()) is None


def test_failed_force_stamp_cannot_reuse_previous_interactive_authorization(tmp_path, monkeypatch):
    frozen = drafts.time.gmtime()
    monkeypatch.setattr(drafts.time, "gmtime", lambda *args: frozen)
    path = handoff(tmp_path)
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    original = path.read_bytes()
    assert origin.verify_handoff(tmp_path, path, original).allows_automatic_filing

    def fail(*args, **kwargs):
        raise OSError("stamp storage unavailable")

    monkeypatch.setattr(origin, "stamp_handoff", fail)
    result = drafts.build_draft(
        target=tmp_path,
        title="Origin",
        summary="A fact.",
        content="Prefer short status updates.",
        target_document="TOOLS.md",
        inbox=".claude/memory-handoffs",
        force=True,
    )
    assert Path(result["path"]) == path
    assert path.read_bytes() == original
    assert origin.verify_handoff(tmp_path, path, path.read_bytes()) is None


@pytest.mark.parametrize("trusted", [False, True])
def test_ingest_removes_archived_handoff_stamp_from_source(tmp_path, trusted):
    source, owner = tmp_path / "source", tmp_path / "owner"
    path = handoff(source)
    stamp(source, path, "interactive" if trusted else "worker")
    if windows_origin_review(source, path, owner=owner):
        return
    sidecar = origin.stamp_path(source, path)
    assert sidecar.exists()
    stats = ingest.IngestStats()
    assert ingest.ingest_into(source=source, owner=owner, stats=stats, route_documents=True) == 0
    assert stats.routed == int(trusted)
    assert stats.inboxed == int(not trusted)
    assert (path.parent / "processed" / path.name).exists()
    assert not sidecar.exists()


def test_attacker_key_environment_cannot_authorize_ingest(tmp_path, monkeypatch):
    import json

    path = handoff(tmp_path, action="create-card")
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    sidecar = origin.stamp_path(tmp_path, path)
    attacker = tmp_path / "attacker-key"
    attacker.write_text("11" * 32 + "\n")
    attacker.chmod(0o600)
    payload = json.loads(sidecar.read_bytes())
    payload.pop("mac")
    key = bytes.fromhex("11" * 32)
    payload["key_id"] = receipt_signing.key_id(key)
    sidecar.write_text(json.dumps({**payload, "mac": origin._mac(payload, key)}))
    monkeypatch.setenv(receipt_signing.KEY_ENV, str(attacker))
    stats = ingest.IngestStats()
    ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats, promote_cards=True)
    assert stats.promoted == 0
    assert stats.inboxed == 1
    assert "origin-review-required" in stats.actions[0]
    assert not (tmp_path / "memory/cards/origin.md").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX key confinement")
@pytest.mark.parametrize("attack", ["symlink", "hardlink", "owner", "group-read", "world-read", "directory", "fifo"])
def test_unsafe_key_cannot_authorize_ingest(tmp_path, monkeypatch, attack):
    import os

    path = handoff(tmp_path, action="create-card")
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    key = tmp_path / ".brigade/receipt-signing-key"
    if attack in {"symlink", "hardlink"}:
        other = tmp_path / "key-copy"
        key.rename(other)
        if attack == "symlink":
            key.symlink_to(other)
        else:
            os.link(other, key)
    elif attack == "owner":
        monkeypatch.setattr(os, "getuid", lambda: key.stat().st_uid + 1)
    elif attack in {"group-read", "world-read"}:
        key.chmod(0o640 if attack == "group-read" else 0o604)
    else:
        key.unlink()
        if attack == "directory":
            key.mkdir()
        else:
            os.mkfifo(key)
    stats = ingest.IngestStats()
    ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats, promote_cards=True)
    assert stats.promoted == 0
    assert stats.inboxed == 1
    assert not (tmp_path / "memory/cards/origin.md").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX links")
@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_linked_handoff_is_refused(tmp_path, link):
    import os

    path = handoff(tmp_path, action="create-card")
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    other = path.with_suffix(".saved")
    path.rename(other)
    if link == "symlink":
        path.symlink_to(other)
    else:
        os.link(other, path)
    stats = ingest.IngestStats()
    ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats, promote_cards=True)
    assert stats.failed == 1
    assert stats.promoted == stats.inboxed == 0
    assert path.exists()
    assert "linked-handoff" in stats.actions[0]


@pytest.mark.skipif(os.name != "posix", reason="POSIX archive replay")
def test_archive_path_swap_cleans_exact_stamp_and_refuses_hardlink_replay(tmp_path, monkeypatch):
    import os

    path = handoff(tmp_path, action="create-card")
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    sidecar = origin.stamp_path(tmp_path, path)
    archive = ingest._archive

    def redirect(src, processed, expected_sha=None):
        result = archive(src, processed, expected_sha)
        decoy = tmp_path / "decoy"
        decoy.write_text("decoy")
        src.symlink_to(decoy)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(ingest, "_archive", redirect)
        first = ingest.IngestStats()
        ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=first, promote_cards=True)
    assert first.promoted == 1
    assert not sidecar.exists()
    path.unlink()
    os.link(path.parent / "processed" / path.name, path)
    second = ingest.IngestStats()
    ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=second, promote_cards=True)
    assert second.reinforced == 0
    assert "reinforcements: 1" not in (tmp_path / "memory/cards/origin.md").read_text()


@pytest.mark.parametrize("boundary", ["before-archive", "after-archive"])
def test_crash_after_memory_write_cannot_replay_stamp(tmp_path, monkeypatch, boundary):
    path = handoff(tmp_path, action="create-card")
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    sidecar = origin.stamp_path(tmp_path, path)
    record = sidecar.read_bytes()

    def crash(*args, **kwargs):
        raise OSError("simulated crash after canonical write")

    with monkeypatch.context() as patch:
        if boundary == "before-archive":
            patch.setattr(ingest, "_archive", crash)
        else:
            patch.setattr(origin, "remove_stamp", crash)
        first = ingest.IngestStats()
        ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=first, promote_cards=True)
    card = tmp_path / "memory/cards/origin.md"
    assert first.failed == 1 and card.exists()
    if boundary == "after-archive":
        (path.parent / "processed" / path.name).rename(path)
    assert path.exists() and path.stat().st_nlink == 1
    # Even restoring the original authenticated record must not revive it.
    import json

    sidecar.write_text(json.dumps(json.loads(record), indent=2))
    second = ingest.IngestStats()
    ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=second, promote_cards=True)
    assert second.reinforced == 0
    assert second.inboxed == 1
    assert "origin-review-required" in second.actions[0]
    assert "reinforcements: 1" not in card.read_text()


def test_platform_without_key_confinement_proof_requires_review(tmp_path, monkeypatch):
    import os

    path = handoff(tmp_path)
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    assert origin.verify_handoff(tmp_path, path, path.read_bytes()) is None


def test_verified_stamp_can_be_claimed_only_once(tmp_path):
    path = handoff(tmp_path)
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    first = origin.verify_handoff(tmp_path, path, path.read_bytes())
    second = origin.verify_handoff(tmp_path, path, path.read_bytes())
    assert first is not None and second is not None
    assert first.stamp.claim()
    assert not second.stamp.claim()
    assert origin.verify_handoff(tmp_path, path, path.read_bytes()) is None


def test_real_workspace_key_still_verifies_with_attacker_override(tmp_path, monkeypatch):
    path = handoff(tmp_path, action="create-card")
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    monkeypatch.setenv(receipt_signing.KEY_ENV, str(tmp_path / "missing-attacker-key"))
    stats = ingest.IngestStats()
    ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats, promote_cards=True)
    assert stats.promoted == 1


def test_claim_failure_cannot_write_canonical_memory(tmp_path, monkeypatch):
    path = handoff(tmp_path, action="create-card")
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    blocked = tmp_path / ".brigade/handoffs/consumed-stamps"
    blocked.write_text("not a ledger directory")
    stats = ingest.IngestStats()
    ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats, promote_cards=True)
    assert stats.promoted == 0 and stats.inboxed == 1
    assert not stats.seen_digests
    assert not (tmp_path / "memory/cards/origin.md").exists()


def test_review_claim_resumes_only_the_original_journal_decision(tmp_path):
    path = handoff(tmp_path)
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    verified = origin.verify_handoff(tmp_path, path, path.read_bytes())
    assert verified is not None
    mac = verified.stamp.consumed.name
    assert origin.claim_review_stamp(tmp_path, mac, "first-decision")
    assert origin.claim_review_stamp(tmp_path, mac, "first-decision")
    assert not origin.claim_review_stamp(tmp_path, mac, "second-decision")
    assert origin.verify_handoff(tmp_path, path, path.read_bytes()) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX hardlink replay")
def test_hardlink_replay_reinforces_exactly_once(tmp_path, monkeypatch):
    path = handoff(tmp_path, action="create-card")
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    initial = ingest.IngestStats()
    ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=initial, promote_cards=True)
    assert initial.promoted == 1
    archived = path.parent / "processed" / path.name
    path.write_bytes(archived.read_bytes())
    stamp(tmp_path, path)
    sidecar = origin.stamp_path(tmp_path, path)
    record = sidecar.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(origin, "remove_stamp", lambda *args: None)
        first = ingest.IngestStats()
        ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=first, promote_cards=True)
    assert first.reinforced == 1
    archived = next(p for p in archived.parent.glob("*.md") if p.name != path.name)
    os.link(archived, path)
    sidecar.write_bytes(record)
    second = ingest.IngestStats()
    ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=second, promote_cards=True)
    assert second.reinforced == 0
    assert "reinforcements: 1" in (tmp_path / "memory/cards/origin.md").read_text()


@pytest.mark.parametrize("published_stamp", [False, True])
def test_failed_review_stamp_archives_once_and_keeps_draft_for_a_person(tmp_path, monkeypatch, published_stamp):
    from datetime import date

    from brigade import memory_inbox_drain

    path = handoff(tmp_path)
    stamp(tmp_path, path)
    if windows_origin_review(tmp_path, path):
        return
    monkeypatch.setattr(
        ingest, "decide", lambda *args, **kwargs: ingest.Outcome("inboxed", code="document-destination-unwritable")
    )
    real_stamp = origin.stamp_review_draft

    def fail(*args, **kwargs):
        if published_stamp:
            assert real_stamp(*args, **kwargs)
        raise OSError("review stamp storage unavailable")

    monkeypatch.setattr(origin, "stamp_review_draft", fail)
    stats = ingest.IngestStats()
    assert ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats, route_documents=True) == 0
    assert stats.inboxed == 1
    assert "review-stamp-write-failed" in stats.actions[0]
    assert not path.exists()
    assert (path.parent / "processed" / path.name).exists()
    inbox = tmp_path / "memory/handoff-inbox"
    (draft,) = inbox.glob("*.md")
    assert not origin.stamp_path(tmp_path, draft).exists()
    assert ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=ingest.IngestStats(), route_documents=True) == 0
    assert list(inbox.glob("*.md")) == [draft]
    result = memory_inbox_drain.drain(tmp_path, today=date(2026, 11, 14), deadline_days=14)
    assert result["kept_for_person"] == 1 and result["retried"] == 0
    assert draft.exists() and not (tmp_path / "TOOLS.md").exists()


@pytest.mark.parametrize("suffix", [b"", b"\xff\x00"])
def test_oversized_handoff_keeps_exact_bytes_outside_review_draft(tmp_path, suffix):
    from datetime import date

    from brigade import memory_inbox_drain
    from brigade.instruction_inventory import INSPECTION_CEILING

    path = handoff(tmp_path, action="create-card")
    snapshot = path.read_bytes() + b"x" * INSPECTION_CEILING + suffix
    path.write_bytes(snapshot)
    stamp(tmp_path, path)
    stats = ingest.IngestStats()
    assert ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats, promote_cards=True) == 0
    assert stats.inboxed == 1 and stats.failed == stats.promoted == 0
    assert "handoff-too-large" in stats.actions[0]
    (review,) = (tmp_path / "memory/handoff-inbox").glob("*.md")
    import hashlib

    sample = b"brigade-handoff-sample-v1\0" + str(len(snapshot)).encode("ascii") + b"\0"
    sample += snapshot[:65536] + snapshot[-65536:]
    assert hashlib.sha256(sample).hexdigest() in review.read_text()
    assert review.stat().st_size < 4096
    assert not path.exists()
    (held,) = (tmp_path / ".brigade/handoffs/held").rglob("*.md")
    assert held.read_bytes() == snapshot
    assert not (tmp_path / "memory/cards/origin.md").exists()
    assert memory_inbox_drain.classify("handoff-too-large", "") == "human"
    result = memory_inbox_drain.drain(tmp_path, today=date(2026, 11, 14), deadline_days=14)
    assert result["kept_for_person"] == 1 and result["retried"] == 0 and review.exists()
    again = ingest.IngestStats()
    assert ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=again, promote_cards=True) == 0
    assert again.processed == again.failed == 0


@pytest.mark.parametrize("defect", ["none", "directory", "reparse", "hardlink"])
def test_windows_key_checks_portable_file_safety_before_acl_hold(tmp_path, monkeypatch, defect):
    import stat
    from types import SimpleNamespace

    receipt_signing.generate_key(tmp_path)
    key = tmp_path / ".brigade/receipt-signing-key"
    monkeypatch.setattr(origin, "sys", SimpleNamespace(platform="win32"))
    real_fstat = os.fstat
    observed = []

    def fstat(fd):
        info = real_fstat(fd)
        observed.append(fd)
        if defect == "none":
            return info
        fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        fields.update(
            st_mode=stat.S_IFDIR if defect == "directory" else info.st_mode,
            st_nlink=2 if defect == "hardlink" else 1,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT if defect == "reparse" else 0,
        )
        return SimpleNamespace(**fields)

    monkeypatch.setattr(os, "fstat", fstat)
    if defect != "none":
        with pytest.raises(OSError):
            origin.read_handoff(key, max_bytes=256, private_key=True)
    else:
        assert origin.read_handoff(key, max_bytes=256, private_key=True) == key.read_bytes()
    assert origin._key(tmp_path) is None
    assert observed


@pytest.mark.skipif(os.name != "posix", reason="origin claims need POSIX key confinement until #2588")
@pytest.mark.parametrize("failure", ["short", "error"])
def test_claim_write_failure_never_publishes_partial_marker(tmp_path, monkeypatch, failure):
    consumed = tmp_path / "handoffs/consumed-stamps/claim"
    consumed.parent.parent.mkdir()
    write = os.write

    def fail(fd, data):
        if failure == "error":
            raise OSError("claim storage unavailable")
        return write(fd, data[:1])

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", fail)
        assert not origin._claim(consumed, decision_id="first-decision")
    assert not consumed.exists()
    assert not list((consumed.parent / "temporaries").iterdir())
    assert origin._claim(consumed, decision_id="first-decision")
    assert consumed.read_bytes() == b"first-decision" and consumed.stat().st_nlink == 1
    assert origin._claim(consumed, decision_id="first-decision")
    assert not origin._claim(consumed, decision_id="second-decision")
    assert set(consumed.parent.iterdir()) == {consumed, consumed.parent / "temporaries"}
    assert not list((consumed.parent / "temporaries").iterdir())


@pytest.mark.skipif(os.name != "posix", reason="POSIX origin claim recovery until #2588")
def test_claim_link_crash_resumes_own_marker(tmp_path):
    consumed = tmp_path / "handoffs/consumed-stamps/claim"
    consumed.parent.mkdir(parents=True)
    temporary = consumed.parent / "temporaries" / origin._temporary_name(consumed.name)
    temporary.parent.mkdir()
    temporary.write_bytes(b"first-decision")
    os.link(temporary, consumed)
    assert origin._claim(consumed, decision_id="first-decision")
    assert consumed.read_bytes() == b"first-decision"
    assert consumed.stat().st_nlink == 1
    assert set(consumed.parent.iterdir()) == {consumed, consumed.parent / "temporaries"}
    assert not list((consumed.parent / "temporaries").iterdir())
    assert not origin._claim(consumed, decision_id="other-decision")


@pytest.mark.skipif(os.name != "posix", reason="POSIX origin claim recovery until #2588")
@pytest.mark.parametrize("location", ["outside", "wrong-name"])
def test_claim_foreign_second_link_is_refused(tmp_path, location):
    consumed = tmp_path / "handoffs/consumed-stamps/claim"
    consumed.parent.mkdir(parents=True)
    consumed.write_bytes(b"first-decision")
    extra = tmp_path / ".claim-foreign.tmp" if location == "outside" else consumed.parent / "foreign.tmp"
    os.link(consumed, extra)
    assert not origin._claim(consumed, decision_id="first-decision")
    assert consumed.stat().st_nlink == 2 and extra.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX origin claim recovery until #2588")
def test_orphan_claim_temporary_files_are_swept(tmp_path):
    consumed = tmp_path / "handoffs/consumed-stamps/claim"
    consumed.parent.mkdir(parents=True)
    orphan = consumed.parent / "temporaries" / (".claim-" + "b" * 32 + ".tmp")
    orphan.parent.mkdir()
    orphan.write_bytes(b"abandoned-decision")
    unrelated = consumed.parent / "unrelated.tmp"
    unrelated.write_bytes(b"keep")
    assert origin._claim(consumed, decision_id="first-decision")
    assert not orphan.exists()
    assert unrelated.read_bytes() == b"keep"


def test_oversized_sparse_handoff_never_uses_an_unbounded_snapshot(tmp_path, monkeypatch):
    from datetime import date
    import hashlib

    from brigade import memory_inbox_drain
    from brigade.memory_inbox_io import MAX_DRAFT_BYTES

    path = handoff(tmp_path)
    size = 128 * 1024 * 1024
    os.truncate(path, size)
    identity = path.stat().st_ino
    real_read = origin.read_handoff
    real_bytes = Path.read_bytes
    real_os_read = os.read

    def chunked_read(fd, length):
        if os.fstat(fd).st_ino == identity:
            assert length <= 65536, "oversized hash used an unbounded read"
        return real_os_read(fd, length)

    def bounded_read(source, *, max_bytes, **kwargs):
        assert max_bytes <= ingest.INSPECTION_CEILING, "oversized source was admitted as a snapshot"
        return real_read(source, max_bytes=max_bytes, **kwargs)

    def no_source_read(source):
        assert source != path, "oversized source was read in full during archival"
        return real_bytes(source)

    monkeypatch.setattr(os, "read", chunked_read)
    monkeypatch.setattr(origin, "read_handoff", bounded_read)
    monkeypatch.setattr(Path, "read_bytes", no_source_read)
    stats = ingest.IngestStats()
    assert ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats, route_documents=True) == 0
    assert stats.inboxed == 1 and stats.failed == 0
    (draft,) = (tmp_path / "memory/handoff-inbox").glob("*.md")
    assert draft.stat().st_size < MAX_DRAFT_BYTES
    text = draft.read_text()
    assert "reason: handoff-too-large" in text and path.name in text and str(size) in text
    held = list((tmp_path / ".brigade/handoffs/held").rglob("*.md"))
    assert len(held) == 1 and held[0].stat().st_ino == identity
    assert held[0].stat().st_size == size and not path.exists()
    sample = b"brigade-handoff-sample-v1\0" + str(size).encode("ascii") + b"\0"
    with held[0].open("rb") as handle:
        sample += handle.read(65536)
        handle.seek(-65536, os.SEEK_END)
        sample += handle.read(65536)
    assert hashlib.sha256(sample).hexdigest() in text
    if os.name == "posix":
        assert held[0].parent.stat().st_mode & 0o777 == 0o700
    result = memory_inbox_drain.drain(tmp_path, today=date(2026, 11, 15), deadline_days=14)
    assert result["kept_for_person"] == 1 and result["retried"] == 0
    assert not (tmp_path / "TOOLS.md").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX sparse files and read accounting")
def test_sparse_hold_has_a_total_read_bound(tmp_path, monkeypatch):
    import json
    from brigade import handoff_quarantine

    path = handoff(tmp_path)
    os.truncate(path, 1 << 40)
    before = path.stat()
    identity = before.st_ino
    real_read = os.read
    total = 0

    def bounded_read(fd, length):
        nonlocal total
        if os.fstat(fd).st_ino == identity:
            total += length
            assert total <= 131072, "quarantine exceeded its total sample read budget"
        return real_read(fd, length)

    monkeypatch.setattr(os, "read", bounded_read)
    handoff_quarantine.hold(tmp_path, path)
    (draft,) = (tmp_path / "memory/handoff-inbox").glob("*.md")
    metadata = json.loads(draft.read_text().split("```json\n")[1].split("\n```")[0])
    assert metadata["size"] == 1 << 40
    assert (metadata["dev"], metadata["ino"], metadata["mtime_ns"]) == (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
    )
    assert metadata["digest"]["algorithm"] == "sha256-size-first-last-v1"
    assert metadata["digest"]["max_read_bytes"] == 131072
    assert "sha256" not in metadata


@pytest.mark.skipif(os.name != "posix", reason="POSIX intermediate symlink confinement")
def test_hold_metadata_refuses_linked_memory_parent(tmp_path):
    from brigade import handoff_quarantine

    owner, outside = tmp_path / "owner", tmp_path / "outside"
    owner.mkdir()
    outside.mkdir()
    (owner / "memory").symlink_to(outside, target_is_directory=True)
    path = handoff(tmp_path / "source")
    os.truncate(path, 2 * 1024 * 1024)
    with pytest.raises(OSError):
        handoff_quarantine.hold(owner, path)
    assert path.exists()
    assert not list(outside.iterdir())


@pytest.mark.parametrize("failure", ["publish", "rename", "private", "metadata"])
def test_failed_hold_removes_empty_token_directory(tmp_path, monkeypatch, failure):
    import errno
    from brigade import handoff_quarantine

    path = handoff(tmp_path)
    os.truncate(path, 2 * 1024 * 1024)

    def refuse(*args, **kwargs):
        if failure == "publish":
            os.close(args[0])
        raise OSError(errno.EACCES, "injected hold failure")

    if failure == "publish":
        monkeypatch.setattr(handoff_quarantine.handoff_publish, "_write_and_sync", refuse)
    elif failure == "rename":
        if os.name != "posix":
            pytest.skip("POSIX rename fault injection")
        monkeypatch.setattr(os, "rename", refuse)
        # Keep backend detection independent of the injected syscall failure.
        monkeypatch.setattr(handoff_quarantine.dirfd, "posix_available", lambda: os.name == "posix")
    elif failure == "private":
        if os.name != "posix":
            pytest.skip("POSIX mode privacy check")
        real_hold = handoff_quarantine._private_hold

        def public_hold(target, token):
            result = real_hold(target, token)
            (target / ".brigade/handoffs/held" / token).chmod(0o755)
            return result

        monkeypatch.setattr(handoff_quarantine, "_private_hold", public_hold)
    else:
        monkeypatch.setattr(handoff_quarantine, "MAX_DRAFT_BYTES", 1)
    with pytest.raises(OSError):
        handoff_quarantine.hold(tmp_path, path)
    assert path.exists()
    assert not list((tmp_path / ".brigade/handoffs/held").iterdir())
    assert not list((tmp_path / "memory/handoff-inbox").glob("*"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor-relative rename fault injection")
@pytest.mark.parametrize("cause", ["EXDEV", "EACCES", "EIO"])
def test_ingest_hold_failure_reports_specific_cause(tmp_path, monkeypatch, cause):
    import errno
    from brigade import handoff_quarantine

    path = handoff(tmp_path)
    os.truncate(path, 2 * 1024 * 1024)

    def refuse(*args, **kwargs):
        raise OSError(getattr(errno, cause), "injected hold rename failure")

    monkeypatch.setattr(os, "rename", refuse)
    monkeypatch.setattr(handoff_quarantine.dirfd, "posix_available", lambda: True)
    stats = ingest.IngestStats()
    assert ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats) == 0
    assert stats.failed == 1 and stats.processed == stats.inboxed == 0
    assert "[handoff-hold-failed]" in stats.actions[0]
    assert f"cause={cause}" in stats.actions[0]
    assert path.exists()
    assert not list((tmp_path / ".brigade/handoffs/held").iterdir())
    assert not list((tmp_path / "memory/handoff-inbox").glob("*"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX claim flock and directory enumeration")
@pytest.mark.parametrize("resume", [False, True])
def test_claim_orphan_sweep_has_a_bounded_batch(tmp_path, monkeypatch, resume):
    consumed = tmp_path / ".brigade/handoffs/consumed-stamps" / ("a" * 64)
    consumed.parent.mkdir(parents=True)
    for number in range(200):
        (consumed.parent / f"marker-{number}").touch()
    if resume:
        assert origin._claim(consumed, decision_id="same-decision")
    real_scan = os.scandir
    visited = 0

    class CountedScan:
        def __init__(self, path):
            self.scan = real_scan(path)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.scan.close()

        def __iter__(self):
            nonlocal visited
            for entry in self.scan:
                visited += 1
                assert visited <= 64, "claim lock performed an unbounded orphan sweep"
                yield entry

    monkeypatch.setattr(os, "scandir", CountedScan)
    if resume:
        assert origin.owns_review_claim(tmp_path, consumed.name, "same-decision")
    else:
        assert origin._claim(consumed, decision_id="same-decision")


@pytest.mark.skipif(os.name != "posix", reason="POSIX Git staging and hold race")
def test_tracked_oversized_handoff_does_not_abort_batch(tmp_path, monkeypatch):
    import subprocess
    from brigade import handoff_quarantine

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    oversized = handoff(tmp_path)
    oversized.rename(oversized.with_name("a-oversized.md"))
    oversized = oversized.with_name("a-oversized.md")
    os.truncate(oversized, ingest.INSPECTION_CEILING + 1)
    second = handoff(tmp_path)
    second.rename(second.with_name("z-second.md"))
    second = second.with_name("z-second.md")
    stamp(tmp_path, second)
    real_hold = handoff_quarantine.hold

    def stage_then_hold(target, source, **kwargs):
        subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "--", str(source)], check=True)
        return real_hold(target, source, **kwargs)

    monkeypatch.setattr(handoff_quarantine, "hold", stage_then_hold)
    stats = ingest.IngestStats()
    assert ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats, route_documents=True) == 0
    assert stats.skipped == 1 and stats.routed == 1 and stats.failed == 0
    assert oversized.exists() and not second.exists()
    assert "a-oversized.md" in stats.actions[0]
    assert (tmp_path / "TOOLS.md").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor-relative rename race")
def test_hold_rejects_swap_at_rename(tmp_path, monkeypatch):
    from brigade import handoff_quarantine, dirfd

    source = handoff(tmp_path)
    original = source.stat()
    replacement = source.with_suffix(".replacement")
    replacement.write_bytes(b"replacement")
    real_rename = os.rename

    def swap_then_rename(src, dst, **kwargs):
        replacement.replace(source)
        return real_rename(src, dst, **kwargs)

    monkeypatch.setattr(dirfd, "posix_available", lambda: True)
    monkeypatch.setattr(os, "rename", swap_then_rename)
    with pytest.raises(ingest.ChangedHandoffError, match="during quarantine"):
        handoff_quarantine.hold(tmp_path, source)
    assert not list((tmp_path / "memory/handoff-inbox").glob("*.md"))
    (held,) = (tmp_path / ".brigade/handoffs/held").rglob("*.md")
    assert held.read_bytes() == b"replacement" and held.stat().st_ino != original.st_ino


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow target components")
def test_hold_refuses_symlinked_target(tmp_path):
    from brigade import handoff_quarantine

    owner = tmp_path / "owner"
    owner.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(owner, target_is_directory=True)
    source = handoff(tmp_path / "source")
    with pytest.raises(OSError):
        handoff_quarantine.hold(alias, source)
    assert source.exists() and not (owner / ".brigade").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX bounded claim recovery")
def test_claim_recovery_opens_known_temporary_with_many_markers(tmp_path, monkeypatch):
    consumed = tmp_path / ".brigade/handoffs/consumed-stamps" / ("a" * 64)
    consumed.parent.mkdir(parents=True)
    for number in range(1000):
        (consumed.parent / f"marker-{number}").touch()
    temporary = consumed.parent / "temporaries" / origin._temporary_name(consumed.name)
    temporary.parent.mkdir()
    temporary.write_bytes(b"same-decision")
    os.link(temporary, consumed)

    def no_scan(*args):
        raise AssertionError("recovery scanned a directory")

    monkeypatch.setattr(os, "scandir", no_scan)
    parent = origin.dirfd.open_directory_chain(consumed.parent)
    try:
        assert origin._resume_claim(parent, consumed.name, b"same-decision")
    finally:
        os.close(parent)
    assert not temporary.exists() and consumed.stat().st_nlink == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX bounded orphan maintenance")
def test_claim_orphan_sweep_eventually_reaches_late_orphan(tmp_path):
    consumed = tmp_path / "handoffs/consumed-stamps/claim"
    temporaries = consumed.parent / "temporaries"
    temporaries.mkdir(parents=True)
    for number in range(150):
        (temporaries / origin._temporary_name(str(number))).write_bytes(b"orphan")
    order = list(temporaries.iterdir())
    late = order[100]
    assert origin._claim(consumed, decision_id="same-decision")
    assert late.exists()
    for _ in range(3):
        assert origin._claim(consumed, decision_id="same-decision")
    assert not late.exists() and not list(temporaries.iterdir())
