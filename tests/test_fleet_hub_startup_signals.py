"""The Python hub's shutdown survives a second stop signal."""

from __future__ import annotations

import os
import signal
import time

import pytest

from brigade import fleet_admin_stats, fleet_hub, fleet_hub_events_dispatch, fleet_hub_grokbot, fleet_hub_startup

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX signals")


class _Server:
    server_address = ("127.0.0.1", 1)
    closed = False

    def serve_forever(self):
        # The first stop: SIGTERM and Ctrl-C both end serving this way.
        raise KeyboardInterrupt

    def server_close(self):
        self.closed = True


class _SecondStop:
    """A shutdown join that takes a second stop, as a process-group signal followed by a supervisor's SIGTERM."""

    def __init__(self, signum):
        self.signum = signum
        self.joined = False

    def join(self, timeout=None):
        os.kill(os.getpid(), self.signum)
        time.sleep(0.05)
        self.joined = True


@pytest.mark.parametrize("second", [signal.SIGTERM, signal.SIGINT])
def test_a_second_stop_during_shutdown_is_ignored_and_the_handlers_come_back(monkeypatch, tmp_path, capsys, second):
    server = _Server()
    joins = {"expiry": _SecondStop(second), "stats": _SecondStop(second), "events": _SecondStop(second)}
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "fake-admin-token")
    monkeypatch.setattr(fleet_hub, "make_server", lambda *_a, **_k: server)
    monkeypatch.setattr(fleet_hub_grokbot, "start_expiry_sweeper", lambda *_a, **_k: joins["expiry"])
    monkeypatch.setattr(fleet_admin_stats, "start_retention_sweeper", lambda *_a, **_k: joins["stats"])
    monkeypatch.setattr(fleet_hub_events_dispatch, "start_dispatcher", lambda *_a, **_k: joins["events"])
    before = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        code = fleet_hub_startup.run(host="127.0.0.1", port=0, db_path=tmp_path / "hub.db", token_file=None)
        after = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    finally:
        for signum, handler in before.items():
            signal.signal(signum, handler)
    assert code == 0
    assert server.closed and all(join.joined for join in joins.values())
    assert capsys.readouterr().out.splitlines()[-1] == "brigade fleet hub stopped cleanly"
    assert after == before
