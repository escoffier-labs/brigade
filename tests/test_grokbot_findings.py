"""Producer-neutral Grok Bot automation-finding delivery."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from itertools import permutations
from pathlib import Path

import pytest

from brigade import cli, grokbot_findings


NOW = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
PRIVATE_TITLE = "PRIVATE_FINDING_TITLE_TOKEN"
PRIVATE_BODY = "PRIVATE_FINDING_BODY_TOKEN_alpha\n## Injected Heading\nUntrusted automation body.\n"
LIVE_REASON_MAX_BYTES = 16_384
LIVE_SUMMARY_MAX_BYTES = 4_096
PROPOSAL_TITLE_MAX_CHARS = 120


def _proposal_title(producer: str, finding_id: str) -> str:
    title = f"[UNTRUSTED] {producer} {finding_id}"
    return title if len(title) <= PROPOSAL_TITLE_MAX_CHARS else title[:PROPOSAL_TITLE_MAX_CHARS]


def _digest(text: str = PRIVATE_BODY) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _content_digest(title: str = PRIVATE_TITLE, body: str = PRIVATE_BODY) -> str:
    return "sha256:" + hashlib.sha256(f"{title}\0{body}".encode("utf-8")).hexdigest()


def _revision_digest(seed: str = "fleet-revision") -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _live_fleet_record() -> dict[str, object]:
    revision = _revision_digest("fleet-control-plane")
    return {
        "producer": "fleet",
        "finding_id": "control-plane:unreachable",
        "revision": revision,
        "observed_at": "",
        "severity": "unknown",
        "title": PRIVATE_TITLE,
        "body": PRIVATE_BODY,
        "source_ref": "fleet:control-plane:unreachable",
        "source_digest": revision,
        "trust": "untrusted",
        "delivery": "review-only",
    }


def _live_backup_record() -> dict[str, object]:
    revision = _revision_digest("backup-stale-lock")
    return {
        "producer": "backup",
        "finding_id": "configuration-nas:stale-lock",
        "revision": revision,
        "observed_at": "2026-08-27T15:00:00Z",
        "severity": "warning",
        "title": PRIVATE_TITLE,
        "body": PRIVATE_BODY,
        "source_ref": "backup:configuration-nas:stale-lock",
        "source_digest": revision,
        "trust": "untrusted",
        "delivery": "review-only",
    }


def _approved_live_entry(
    *,
    producer: str = "fleet",
    finding_id: str = "control-plane:unreachable",
    revision: str | None = None,
    observed_at: str = "",
    severity: str = "unknown",
    source_ref: str = "fleet:control-plane:unreachable",
    source_digest: str | None = None,
) -> dict[str, object]:
    revision_hex = revision or _revision_digest("fleet-control-plane")
    return {
        "producer": producer,
        "finding_id": finding_id,
        "revision": revision_hex,
        "observed_at": observed_at,
        "severity": severity,
        "title": PRIVATE_TITLE,
        "body": PRIVATE_BODY,
        "source_ref": source_ref,
        "source_digest": source_digest or f"sha256:{revision_hex}",
        "content_digest": _content_digest(),
    }


def _entry(
    *,
    producer: str = "cerebro-research",
    finding_id: str = "finding-alpha",
    revision: str = "1",
    title: str = PRIVATE_TITLE,
    body: str = PRIVATE_BODY,
    source_ref: str = "cerebro://vault/findings/alpha",
) -> dict[str, object]:
    return {
        "producer": producer,
        "finding_id": finding_id,
        "revision": revision,
        "observed_at": "2026-08-27T15:00:00Z",
        "severity": "high",
        "title": title,
        "body": body,
        "source_ref": source_ref,
        "source_digest": _digest(body),
        "content_digest": _content_digest(title, body),
    }


def _manifest(*entries: dict[str, object]) -> dict[str, object]:
    return {"schema": "brigade.grokbot.findings.v1", "entries": list(entries)}


def _write_manifest(path: Path, payload: dict[str, object], *, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)
    return path


def _queue_root(queue: Path) -> Path:
    return queue / ".brigade" / "cloud" / "grokbot"


def _review_inbox(owner: Path) -> Path:
    return owner / "memory" / "handoff-inbox"


def _draft_name(entry: dict[str, object]) -> str:
    return grokbot_findings.draft_filename(
        str(entry["producer"]),
        str(entry["finding_id"]),
        str(entry["revision"]),
    )


def _marker_name(entry: dict[str, object]) -> str:
    return grokbot_findings.marker_filename(
        str(entry["producer"]),
        str(entry["finding_id"]),
        str(entry["revision"]),
    )


def _handle(entry: dict[str, object]) -> dict[str, str]:
    return {
        "producer": str(entry["producer"]),
        "finding_id": str(entry["finding_id"]),
        "revision": str(entry["revision"]),
    }


def test_preview_counts_valid_entries_and_writes_nothing(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    first = _entry()
    second = _entry(finding_id="finding-beta", source_ref="cerebro://vault/findings/beta")
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(first, second))

    result = grokbot_findings.preview(queue, owner, manifest)

    assert result == {
        "eligible": 2,
        "known": 0,
        "created": 0,
        "limit": 1,
        "findings": [_handle(first)],
    }
    dumped = json.dumps(result, sort_keys=True)
    assert PRIVATE_TITLE not in dumped
    assert PRIVATE_BODY not in dumped
    assert "PRIVATE_FINDING_BODY_TOKEN_alpha" not in dumped
    assert "Injected Heading" not in dumped
    assert not _review_inbox(owner).exists()
    assert not (_queue_root(queue) / "findings").exists()


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            {
                "schema": "brigade.grokbot.findings.v0",
                "entries": [_entry()],
            },
            "invalid-schema",
        ),
        (
            {
                "schema": "brigade.grokbot.findings.v1",
                "approved": True,
                "entries": [_entry()],
            },
            "malformed-manifest",
        ),
        (
            {
                "schema": "brigade.grokbot.findings.v1",
                "api_token": "secret-value",
                "entries": [_entry()],
            },
            "malformed-manifest",
        ),
        (
            {
                "schema": "brigade.grokbot.findings.v1",
                "entries": [{**_entry(), "api_token": "secret-value"}],
            },
            "invalid-entry",
        ),
        (
            {
                "schema": "brigade.grokbot.findings.v1",
                "entries": [_entry(), _entry(revision="2")],
            },
            "duplicate-identity",
        ),
        (
            {
                "schema": "brigade.grokbot.findings.v1",
                "entries": [{**_entry(), "content_digest": "sha256:" + ("ab" * 32)}],
            },
            "digest-mismatch",
        ),
    ],
)
def test_preview_rejects_malformed_manifests_without_writing(tmp_path: Path, payload: dict[str, object], reason: str):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    manifest = _write_manifest(tmp_path / "findings.json", payload)

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        grokbot_findings.preview(queue, owner, manifest)

    assert exc.value.reason == reason
    assert not _review_inbox(owner).exists()
    assert not (_queue_root(queue) / "findings").exists()


def test_preview_rejects_group_readable_manifest_without_writing(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(_entry()), mode=0o640)

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        grokbot_findings.preview(queue, owner, manifest)

    assert exc.value.reason == "unsafe-manifest"
    assert not _review_inbox(owner).exists()


def test_preview_rejects_symlink_manifest_without_writing(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    real = _write_manifest(tmp_path / "real.json", _manifest(_entry()))
    link = tmp_path / "findings.json"
    link.symlink_to(real)

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        grokbot_findings.preview(queue, owner, link)

    assert exc.value.reason == "unsafe-manifest"
    assert not _review_inbox(owner).exists()


def test_preview_rejects_oversized_manifest_without_writing(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    manifest = tmp_path / "findings.json"
    manifest.write_bytes(b"{" + (b"a" * grokbot_findings.MAX_MANIFEST_BYTES) + b"}")
    manifest.chmod(0o600)

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        grokbot_findings.preview(queue, owner, manifest)

    assert exc.value.reason == "oversized-manifest"
    assert not _review_inbox(owner).exists()


@pytest.mark.parametrize("limit", [0, 51, -1, True, 1.5, "1"])
def test_preview_rejects_invalid_limits_without_writing(tmp_path: Path, limit: object):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(_entry()))

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        grokbot_findings.preview(queue, owner, manifest, limit=limit)  # type: ignore[arg-type]

    assert exc.value.reason == "invalid-limit"
    assert not _review_inbox(owner).exists()


def test_apply_writes_one_quoted_handoff_and_a_private_marker(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entry = _entry()
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(entry, _entry(finding_id="finding-beta")))

    result = grokbot_findings.apply(queue, owner, manifest, now=NOW)

    assert result == {
        "eligible": 2,
        "known": 0,
        "created": 1,
        "skipped": 0,
        "limit": 1,
        "findings": [_handle(entry)],
    }
    dumped = json.dumps(result, sort_keys=True)
    assert PRIVATE_TITLE not in dumped
    assert "PRIVATE_FINDING_BODY_TOKEN_alpha" not in dumped

    inbox = _review_inbox(owner)
    drafts = list(inbox.glob("*.md"))
    assert [path.name for path in drafts] == [_draft_name(entry)]
    text = drafts[0].read_text(encoding="utf-8")
    assert "## Recommended memory action\n\nno-card" in text
    assert "untrusted" in text.casefold()
    assert "review-only" in text.casefold() or "review only" in text.casefold()
    assert "canonical" in text.casefold()
    assert entry["producer"] in text
    assert entry["finding_id"] in text
    assert entry["revision"] in text
    assert entry["observed_at"] in text
    assert entry["severity"] in text
    assert entry["source_ref"] in text
    assert entry["source_digest"] in text
    assert entry["content_digest"] in text
    assert f"> {PRIVATE_TITLE}" in text
    assert "> PRIVATE_FINDING_BODY_TOKEN_alpha" in text
    assert "> ## Injected Heading" in text
    assert f"\n{PRIVATE_TITLE}\n" not in text
    assert "\n## Injected Heading\n" not in text
    assert "do not auto-edit canonical memory" in text.casefold() or "rather than auto-edit" in text.casefold()
    assert inbox.stat().st_mode & 0o777 == 0o700
    assert drafts[0].stat().st_mode & 0o777 == 0o600

    marker = _queue_root(queue) / "findings" / _marker_name(entry)
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert set(payload) == {
        "schema",
        "identity",
        "revision",
        "source_digest",
        "content_digest",
        "draft_path",
        "draft_sha256",
        "delivered_at",
    }
    assert payload["schema"] == "brigade.grokbot.findings.v1"
    assert payload["identity"] == grokbot_findings.identity_digest(
        str(entry["producer"]), str(entry["finding_id"]), str(entry["revision"])
    )
    assert payload["revision"] == entry["revision"]
    assert payload["source_digest"] == entry["source_digest"]
    assert payload["content_digest"] == entry["content_digest"]
    assert payload["draft_path"] == f"memory/handoff-inbox/{_draft_name(entry)}"
    assert payload["draft_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert payload["delivered_at"] == "2026-08-27T15:00:00Z"
    assert PRIVATE_TITLE not in json.dumps(payload)
    assert PRIVATE_BODY not in json.dumps(payload)
    assert marker.stat().st_mode & 0o777 == 0o600
    assert marker.parent.stat().st_mode & 0o777 == 0o700
    assert not (_review_inbox(owner) / _draft_name(_entry(finding_id="finding-beta"))).exists()


def test_same_revision_is_idempotent(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entry = _entry()
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(entry))
    first = grokbot_findings.apply(queue, owner, manifest, now=NOW)
    draft = _review_inbox(owner) / _draft_name(entry)
    before = draft.read_text(encoding="utf-8")

    second = grokbot_findings.apply(queue, owner, manifest, now=NOW)
    preview = grokbot_findings.preview(queue, owner, manifest)

    assert first["created"] == 1
    assert second == {
        "eligible": 0,
        "known": 1,
        "created": 0,
        "skipped": 1,
        "limit": 1,
        "findings": [],
    }
    assert preview["eligible"] == 0
    assert preview["known"] == 1
    assert preview["created"] == 0
    assert list(_review_inbox(owner).glob("*.md")) == [draft]
    assert draft.read_text(encoding="utf-8") == before


def test_revision_change_writes_a_non_colliding_replacement_draft(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    first = _entry(revision="1")
    second = _entry(revision="2", body=PRIVATE_BODY + "revision two\n")
    grokbot_findings.apply(queue, owner, _write_manifest(tmp_path / "first.json", _manifest(first)), now=NOW)

    result = grokbot_findings.apply(queue, owner, _write_manifest(tmp_path / "second.json", _manifest(second)), now=NOW)

    assert result["created"] == 1
    assert result["findings"] == [_handle(second)]
    names = sorted(path.name for path in _review_inbox(owner).glob("*.md"))
    assert names == sorted((_draft_name(first), _draft_name(second)))
    assert _draft_name(first) != _draft_name(second)
    assert (_queue_root(queue) / "findings" / _marker_name(first)).is_file()
    assert (_queue_root(queue) / "findings" / _marker_name(second)).is_file()


def test_malformed_later_entry_fails_closed_before_any_write(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    later = _entry(finding_id="finding-beta")
    later.pop("body")
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(_entry(), later))

    with pytest.raises(grokbot_findings.FindingsError, match="^invalid-entry$"):
        grokbot_findings.apply(queue, owner, manifest, now=NOW)

    assert not _review_inbox(owner).exists()
    assert (
        not (_queue_root(queue) / "findings").exists() or list((_queue_root(queue) / "findings").glob("*.json")) == []
    )


def test_conflicting_marker_fails_closed_before_any_write(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    first = _entry()
    second = _entry(finding_id="finding-beta", source_ref="cerebro://vault/findings/beta")
    findings_dir = _queue_root(queue) / "findings"
    findings_dir.mkdir(parents=True)
    (findings_dir / _marker_name(second)).write_text(
        json.dumps(
            {
                "schema": "brigade.grokbot.findings.v1",
                "identity": grokbot_findings.identity_digest(
                    str(second["producer"]), str(second["finding_id"]), str(second["revision"])
                ),
                "revision": second["revision"],
                "source_digest": "sha256:" + ("ab" * 32),
                "content_digest": "sha256:" + ("ef" * 32),
                "draft_path": "memory/handoff-inbox/other.md",
                "draft_sha256": "cd" * 32,
                "delivered_at": "2026-08-27T15:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(grokbot_findings.FindingsError, match="^(marker-conflict|digest-mismatch)$"):
        grokbot_findings.apply(
            queue, owner, _write_manifest(tmp_path / "findings.json", _manifest(first, second)), now=NOW
        )

    assert not _review_inbox(owner).exists()
    assert not (_queue_root(queue) / "findings" / _marker_name(first)).exists()


def test_existing_draft_with_different_bytes_fails_closed(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entry = _entry()
    inbox = _review_inbox(owner)
    inbox.mkdir(parents=True)
    draft = inbox / _draft_name(entry)
    draft.write_text("different draft bytes\n", encoding="utf-8")

    with pytest.raises(grokbot_findings.FindingsError, match="^draft-conflict$"):
        grokbot_findings.apply(queue, owner, _write_manifest(tmp_path / "findings.json", _manifest(entry)), now=NOW)

    assert draft.read_text(encoding="utf-8") == "different draft bytes\n"
    assert not (_queue_root(queue) / "findings" / _marker_name(entry)).exists()


def test_existing_handoff_symlink_fails_closed(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entry = _entry()
    inbox = _review_inbox(owner)
    inbox.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("do not replace\n", encoding="utf-8")
    (_review_inbox(owner) / _draft_name(entry)).symlink_to(outside)

    with pytest.raises(grokbot_findings.FindingsError, match="^unsafe-storage$"):
        grokbot_findings.apply(queue, owner, _write_manifest(tmp_path / "findings.json", _manifest(entry)), now=NOW)

    assert outside.read_text(encoding="utf-8") == "do not replace\n"
    assert not (_queue_root(queue) / "findings" / _marker_name(entry)).exists()


def test_restart_after_draft_without_marker_does_not_duplicate(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entry = _entry()
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(entry))
    grokbot_findings.apply(queue, owner, manifest, now=NOW)
    marker = _queue_root(queue) / "findings" / _marker_name(entry)
    marker.unlink()

    result = grokbot_findings.apply(queue, owner, manifest, now=NOW)

    assert result["created"] == 0
    assert result["skipped"] == 1
    drafts = list(_review_inbox(owner).glob("*.md"))
    assert [path.name for path in drafts] == [_draft_name(entry)]
    assert marker.is_file()


def _run_findings(queue: Path, owner: Path, manifest: Path, *command: str) -> int:
    return cli.main(
        [
            "run",
            "cloud",
            "grokbot",
            "reconcile-findings",
            "--target",
            str(queue),
            "--owner",
            str(owner),
            "--manifest",
            str(manifest),
            *command,
        ]
    )


def test_cli_preview_defaults_to_write_nothing_and_omits_finding_text(tmp_path: Path, capsys):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entry = _entry()
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(entry))

    assert _run_findings(queue, owner, manifest) == 0
    captured = capsys.readouterr()
    assert "eligible=1" in captured.out
    assert "known=0" in captured.out
    assert entry["producer"] in captured.out
    assert entry["finding_id"] in captured.out
    assert PRIVATE_TITLE not in captured.out
    assert PRIVATE_BODY not in captured.out
    assert "Injected Heading" not in captured.out
    assert captured.err == ""
    assert not _review_inbox(owner).exists()


def test_cli_apply_json_stays_free_of_finding_text(tmp_path: Path, capsys):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entry = _entry()
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(entry))

    assert _run_findings(queue, owner, manifest, "--apply", "--json") == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["created"] == 1
    assert payload["findings"][0]["finding_id"] == entry["finding_id"]
    assert PRIVATE_TITLE not in captured.out
    assert PRIVATE_BODY not in captured.out
    assert (_review_inbox(owner) / _draft_name(entry)).is_file()


def test_cli_rejects_invalid_limit_without_writing(tmp_path: Path, capsys):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(_entry()))

    assert _run_findings(queue, owner, manifest, "--apply", "--limit", "0") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "error: invalid-limit"
    assert not _review_inbox(owner).exists()


def test_reconcile_findings_appears_in_parser_inventory():
    from brigade.roadmap_cmd import _cli_command_paths

    assert "brigade run-cloud grokbot reconcile-findings" in _cli_command_paths()


def _apply_in_process(queue: str, owner: str, manifest: str, connection) -> None:
    try:
        result = grokbot_findings.apply(Path(queue), Path(owner), Path(manifest), now=NOW)
    except grokbot_findings.FindingsError as exc:
        connection.send({"error": exc.reason})
    else:
        connection.send(result)
    finally:
        connection.close()


def test_concurrent_apply_creates_only_one_draft(tmp_path: Path):
    from multiprocessing import Pipe, Process

    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entry = _entry()
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(entry))
    first_parent, first_child = Pipe(duplex=False)
    second_parent, second_child = Pipe(duplex=False)
    first = Process(target=_apply_in_process, args=(str(queue), str(owner), str(manifest), first_child))
    second = Process(target=_apply_in_process, args=(str(queue), str(owner), str(manifest), second_child))
    first.start()
    second.start()
    first.join()
    second.join()
    results = [first_parent.recv(), second_parent.recv()]
    created = sum(item.get("created", 0) for item in results if "created" in item)
    assert created == 1
    drafts = list(_review_inbox(owner).glob("*.md"))
    assert [path.name for path in drafts] == [_draft_name(entry)]


def test_generic_manifest_accepts_live_compatible_values_without_leaking_text(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entry = _approved_live_entry()
    assert entry["source_digest"] != _digest(PRIVATE_BODY)
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(entry))

    result = grokbot_findings.preview(queue, owner, manifest)

    assert result["eligible"] == 1
    assert result["findings"] == [_handle(entry)]
    dumped = json.dumps(result, sort_keys=True)
    assert PRIVATE_TITLE not in dumped
    assert PRIVATE_BODY not in dumped
    assert not _review_inbox(owner).exists()
    assert not (_queue_root(queue) / "findings").exists()


@pytest.mark.parametrize("severity", ["info", "low", "medium", "warning", "high", "critical", "unknown"])
def test_generic_manifest_accepts_live_and_backup_severities(tmp_path: Path, severity: str):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entry = _approved_live_entry(severity=severity)
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(entry))

    result = grokbot_findings.preview(queue, owner, manifest)

    assert result["eligible"] == 1


def test_generic_manifest_rejects_content_digest_mismatch_without_writing(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entry = {**_approved_live_entry(), "content_digest": "sha256:" + ("cd" * 32)}
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(entry))

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        grokbot_findings.preview(queue, owner, manifest)

    assert exc.value.reason == "digest-mismatch"
    assert not _review_inbox(owner).exists()
    assert not (_queue_root(queue) / "findings").exists()


def test_adapt_live_fleet_preserves_unknown_severity_empty_time_and_revision_digest():
    live = _live_fleet_record()
    adapted = grokbot_findings.adapt_live_finding(live)

    assert adapted["producer"] == "fleet"
    assert adapted["finding_id"] == live["finding_id"]
    assert adapted["revision"] == live["revision"]
    assert adapted["observed_at"] == ""
    assert adapted["severity"] == "unknown"
    assert adapted["source_digest"] == f"sha256:{live['revision']}"
    assert adapted["source_digest"] != _digest(PRIVATE_BODY)
    assert adapted["title"] == _proposal_title("fleet", str(live["finding_id"]))
    assert adapted["body"] == PRIVATE_BODY
    assert adapted["content_digest"] == _content_digest(adapted["title"], PRIVATE_BODY)
    assert "trust" not in adapted
    assert "delivery" not in adapted
    assert set(adapted) == grokbot_findings.REQUIRED_ENTRY_KEYS


def test_adapt_live_backup_preserves_warning_severity_and_revision_digest():
    live = _live_backup_record()
    adapted = grokbot_findings.adapt_live_finding(live)

    assert adapted["producer"] == "backup"
    assert adapted["severity"] == "warning"
    assert adapted["observed_at"] == live["observed_at"]
    assert adapted["source_digest"] == f"sha256:{live['revision']}"
    assert adapted["title"] == _proposal_title("backup", str(live["finding_id"]))
    assert adapted["body"] == PRIVATE_BODY
    assert adapted["content_digest"] == _content_digest(adapted["title"], PRIVATE_BODY)
    assert set(adapted) == grokbot_findings.REQUIRED_ENTRY_KEYS


def test_adapt_live_finding_rejects_unexpected_keys():
    live = {**_live_fleet_record(), "api_token": "secret-value"}

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        grokbot_findings.adapt_live_finding(live)

    assert exc.value.reason == "invalid-entry"


def test_adapted_live_finding_loads_through_the_generic_manifest(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    adapted = grokbot_findings.adapt_live_finding(_live_fleet_record())
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(adapted))

    result = grokbot_findings.apply(queue, owner, manifest, now=NOW)

    assert result["created"] == 1
    marker = _queue_root(queue) / "findings" / _marker_name(adapted)
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["source_digest"] == adapted["source_digest"]
    assert payload["content_digest"] == adapted["content_digest"]
    assert PRIVATE_TITLE not in json.dumps(payload)
    assert PRIVATE_BODY not in json.dumps(payload)


def test_recovery_writes_count_against_the_same_limit(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entries = [
        _entry(finding_id=f"finding-{index:02d}", source_ref=f"cerebro://vault/findings/{index:02d}")
        for index in range(50)
    ]
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(*entries))
    seeded = grokbot_findings.apply(queue, owner, manifest, limit=50, now=NOW)
    assert seeded["created"] == 50
    findings_dir = _queue_root(queue) / "findings"
    for entry in entries:
        (findings_dir / _marker_name(entry)).unlink()
    drafts_before = sorted(path.name for path in _review_inbox(owner).glob("*.md"))

    result = grokbot_findings.apply(queue, owner, manifest, limit=1, now=NOW)

    assert result["created"] == 0
    assert result["skipped"] == 1
    markers = sorted(path.name for path in findings_dir.glob("*.json"))
    assert markers == [_marker_name(entries[0])]
    assert sorted(path.name for path in _review_inbox(owner).glob("*.md")) == drafts_before


def test_selection_is_independent_of_manifest_order(tmp_path: Path):
    entries = [
        _entry(producer="zeta-producer", finding_id="finding-z", revision="2", source_ref="cerebro://vault/z"),
        _entry(producer="alpha-producer", finding_id="finding-b", revision="1", source_ref="cerebro://vault/b"),
        _entry(producer="alpha-producer", finding_id="finding-a", revision="9", source_ref="cerebro://vault/a"),
    ]
    expected = _handle(entries[2])
    handles: list[dict[str, str]] = []
    for index, ordered in enumerate(permutations(entries)):
        queue = tmp_path / f"queue-{index}"
        owner = tmp_path / f"owner-{index}"
        queue.mkdir()
        owner.mkdir()
        manifest = _write_manifest(tmp_path / f"findings-{index}.json", _manifest(*ordered))
        preview = grokbot_findings.preview(queue, owner, manifest)
        applied = grokbot_findings.apply(queue, owner, manifest, now=NOW)
        handles.append(preview["findings"][0])
        assert applied["findings"] == [expected]
        assert preview["findings"] == [expected]
        assert PRIVATE_TITLE not in json.dumps(preview)
        assert PRIVATE_BODY not in json.dumps(applied)
    assert handles == [expected] * 6


@pytest.mark.parametrize("delivered_at", ["", "not-a-timestamp", "2026-08-27T15:00:00"])
def test_corrupt_marker_delivered_at_fails_closed(tmp_path: Path, delivered_at: str):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entry = _entry()
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(entry))
    grokbot_findings.apply(queue, owner, manifest, now=NOW)
    marker = _queue_root(queue) / "findings" / _marker_name(entry)
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["delivered_at"] = delivered_at
    marker.write_text(json.dumps(payload), encoding="utf-8")
    marker.chmod(0o600)
    draft = _review_inbox(owner) / _draft_name(entry)
    before = draft.read_text(encoding="utf-8")

    with pytest.raises(grokbot_findings.FindingsError) as preview_exc:
        grokbot_findings.preview(queue, owner, manifest)
    with pytest.raises(grokbot_findings.FindingsError) as apply_exc:
        grokbot_findings.apply(queue, owner, manifest, now=NOW)

    assert preview_exc.value.reason == "corrupt-storage"
    assert apply_exc.value.reason == "corrupt-storage"
    assert json.loads(marker.read_text(encoding="utf-8"))["delivered_at"] == delivered_at
    assert draft.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("component", [".brigade", "cloud", "grokbot"])
def test_preview_refuses_symlinked_storage_parents_and_writes_nothing(tmp_path: Path, component: str):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entry = _entry()
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(entry))
    grokbot_findings.apply(queue, owner, manifest, now=NOW)
    if component == ".brigade":
        real = tmp_path / "outside-brigade"
        (queue / ".brigade").rename(real)
        (queue / ".brigade").symlink_to(real)
        outside = real
    elif component == "cloud":
        real = tmp_path / "outside-cloud"
        (queue / ".brigade" / "cloud").rename(real)
        (queue / ".brigade" / "cloud").symlink_to(real)
        outside = real
    else:
        real = tmp_path / "outside-grokbot"
        (queue / ".brigade" / "cloud" / "grokbot").rename(real)
        (queue / ".brigade" / "cloud" / "grokbot").symlink_to(real)
        outside = real
    owner_before = _walk_names(owner)
    queue_before = _walk_names(queue)
    outside_before = _walk_names(outside)

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        grokbot_findings.preview(queue, owner, manifest)

    assert exc.value.reason == "unsafe-storage"
    assert _walk_names(owner) == owner_before
    assert _walk_names(queue) == queue_before
    assert _walk_names(outside) == outside_before


def _walk_names(root: Path) -> list[str]:
    names: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        for name in dirnames + filenames:
            names.append(str((current / name).relative_to(root)))
    return sorted(names)


def test_adapt_live_fleet_accepts_max_reason_and_derives_proposal_title():
    live = _live_fleet_record()
    live["title"] = "R" * LIVE_REASON_MAX_BYTES

    adapted = grokbot_findings.adapt_live_finding(live)

    assert adapted["title"] == _proposal_title("fleet", str(live["finding_id"]))
    assert len(adapted["title"]) <= PROPOSAL_TITLE_MAX_CHARS
    assert adapted["body"] == PRIVATE_BODY
    assert adapted["content_digest"] == _content_digest(adapted["title"], PRIVATE_BODY)
    assert "R" * 200 not in adapted["title"]


def test_adapt_live_backup_accepts_max_summary_and_derives_proposal_title():
    live = _live_backup_record()
    live["title"] = "S" * LIVE_SUMMARY_MAX_BYTES

    adapted = grokbot_findings.adapt_live_finding(live)

    assert adapted["title"] == _proposal_title("backup", str(live["finding_id"]))
    assert adapted["body"] == PRIVATE_BODY
    assert adapted["content_digest"] == _content_digest(adapted["title"], PRIVATE_BODY)


def test_adapt_live_proposal_title_is_sliced_to_the_live_relay_limit():
    live = _live_fleet_record()
    live["finding_id"] = "a" * 128

    adapted = grokbot_findings.adapt_live_finding(live)

    assert adapted["title"] == _proposal_title("fleet", "a" * 128)
    assert adapted["title"] == ("[UNTRUSTED] fleet " + ("a" * 128))[:PROPOSAL_TITLE_MAX_CHARS]
    assert len(adapted["title"]) == PROPOSAL_TITLE_MAX_CHARS
    assert adapted["body"] == PRIVATE_BODY


def test_adapt_live_rejects_title_over_the_live_maximum():
    live = _live_fleet_record()
    live["title"] = "R" * (LIVE_REASON_MAX_BYTES + 1)

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        grokbot_findings.adapt_live_finding(live)

    assert exc.value.reason == "invalid-title"


def test_adapt_live_accepts_source_ref_with_consecutive_dots():
    live = _live_fleet_record()
    live["finding_id"] = "host..alias:unreachable"
    live["source_ref"] = "fleet:host..alias:unreachable"

    adapted = grokbot_findings.adapt_live_finding(live)

    assert adapted["finding_id"] == "host..alias:unreachable"
    assert adapted["source_ref"] == "fleet:host..alias:unreachable"


def test_generic_manifest_accepts_source_ref_with_consecutive_dots(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    entry = _approved_live_entry(
        finding_id="host..alias:unreachable",
        source_ref="fleet:host..alias:unreachable",
    )
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(entry))

    result = grokbot_findings.preview(queue, owner, manifest)

    assert result["eligible"] == 1
    assert result["findings"] == [_handle(entry)]


@pytest.mark.parametrize(
    "source_ref",
    ["fleet:\x00id", "fleet:\nid", "fleet:\rid", "x" * 513],
)
def test_source_ref_still_rejects_control_characters_and_overlength(source_ref: str):
    live = {**_live_fleet_record(), "source_ref": source_ref}

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        grokbot_findings.adapt_live_finding(live)

    assert exc.value.reason == "invalid-source-ref"


def _live_manifest(*entries: dict[str, object]) -> dict[str, object]:
    return {"schema": "brigade.grokbot.live-findings.v1", "entries": list(entries)}


def _run_convert(target: Path, source: Path, destination: Path, *command: str) -> int:
    return cli.main(
        [
            "run",
            "cloud",
            "grokbot",
            "convert-findings",
            "--target",
            str(target),
            "--input",
            str(source),
            "--output",
            str(destination),
            *command,
        ]
    )


def test_convert_live_findings_writes_0600_manifest_without_printing_text(tmp_path: Path, capsys):
    source = _write_manifest(
        tmp_path / "live.json",
        _live_manifest(_live_backup_record(), _live_fleet_record()),
    )
    destination = tmp_path / "findings.json"

    assert _run_convert(tmp_path, source, destination) == 0
    captured = capsys.readouterr()
    payload = json.loads(destination.read_text(encoding="utf-8"))
    fleet = grokbot_findings.adapt_live_finding(_live_fleet_record())
    backup = grokbot_findings.adapt_live_finding(_live_backup_record())

    assert destination.stat().st_mode & 0o777 == 0o600
    assert payload == {"schema": "brigade.grokbot.findings.v1", "entries": [backup, fleet]}
    assert "converted=2" in captured.out
    assert fleet["producer"] in captured.out
    assert fleet["finding_id"] in captured.out
    assert PRIVATE_TITLE not in captured.out
    assert PRIVATE_BODY not in captured.out
    assert PRIVATE_TITLE not in captured.err
    assert PRIVATE_BODY not in captured.err
    assert "Injected Heading" not in captured.out
    assert captured.err == ""


def test_convert_live_findings_json_stays_free_of_finding_text(tmp_path: Path, capsys):
    source = _write_manifest(tmp_path / "live.json", _live_manifest(_live_fleet_record()))
    destination = tmp_path / "findings.json"

    assert _run_convert(tmp_path, source, destination, "--json") == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    live = _live_fleet_record()

    assert payload["converted"] == 1
    assert payload["findings"] == [_handle(live)]
    assert PRIVATE_TITLE not in captured.out
    assert PRIVATE_BODY not in captured.out
    assert set(payload) == {"converted", "findings"}
    assert set(payload["findings"][0]) == {"producer", "finding_id", "revision"}


def test_convert_live_findings_fails_closed_on_invalid_later_entry(tmp_path: Path, capsys):
    later = _live_fleet_record()
    later.pop("body")
    source = _write_manifest(tmp_path / "live.json", _live_manifest(_live_backup_record(), later))
    destination = tmp_path / "findings.json"

    assert _run_convert(tmp_path, source, destination) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "error: invalid-entry"
    assert not destination.exists()


def test_convert_live_findings_refuses_symlink_output_and_writes_nothing(tmp_path: Path):
    source = _write_manifest(tmp_path / "live.json", _live_manifest(_live_fleet_record()))
    outside = tmp_path / "outside.json"
    outside.write_text("do not replace\n", encoding="utf-8")
    destination = tmp_path / "findings.json"
    destination.symlink_to(outside)

    assert _run_convert(tmp_path, source, destination) == 2
    assert outside.read_text(encoding="utf-8") == "do not replace\n"
    assert destination.is_symlink()


def _via_intermediate_parent(tmp_path: Path, *, label: str) -> tuple[Path, Path]:
    real = tmp_path / f"real-{label}"
    nested = real / "nested"
    nested.mkdir(parents=True)
    hop = tmp_path / f"hop-{label}"
    hop.mkdir()
    (hop / "mid").symlink_to(real)
    return nested, hop / "mid" / "nested"


def test_convert_live_findings_refuses_symlinked_intermediate_input_parent_and_reads_nothing(tmp_path: Path, capsys):
    real_dir, via_dir = _via_intermediate_parent(tmp_path, label="input")
    source_real = _write_manifest(real_dir / "live.json", _live_manifest(_live_fleet_record()))
    source = via_dir / "live.json"
    destination = tmp_path / "findings.json"
    before = source_real.read_bytes()

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        grokbot_findings.convert_live_findings(source, destination)
    assert exc.value.reason == "unsafe-manifest"
    assert _run_convert(tmp_path, source, destination) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err.strip() == "error: unsafe-manifest"
    assert PRIVATE_TITLE not in captured.out
    assert PRIVATE_BODY not in captured.out
    assert PRIVATE_TITLE not in captured.err
    assert PRIVATE_BODY not in captured.err
    assert not destination.exists()
    assert source_real.read_bytes() == before
    assert not (via_dir / "findings.json").exists()


def test_convert_live_findings_refuses_symlinked_intermediate_output_parent_and_writes_nothing(tmp_path: Path, capsys):
    source = _write_manifest(tmp_path / "live.json", _live_manifest(_live_fleet_record()))
    real_dir, via_dir = _via_intermediate_parent(tmp_path, label="output")
    canary = real_dir / "canary.txt"
    canary.write_text("keep outside\n", encoding="utf-8")
    destination = via_dir / "findings.json"
    outside_before = _walk_names(real_dir.parent)

    with pytest.raises(grokbot_findings.FindingsError) as exc:
        grokbot_findings.convert_live_findings(source, destination)
    assert exc.value.reason == "unsafe-manifest"
    assert _run_convert(tmp_path, source, destination) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err.strip() == "error: unsafe-manifest"
    assert PRIVATE_TITLE not in captured.out
    assert PRIVATE_BODY not in captured.out
    assert PRIVATE_TITLE not in captured.err
    assert PRIVATE_BODY not in captured.err
    assert canary.read_text(encoding="utf-8") == "keep outside\n"
    assert not (real_dir / "findings.json").exists()
    assert _walk_names(real_dir.parent) == outside_before


def test_convert_findings_appears_in_parser_inventory():
    from brigade.roadmap_cmd import _cli_command_paths

    assert "brigade run-cloud grokbot convert-findings" in _cli_command_paths()


def test_apply_entries_matches_path_apply_without_a_temp_manifest(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    first = _entry()
    second = _entry(finding_id="finding-beta", source_ref="cerebro://vault/findings/beta")
    manifest = _write_manifest(tmp_path / "findings.json", _manifest(first, second))
    path_result = grokbot_findings.apply(queue, owner, manifest, now=NOW)
    inbox_after_path = {path.name for path in _review_inbox(owner).glob("*.md")}

    other_queue = tmp_path / "queue-entries"
    other_owner = tmp_path / "owner-entries"
    other_queue.mkdir()
    other_owner.mkdir()
    before = {path.name for path in tmp_path.rglob("*.json")}
    entry_result = grokbot_findings.apply_entries(other_queue, other_owner, [second, first], now=NOW)
    after = {path.name for path in tmp_path.rglob("*.json")}

    assert entry_result == path_result
    assert inbox_after_path == {path.name for path in _review_inbox(other_owner).glob("*.md")}
    assert after - before <= {_marker_name(first)}
    dumped = json.dumps(entry_result, sort_keys=True)
    assert PRIVATE_TITLE not in dumped
    assert PRIVATE_BODY not in dumped


def test_apply_entries_validates_every_entry_before_storage_access(tmp_path: Path, monkeypatch):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    later = _entry(finding_id="finding-beta")
    later["title"] = ""

    def _boom(*_args: object, **_kwargs: object):
        raise AssertionError("storage opened during preflight")

    monkeypatch.setattr(grokbot_findings.grokbot_jobs, "_storage_paths", _boom)
    with pytest.raises(grokbot_findings.FindingsError, match="^invalid-title$"):
        grokbot_findings.apply_entries(queue, owner, [_entry(), later], now=NOW)
    assert not _review_inbox(owner).exists()


def test_apply_entries_delivers_in_producer_finding_revision_order(tmp_path: Path):
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    later = _entry(producer="zeta", finding_id="z-finding", revision="2")
    first = _entry(producer="alpha", finding_id="a-finding", revision="1")
    middle = _entry(producer="alpha", finding_id="b-finding", revision="1")

    result = grokbot_findings.apply_entries(queue, owner, [later, middle, first], limit=50, now=NOW)

    assert result["findings"] == [_handle(first), _handle(middle), _handle(later)]
    assert {path.name for path in _review_inbox(owner).glob("*.md")} == {
        _draft_name(first),
        _draft_name(middle),
        _draft_name(later),
    }
