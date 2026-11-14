"""Server-side expiry remains paired with its ownership projections until contract v2."""

import threading

from brigade import cli, fleet_hub, fleet_hub_grokbot, fleet_owners, grokbot_jobs
from tests.test_fleet_hub_grokbot_expiry import _enroll, _enqueue, _backdate


def test_stored_job_expires_without_client_creation(tmp_path, monkeypatch, capsys):
    from tests._home import set_home

    set_home(monkeypatch, tmp_path / "home")
    database = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(database)
    _enroll(conn)
    job = _enqueue(conn)
    assert fleet_owners._grokbot_jobs(conn, {})
    assert not hasattr(grokbot_jobs, "enqueue")
    assert cli.main(["run", "cloud", "grokbot", "enqueue", "--obsolete"]) == 2
    assert "this was removed" in capsys.readouterr().err
    _backdate(conn, seconds=86460)
    done = threading.Event()
    stop = threading.Event()
    sweep = fleet_hub_grokbot.sweep_expired_jobs

    def observed(*args, **kwargs):
        result = sweep(*args, **kwargs)
        done.set()
        return result

    monkeypatch.setattr(fleet_hub_grokbot, "sweep_expired_jobs", observed)
    thread = fleet_hub_grokbot.start_expiry_sweeper(database, interval=0.01, stop=stop)
    try:
        assert done.wait(5)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert conn.execute("SELECT state FROM grokbot_jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0] == "expired"
    assert fleet_owners._grokbot_jobs(conn, {}) == []
    assert conn.execute("SELECT COUNT(*) FROM grokbot_jobs").fetchone()[0] == 1
    conn.close()
