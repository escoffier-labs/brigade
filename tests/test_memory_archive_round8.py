"""Fresh filesystem activity must protect cards across policy-date boundaries."""

from datetime import datetime, timezone
import os

import pytest

from brigade import memory_card_reads as reads
from tests.test_memory_lifecycle import TODAY, card, observed_run
from tests.memory_read_fixtures import signed_memory_authority as signed_memory_authority
from tests.memory_read_fixtures import windows_scheduler as windows_scheduler


@pytest.mark.parametrize("day", [15, 16])
@pytest.mark.parametrize("activity", ["new-card", "edit", "reference"])
def test_fresh_files_protected_across_policy_date(tmp_path, day, activity):
    path = card(tmp_path)
    if activity == "new-card":
        path.unlink()
        path.write_text("---\ncard_id: old\ntrust: agent-unreviewed\n---\nNew card\n")
    elif activity == "reference":
        path = card(tmp_path, name="recent", body="[Old](old.md)")
    stamp = datetime(2026, 11, day, tzinfo=timezone.utc).timestamp()
    os.utime(path, (stamp, stamp))
    assert not observed_run(tmp_path, today=TODAY)["transitions"]
    assert (tmp_path / "memory/cards/old.md").exists()


def test_recent_read_of_old_non_index_card_protects_reference(tmp_path):
    path = card(tmp_path)
    card(tmp_path, name="recent", body="[Old](old.md)")
    assert reads.record(tmp_path, ["recent"], today=TODAY)
    assert not observed_run(tmp_path, today=TODAY)["transitions"]
    assert path.exists()


def test_new_observation_history_protects_old_card(tmp_path):
    path = card(tmp_path)
    (tmp_path / reads.READS_REL).unlink()
    assert reads.record(tmp_path, [], today=TODAY)
    result = observed_run(tmp_path, today=TODAY)
    assert not result["transitions"]
    assert result["blocked"] == "local read coverage incomplete"
    assert path.exists()


def test_far_future_filesystem_date_does_not_protect(tmp_path):
    path = card(tmp_path)
    stamp = datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(path, (stamp, stamp))
    assert observed_run(tmp_path, today=TODAY)["transitions"][-1]["to_state"] == "archived"
    assert not path.exists()
