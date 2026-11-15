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
