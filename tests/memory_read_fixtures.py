"""Provision test authority and observe coverage through the real recorder."""

from datetime import timedelta
import sys

import pytest

from brigade import memory_card_reads as reads, receipt_signing


def signing_key(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".brigade" / receipt_signing.KEY_NAME
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("12" * 32 + "\n")
        path.chmod(0o600)


def coverage(root, start, end):
    signing_key(root)
    ledger = reads.load(root)
    # Model a trusted recorder's historical snapshot without hundreds of fsyncs.
    # The recorder's continuity, gap and corruption paths have direct tests.
    value = {
        "tracking_since": start.isoformat(),
        "healthy_since": start.isoformat(),
        "heartbeat": end.isoformat(),
        "coverage_days": [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)],
        "last_read": {key: day.isoformat() for key, day in ledger.last_read.items()},
    }
    value["mac"] = reads.authenticate(root, reads.READS_REL, value)
    reads._write_json(root, reads.READS_REL, value)


# Protocol tests inject a trusted key on Windows. Production key confinement is
# tested separately and remains unavailable until its ACL checks are supported.
@pytest.fixture(autouse=True)
def signed_memory_authority(monkeypatch):
    if sys.platform == "win32":

        def trusted_test_key(target):
            path = target / ".brigade" / receipt_signing.KEY_NAME
            return bytes.fromhex("12" * 32) if path.is_file() else None

        monkeypatch.setattr(receipt_signing, "workspace_key", trusted_test_key)


@pytest.fixture(autouse=True)
def windows_scheduler(monkeypatch):
    """Lifecycle tests must not query the runner's real Task Scheduler."""
    if sys.platform == "win32":
        import subprocess

        run = subprocess.run

        def isolated(args, *positional, **kwargs):
            if args == ["schtasks", "/Query", "/FO", "CSV", "/V"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return run(args, *positional, **kwargs)

        monkeypatch.setattr(subprocess, "run", isolated)


@pytest.fixture(autouse=True)
def automatic_archive_test_mode(monkeypatch):
    """Exercise the gated archive implementation with simulated verified hub coverage.

    Explicitly configured hub scenarios still use the real stats validator.
    Gate and absent-hub regressions deliberately do not import this fixture.
    """
    from brigade import memory_hub_client as hubc, memory_lifecycle

    monkeypatch.setattr(memory_lifecycle, "AUTOMATIC_ARCHIVE_ENABLED", True)
    real = reads.hub_last_reads

    def verified_empty_hub(target, **kwargs):
        settings = hubc.load_settings(target)
        if not settings.enabled:
            return {}
        return real(target, **kwargs)

    monkeypatch.setattr(reads, "hub_last_reads", verified_empty_hub)


def pending_decision_id(target):
    """Select the single interrupted decision, including an unpublished prepare."""
    import re
    from brigade import memory_care_journal as journal
    from brigade.memory_care_pending import load

    ids = set(load(target))
    for operation in journal.kernel.unfinished_operations(target):
        match = re.fullmatch(rf"care-({journal.DECISION_ID})-(prepare|finish|repair)", operation.operation_id)
        if match:
            ids.add(match[1])
    assert len(ids) == 1, ids
    return ids.pop()


def operator_recovery(target, decision_id, *, stages=()):
    """Recover the named decision, then exercise the requested tend stages."""
    from brigade import memory_care_journal as journal, memory_care_tend as tend

    recovery = journal.resolve_prepared(target, decision_id=decision_id)
    result = tend.run(target, stages=stages)
    assert result["recovery"] == []
    return {**result, "recovery": recovery}
