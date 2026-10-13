"""Contract checks for the native Linux Fleet Hub runbook (#1632)."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "runbooks" / "fleet-hub-linux.md"


def test_linux_runbook_covers_installation_and_service_lifecycle():
    text = RUNBOOK.read_text()

    assert "brigade-cli==0.28.0" in text
    assert all(tool in text for tool in ("pipx", "uv tool", "python3 -m venv"))
    assert "User=brigade-hub" in text
    assert "127.0.0.1" in text
    assert "systemctl enable --now brigade-fleet.service" in text
    assert all(
        command in text for command in ("systemctl start", "systemctl stop", "systemctl status", "journalctl -u")
    )
    assert "systemctl reboot" in text
    assert "userdel brigade-hub" in text


def test_linux_runbook_covers_data_safety_and_acceptance():
    text = RUNBOOK.read_text()

    assert "fleet-hub.db-wal" in text
    assert "NFS" in text and "SMB" in text and "cloud-synced" in text
    assert "sqlite3.sqlite_version" in text
    assert "PRAGMA journal_mode" in text
    assert "PRAGMA integrity_check" in text
    assert "PRAGMA user_version" in text
    assert ".backup" in text
    assert "post-backup writes" in text
    assert all(
        term in text for term in ("node authentication", "ingestion", "dashboard enrollment", "crash", "restore")
    )


def test_runbook_index_links_native_linux_and_labels_support():
    text = (ROOT / "docs" / "runbooks" / "README.md").read_text()

    assert "fleet-hub-linux.md" in text
    assert "Tested" in text
    assert "Proposed" in text
