"""Hash-stamped managed instruction blocks (issue #732)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from brigade import managed_block


def _desired() -> str:
    return "## Brigade work loop\n\nUse the brief.\n"


def test_render_stores_full_sha256_and_status_abbreviates():
    body = _desired()
    block = managed_block.render_block(body, profile="full")
    digest = managed_block.body_hash(body)
    assert f"hash:{digest}" in block
    assert f"hash:{digest[:8]} " not in block
    assert len(digest) == 64
    assessment = managed_block.assess_block(block, desired=body)
    assert assessment.status == managed_block.STATUS_CURRENT
    assert assessment.abbreviated_hash == digest[:8]
    assert assessment.recorded_hash == digest
    assert assessment.actual_hash == digest


def test_three_installs_are_byte_identical_without_duplication(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# notes\nkeep me\n", encoding="utf-8")
    desired = _desired()
    for _ in range(3):
        plan, outcome = managed_block.install_block(path, desired, owned_digest=managed_block.body_hash(desired))
        assert plan.status in {managed_block.STATUS_MISSING, managed_block.STATUS_CURRENT, managed_block.STATUS_STALE}
    text = path.read_text(encoding="utf-8")
    assert text.count("BEGIN BRIGADE INTEGRATION") == 1
    assert text.count("END BRIGADE INTEGRATION") == 1
    assert "# notes" in text
    assert "keep me" in text
    # Final reinstall is a true no-op.
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    time.sleep(0.01)
    plan, outcome = managed_block.install_block(path, desired, owned_digest=managed_block.body_hash(desired))
    assert plan.action == managed_block.ACTION_NONE
    assert outcome.status == managed_block.WRITE_NOOP
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime


@pytest.mark.parametrize(
    ("builder", "expected"),
    [
        (lambda body: None, managed_block.STATUS_MISSING),
        (
            lambda body: (
                f"{managed_block.LEGACY_USER_PROFILE_START}\n{body}\n{managed_block.LEGACY_USER_PROFILE_END}\n"
            ),
            managed_block.STATUS_STALE,
        ),
        (lambda body: managed_block.render_block(body), managed_block.STATUS_CURRENT),
        (
            lambda body: managed_block.render_block(body).replace(body, body + "edited\n", 1),
            managed_block.STATUS_LOCALLY_MODIFIED,
        ),
    ],
)
def test_check_classifies_missing_legacy_current_and_hand_edited(builder, expected):
    body = _desired()
    text = builder(body)
    if text is None:
        assessment = managed_block.assess_block("", desired=body)
        # empty file with no markers
        assert assessment.status == managed_block.STATUS_MISSING
        return
    assessment = managed_block.assess_block(text, desired=body)
    assert assessment.status == expected
    assert assessment.fix_command


def test_hand_edited_body_with_unchanged_marker_is_locally_modified_not_current():
    body = _desired()
    stamped = managed_block.render_block(body)
    # Keep the recorded hash, change only the body bytes.
    tampered = stamped.replace("Use the brief.", "Use the brief.\nuser edit", 1)
    assessment = managed_block.assess_block(tampered, desired=body)
    assert assessment.status == managed_block.STATUS_LOCALLY_MODIFIED
    assert assessment.recorded_hash != assessment.actual_hash
    plan = managed_block.plan_install(tampered, desired=body)
    assert plan.action == managed_block.ACTION_PRESERVE
    forced = managed_block.plan_install(tampered, desired=body, force=True)
    assert forced.action == managed_block.ACTION_UPDATE


def test_stale_content_updates_without_force_but_local_mod_does_not():
    body = _desired()
    old = "old body\n"
    stamped_old = managed_block.render_block(old)
    stale = managed_block.plan_install(stamped_old, desired=body)
    assert stale.status == managed_block.STATUS_STALE
    assert stale.action == managed_block.ACTION_UPDATE
    local = managed_block.render_block(body).replace(body, "tampered\n", 1)
    refused = managed_block.plan_install(local, desired=body)
    assert refused.status == managed_block.STATUS_LOCALLY_MODIFIED
    assert refused.action == managed_block.ACTION_PRESERVE


def test_remove_preserves_user_content_above_and_below():
    body = _desired()
    text = f"# above\nkeep\n{managed_block.render_block(body)}# below\nalso keep\n"
    plan = managed_block.plan_remove(text, owned_digest=managed_block.body_hash(body))
    assert plan.action == managed_block.ACTION_REMOVE
    assert plan.rendered == "# above\nkeep\n# below\nalso keep\n"
    assert "BEGIN BRIGADE" not in (plan.rendered or "")


def test_remove_refuses_locally_modified_without_force():
    body = _desired()
    text = managed_block.render_block(body).replace(body, "edited\n", 1)
    plan = managed_block.plan_remove(text, owned_digest=managed_block.body_hash(body))
    assert plan.status == managed_block.STATUS_LOCALLY_MODIFIED
    assert plan.action == managed_block.ACTION_PRESERVE
    forced = managed_block.plan_remove(text, owned_digest=managed_block.body_hash(body), force=True)
    assert forced.action == managed_block.ACTION_REMOVE


@pytest.mark.parametrize(
    "text",
    [
        "<!-- BEGIN BRIGADE INTEGRATION v:1 profile:full hash:deadbeef -->\nbody\n<!-- END BRIGADE INTEGRATION -->\n",
        (f"{managed_block.render_block('a')}{managed_block.render_block('b')}"),
        (
            "<!-- BEGIN BRIGADE INTEGRATION v:1 profile:full hash:"
            + ("a" * 64)
            + " -->body\n<!-- END BRIGADE INTEGRATION -->\n"
        ),
        "<!-- END BRIGADE INTEGRATION -->\nbody\n<!-- BEGIN BRIGADE INTEGRATION v:1 profile:full hash:"
        + ("b" * 64)
        + " -->\n",
        (
            "<!-- BEGIN BRIGADE INTEGRATION v:1 profile:full hash:" + ("c" * 64) + " -->\n"
            "outer\n<!-- BEGIN BRIGADE INTEGRATION v:1 profile:full hash:"
            + ("d" * 64)
            + " -->\ninner\n<!-- END BRIGADE INTEGRATION -->\n"
            "<!-- END BRIGADE INTEGRATION -->\n"
        ),
        "<!-- BEGIN BRIGADE INTEGRATION broken -->\nbody\n<!-- END BRIGADE INTEGRATION -->\n",
    ],
)
def test_malformed_markers_fail_closed(text):
    assessment = managed_block.assess_block(text, desired=_desired())
    assert assessment.status == managed_block.STATUS_MALFORMED
    plan = managed_block.plan_install(text, desired=_desired())
    assert plan.action == managed_block.ACTION_PRESERVE


def test_crlf_input_normalizes_for_hash_and_parse():
    body = _desired()
    lf = managed_block.render_block(body)
    crlf = lf.replace("\n", "\r\n")
    assessment = managed_block.assess_block(crlf, desired=body)
    assert assessment.status == managed_block.STATUS_CURRENT


def test_crlf_neighbors_preserved_on_stale_update():
    body = _desired()
    old = "old body\n"
    crlf_prefix = "# above\r\nkeep\r\n"
    crlf_suffix = "# below\r\nalso keep\r\n"
    crlf_block = managed_block.render_block(old).replace("\n", "\r\n")
    text = crlf_prefix + crlf_block + crlf_suffix

    plan = managed_block.plan_install(text, desired=body, owned_digest=managed_block.body_hash(old))
    assert plan.status == managed_block.STATUS_STALE
    assert plan.action == managed_block.ACTION_UPDATE
    assert plan.rendered is not None
    assert plan.rendered.startswith(crlf_prefix)
    assert plan.rendered.endswith(crlf_suffix)
    assert "\r\n" in plan.rendered
    assert "\n" not in plan.rendered.replace("\r\n", "")


def test_crlf_neighbors_preserved_on_managed_span_removal():
    body = _desired()
    crlf_prefix = "# above\r\nkeep\r\n"
    crlf_suffix = "# below\r\nalso keep\r\n"
    crlf_block = managed_block.render_block(body).replace("\n", "\r\n")
    text = crlf_prefix + crlf_block + crlf_suffix

    plan = managed_block.plan_remove(text, owned_digest=managed_block.body_hash(body))
    assert plan.action == managed_block.ACTION_REMOVE
    assert plan.rendered == crlf_prefix + crlf_suffix


def test_remove_block_preserves_crlf_neighbors_on_disk(tmp_path):
    body = _desired()
    crlf_prefix = "# above\r\nkeep\r\n"
    crlf_suffix = "# below\r\nalso keep\r\n"
    crlf_block = managed_block.render_block(body).replace("\n", "\r\n")
    expected_neighbors = (crlf_prefix + crlf_suffix).encode("utf-8")
    path = tmp_path / "AGENTS.md"
    path.write_bytes((crlf_prefix + crlf_block + crlf_suffix).encode("utf-8"))

    plan, outcome = managed_block.remove_block(path, owned_digest=managed_block.body_hash(body))
    assert plan.action == managed_block.ACTION_REMOVE
    assert outcome.status == managed_block.WRITE_WRITTEN
    assert path.read_bytes() == expected_neighbors
    assert b"\r\n" in path.read_bytes()


def test_legacy_markers_upgrade_in_place():
    body = _desired()
    legacy = (
        f"# top\n{managed_block.LEGACY_USER_PROFILE_START}\n{body}\n{managed_block.LEGACY_USER_PROFILE_END}\n# bottom\n"
    )
    plan = managed_block.plan_install(legacy, desired=body, owned_digest=managed_block.body_hash(body))
    assert plan.status == managed_block.STATUS_STALE
    assert plan.action == managed_block.ACTION_UPDATE
    assert plan.rendered is not None
    assert "BEGIN BRIGADE INTEGRATION" in plan.rendered
    assert managed_block.LEGACY_USER_PROFILE_START not in plan.rendered
    assert "# top" in plan.rendered and "# bottom" in plan.rendered


def test_symlink_target_is_skipped_with_warning(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("# real\n", encoding="utf-8")
    link = tmp_path / "AGENTS.md"
    link.symlink_to(real)
    with pytest.warns(UserWarning, match="symlinked"):
        plan, outcome = managed_block.install_block(link, _desired())
    assert outcome.status == managed_block.WRITE_SKIPPED_SYMLINK
    assert plan.action == managed_block.ACTION_PRESERVE
    assert real.read_text(encoding="utf-8") == "# real\n"


def test_symlink_swap_between_lstat_and_write_is_skipped(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# before\n", encoding="utf-8")
    target = tmp_path / "elsewhere.md"
    target.write_text("secret\n", encoding="utf-8")
    calls = {"n": 0}

    def probe(candidate: Path) -> int | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return os.lstat(candidate).st_mode
        # Swap to a symlink before publish.
        if candidate == path and path.exists() and not path.is_symlink():
            path.unlink()
            path.symlink_to(target)
        mode = os.lstat(candidate).st_mode
        return mode

    with pytest.warns(UserWarning, match="symlink"):
        outcome = managed_block.write_text_nofollow_atomic(
            path,
            managed_block.render_block(_desired()),
            lstat_probe=probe,
        )
    assert outcome.status == managed_block.WRITE_SKIPPED_SYMLINK
    assert target.read_text(encoding="utf-8") == "secret\n"
    assert path.is_symlink()


def test_other_kind_block_is_left_alone():
    body = _desired()
    other = managed_block.render_block("security notes\n", kind="SECURITY", profile="full")
    text = f"# head\n{other}"
    plan = managed_block.plan_install(text, desired=body, kind="INTEGRATION")
    assert plan.action == managed_block.ACTION_CREATE
    assert "BEGIN BRIGADE SECURITY" in (plan.rendered or "")
    assert "BEGIN BRIGADE INTEGRATION" in (plan.rendered or "")
    removed = managed_block.plan_remove(
        plan.rendered or "", kind="INTEGRATION", owned_digest=managed_block.body_hash(body)
    )
    assert "BEGIN BRIGADE SECURITY" in (removed.rendered or "")
    assert "BEGIN BRIGADE INTEGRATION" not in (removed.rendered or "")


def test_hash_comment_markers_round_trip_with_full_sha256():
    body = "PATH=/bin\n0 1 * * * true\n"
    block = managed_block.render_block(body, kind="CARE", profile="crontab", style=managed_block.MARKER_STYLE_HASH)
    digest = managed_block.body_hash(body)
    assert block.startswith(f"# BEGIN BRIGADE CARE v:1 profile:crontab hash:{digest}\n")
    assert "# END BRIGADE CARE\n" in block
    assert "<!--" not in block
    assessment = managed_block.assess_block(
        block, desired=body, kind="CARE", profile="crontab", style=managed_block.MARKER_STYLE_HASH
    )
    assert assessment.status == managed_block.STATUS_CURRENT
    assert assessment.recorded_hash == digest


def test_html_default_style_unchanged_for_integration_blocks():
    body = _desired()
    block = managed_block.render_block(body)
    assert block.startswith("<!-- BEGIN BRIGADE INTEGRATION")
    assessment = managed_block.assess_block(block, desired=body)
    assert assessment.status == managed_block.STATUS_CURRENT


def test_remove_block_skips_symlink_swap(tmp_path):
    body = _desired()
    path = tmp_path / "AGENTS.md"
    path.write_text(f"# keep\n{managed_block.render_block(body)}", encoding="utf-8")
    target = tmp_path / "elsewhere.md"
    target.write_text("secret\n", encoding="utf-8")
    calls = {"n": 0}

    def probe(candidate: Path) -> int | None:
        calls["n"] += 1
        if calls["n"] <= 2:
            return os.lstat(candidate).st_mode
        if candidate == path and path.exists() and not path.is_symlink():
            path.unlink()
            path.symlink_to(target)
        return os.lstat(candidate).st_mode

    with pytest.warns(UserWarning, match="symlink"):
        plan, outcome = managed_block.remove_block(path, owned_digest=managed_block.body_hash(body), lstat_probe=probe)
    assert outcome.status == managed_block.WRITE_SKIPPED_SYMLINK
    assert target.read_text(encoding="utf-8") == "secret\n"


def test_mixed_html_and_hash_markers_are_malformed():
    body = _desired()
    html = managed_block.render_block(body, kind="CARE", profile="full", style=managed_block.MARKER_STYLE_HTML)
    hybrid = html + managed_block.render_block(
        "other\n", kind="CARE", profile="crontab", style=managed_block.MARKER_STYLE_HASH
    )
    assessment = managed_block.assess_block(hybrid, desired=body, kind="CARE", profile="full")
    assert assessment.status == managed_block.STATUS_MALFORMED


def test_explicit_hash_style_rejects_html_stamped_markers():
    body = _desired()
    html = managed_block.render_block(body, kind="CARE", profile="full", style=managed_block.MARKER_STYLE_HTML)
    parsed = managed_block.parse_blocks(html, kind="CARE", style=managed_block.MARKER_STYLE_HASH)
    assert parsed.status == managed_block.STATUS_MALFORMED
    assert parsed.detail == "stamped managed markers do not match requested marker style"
    assessment = managed_block.assess_block(
        html, desired=body, kind="CARE", profile="full", style=managed_block.MARKER_STYLE_HASH
    )
    assert assessment.status == managed_block.STATUS_MALFORMED
    plan = managed_block.plan_install(
        html, desired=body, kind="CARE", profile="full", style=managed_block.MARKER_STYLE_HASH
    )
    assert plan.status == managed_block.STATUS_MALFORMED
    assert plan.action == managed_block.ACTION_PRESERVE


def test_explicit_html_style_rejects_hash_stamped_markers():
    body = _desired()
    hashed = managed_block.render_block(body, kind="CARE", profile="crontab", style=managed_block.MARKER_STYLE_HASH)
    parsed = managed_block.parse_blocks(hashed, kind="CARE", style=managed_block.MARKER_STYLE_HTML)
    assert parsed.status == managed_block.STATUS_MALFORMED
    assert parsed.detail == "stamped managed markers do not match requested marker style"
    assessment = managed_block.assess_block(
        hashed, desired=body, kind="CARE", profile="crontab", style=managed_block.MARKER_STYLE_HTML
    )
    assert assessment.status == managed_block.STATUS_MALFORMED
    plan = managed_block.plan_install(
        hashed, desired=body, kind="CARE", profile="crontab", style=managed_block.MARKER_STYLE_HTML
    )
    assert plan.status == managed_block.STATUS_MALFORMED
    assert plan.action == managed_block.ACTION_PRESERVE


def test_explicit_style_rejects_mixed_stamped_markers():
    body = _desired()
    html = managed_block.render_block(body, kind="CARE", profile="full", style=managed_block.MARKER_STYLE_HTML)
    hybrid = html + managed_block.render_block(
        "other\n", kind="CARE", profile="crontab", style=managed_block.MARKER_STYLE_HASH
    )
    parsed = managed_block.parse_blocks(hybrid, kind="CARE", style=managed_block.MARKER_STYLE_HASH)
    assert parsed.status == managed_block.STATUS_MALFORMED
    assert parsed.detail == "stamped html and hash-comment managed markers both present"
    assessment = managed_block.assess_block(
        hybrid, desired=body, kind="CARE", profile="full", style=managed_block.MARKER_STYLE_HASH
    )
    assert assessment.status == managed_block.STATUS_MALFORMED
    plan = managed_block.plan_install(
        hybrid, desired=body, kind="CARE", profile="full", style=managed_block.MARKER_STYLE_HASH
    )
    assert plan.status == managed_block.STATUS_MALFORMED
    assert plan.action == managed_block.ACTION_PRESERVE
