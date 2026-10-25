"""Fleet client hardening (#1154): proxy-disabled hub transport and private,
no-follow spool files."""

from __future__ import annotations

import email
import json
import logging
import os
import stat
import sys
import tempfile
import urllib.request
from pathlib import Path

import pytest

from brigade import fleet_client


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")  # nothing listens
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "irrelevant")
    return home


def _spool() -> Path:
    return fleet_client.spool_path(fleet_client.resolve_node_id())


@pytest.mark.parametrize("existing_directory", [True, False])
def test_spool_creation_handles_access_denied_on_existing_ancestor(tmp_path, monkeypatch, existing_directory):
    """Windows mkdir may deny an existing drive root, even for user-local spools."""
    ancestor = tmp_path / "ancestor"
    if existing_directory:
        ancestor.mkdir()
    else:
        ancestor.touch()
    path = ancestor / "LocalAppData" / "brigade" / "fleet-spool" / "node.jsonl"
    mkdir = Path.mkdir

    def windows_mkdir(directory, *args, **kwargs):
        if directory == ancestor:
            raise PermissionError("Access is denied")
        return mkdir(directory, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", windows_mkdir)
    if existing_directory:
        with fleet_client._spool_lock(path) as fd:
            assert fleet_client._spool_append_locked(path, {"run_id": "offline"}, dir_fd=fd)
        assert fleet_client._read_spool(path) == [{"run_id": "offline"}]
    else:
        with pytest.raises(fleet_client.FleetClientError, match="could not be persisted"):
            with fleet_client._spool_lock(path):
                pass
        assert not path.exists()


class TestProxyDisabledTransport:
    def test_hub_traffic_ignores_proxy_environment(self, monkeypatch):
        """A configured proxy never receives hub traffic or the bearer token."""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from brigade import fleet_hub

        proxy_requests = []

        class Proxy(BaseHTTPRequestHandler):
            def do_GET(self):
                proxy_requests.append(self.path)
                self.send_error(502)

            do_POST = do_GET

            def log_message(self, *_args):
                pass

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        proxy_thread.start()
        db = Path(tempfile.mkdtemp()) / "fleet.db"
        token = "test-token-12345"
        server = fleet_hub.make_server("127.0.0.1", 0, db, token, allow_admin_writes=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.delenv("NO_PROXY", raising=False)
            monkeypatch.delenv("no_proxy", raising=False)
            proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                monkeypatch.setenv(name, proxy_url)
            url = f"http://127.0.0.1:{server.server_address[1]}"
            fleet_client._post_events_blocking(
                url,
                token,
                [
                    {
                        "node_id": "node-a",
                        "run_id": "run-1",
                        "state": "run.started",
                        "ts": "2026-01-01T00:00:00Z",
                        "sequence": 1,
                        "digest": "d" * 64,
                    }
                ],
                timeout=5,
            )
            request = urllib.request.Request(url + "/status", headers={"Authorization": f"Bearer {token}"})
            with fleet_client._hub_open(request, timeout=5) as response:
                payload = json.loads(response.read())
            assert [r["run_id"] for r in payload["runs"]] == ["run-1"]
            assert proxy_requests == []
        finally:
            server.shutdown()
            server.server_close()
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=5)
            proxy_thread.join(timeout=5)

    def test_events_go_through_the_hub_opener(self, monkeypatch):
        opened = []

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_open(request, timeout=None):
            opened.append((request.full_url, timeout))
            return FakeResponse()

        monkeypatch.setattr(fleet_client, "_HUB_OPENER", type("O", (), {"open": staticmethod(fake_open)})())
        fleet_client._post_events_blocking("http://hub.example:1", "tok", [{"a": 1}], timeout=0.5)
        assert opened == [("http://hub.example:1/events", 0.5)]


class TestSpoolPrivacy:
    def test_flush_preserves_spool_when_read_fails(self, home, monkeypatch):
        spool = _spool()
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.write_text(json.dumps({"run_id": "keep"}) + "\n", encoding="utf-8")
        original = spool.read_bytes()

        def fail_read(path, _flags, *, dir_fd=None):
            if path == spool:
                raise PermissionError("temporarily unreadable")
            return real_open(path, _flags, dir_fd=dir_fd)

        real_open = fleet_client._open_private
        monkeypatch.setattr(fleet_client, "_open_private", fail_read)

        with pytest.raises(fleet_client.FleetClientError, match="could not be read"):
            fleet_client.flush_spool(fleet_client.resolve_node_id(), hub_url="http://127.0.0.1:9", token="token")

        assert spool.read_bytes() == original

    @pytest.mark.parametrize("bad_record", [b'{"run_id":\n', b'{"run_id":', b"[]\n"])
    def test_flush_delivers_valid_events_around_malformed_records(self, home, monkeypatch, bad_record):
        spool = _spool()
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.write_bytes(b'{"run_id":"keep"}\n' + bad_record)
        delivered = []
        monkeypatch.setattr(
            fleet_client, "_post_events_blocking", lambda _hub, _token, events, **kw: delivered.extend(events)
        )

        assert (
            fleet_client.flush_spool(fleet_client.resolve_node_id(), hub_url="http://127.0.0.1:9", token="token") == 1
        )
        assert delivered == [{"run_id": "keep"}]
        assert not spool.exists()

    def test_report_event_after_torn_tail_delivers_old_and_new_events(self, home, monkeypatch):
        spool = _spool()
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.write_bytes(b'{"run_id":"old"}\n{"run_id":')
        delivered = []
        monkeypatch.setattr(
            fleet_client, "_post_events_blocking", lambda _hub, _token, events, **kw: delivered.extend(events)
        )

        assert fleet_client.report_event({"run_id": "new"})
        assert [event["run_id"] for event in delivered] == ["old", "new"]
        assert not spool.exists()

    def test_flush_preserves_spool_with_invalid_utf8(self, home):
        spool = _spool()
        spool.parent.mkdir(parents=True, exist_ok=True)
        original = b"\xff\n"
        spool.write_bytes(original)

        with pytest.raises(fleet_client.FleetClientError, match="could not be read"):
            fleet_client.flush_spool(fleet_client.resolve_node_id(), hub_url="http://127.0.0.1:9", token="token")

        assert spool.read_bytes() == original

    @pytest.mark.skipif(os.name != "posix", reason="directory durability is POSIX-specific")
    def test_spool_mutations_sync_file_and_directory(self, home, monkeypatch):
        spool = _spool()
        real_fsync = os.fsync
        synced_modes = []

        def record_fsync(fd):
            synced_modes.append(stat.S_IFMT(os.fstat(fd).st_mode))
            real_fsync(fd)

        monkeypatch.setattr(fleet_client.os, "fsync", record_fsync)
        with fleet_client._spool_lock(spool) as dir_fd:
            assert fleet_client._spool_append_locked(spool, {"run_id": "append"}, dir_fd=dir_fd)
            fleet_client._write_spool_atomic(spool, [{"run_id": "replace"}], dir_fd=dir_fd)
            fleet_client._write_spool_atomic(spool, [], dir_fd=dir_fd)

        assert synced_modes.count(stat.S_IFREG) >= 2
        assert synced_modes.count(stat.S_IFDIR) >= 3

    @pytest.mark.skipif(os.name != "posix", reason="directory durability is POSIX-specific")
    def test_first_spool_creation_syncs_each_new_directory_entry(self, home, monkeypatch):
        spool = _spool()
        real_fsync = os.fsync
        synced_modes = []

        def record_fsync(fd):
            synced_modes.append(stat.S_IFMT(os.fstat(fd).st_mode))
            real_fsync(fd)

        monkeypatch.setattr(fleet_client.os, "fsync", record_fsync)
        assert fleet_client.report_event({"run_id": "r1", "state": "run.started"}) is False
        assert spool.is_file()
        assert synced_modes.count(stat.S_IFDIR) >= 3

    @pytest.mark.skipif(os.name != "posix", reason="directory durability is POSIX-specific")
    def test_existing_execute_only_ancestor_does_not_block_spool_setup(self, tmp_path):
        root = tmp_path / "execute-only"
        root.mkdir(mode=0o700)
        home = root / "home"
        home.mkdir(mode=0o700)
        spool_dir = home / "fleet-spool"
        os.chmod(root, 0o111)
        try:
            descriptor = fleet_client._ensure_private_dir(spool_dir)
            assert descriptor is not None
            os.close(descriptor)
            assert stat.S_IMODE(os.stat(spool_dir).st_mode) == 0o700
        finally:
            os.chmod(root, 0o700)

    def test_oversized_new_event_preserves_existing_spool(self, home, monkeypatch):
        spool = _spool()
        spool.parent.mkdir(parents=True, exist_ok=True)
        fleet_client._write_spool_atomic(spool, [{"run_id": "old", "body": "o" * 50}])
        original = spool.read_text(encoding="utf-8")
        monkeypatch.setattr(fleet_client, "MAX_SPOOL_BYTES", 120)

        assert fleet_client._spool_append_locked(spool, {"run_id": "new", "body": "n" * 40}) is False

        assert spool.read_text(encoding="utf-8") == original
        assert [event["run_id"] for event in fleet_client._read_spool(spool)] == ["old"]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
    def test_spool_dir_and_file_are_private(self, home):
        assert fleet_client.report_event({"run_id": "r1", "state": "run.started"}) is False
        spool = _spool()
        assert spool.is_file(), "failed delivery must spool the event"
        assert stat.S_IMODE(os.stat(spool).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(spool.parent).st_mode) == 0o700

    @pytest.mark.skipif(os.name != "posix", reason="symlink semantics under test are POSIX-only")
    def test_symlinked_spool_path_is_refused(self, home, tmp_path):
        victim = tmp_path / "victim.jsonl"
        victim.write_text("do not touch\n", encoding="utf-8")
        spool = _spool()
        spool.parent.mkdir(parents=True)
        spool.symlink_to(victim)
        assert fleet_client.report_event({"run_id": "r1", "state": "run.started"}) is False
        assert victim.read_text(encoding="utf-8") == "do not touch\n"

    @pytest.mark.skipif(os.name != "posix", reason="os.mkfifo is POSIX-only")
    def test_non_regular_spool_file_is_refused_without_blocking(self, home):
        spool = _spool()
        spool.parent.mkdir(parents=True)
        os.mkfifo(spool)
        assert fleet_client.report_event({"run_id": "r1", "state": "run.started"}) is False
        assert stat.S_ISFIFO(os.stat(spool).st_mode)

    def test_atomic_rewrite_uses_exclusive_unpredictable_temp_names(self, home, tmp_path, monkeypatch):
        spool = _spool()
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.write_text(json.dumps({"run_id": "old"}) + "\n", encoding="utf-8")
        used: list[Path] = []
        real_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            used.append(Path(name))
            return fd, name

        monkeypatch.setattr(fleet_client.tempfile, "mkstemp", tracking_mkstemp)
        fleet_client._write_spool_atomic(spool, [{"run_id": "new"}])
        assert used and used[0] != spool.with_name(spool.name + ".tmp")
        assert used[0].parent == spool.parent
        assert not used[0].exists() and not list(spool.parent.glob("*.tmp"))
        assert [e["run_id"] for e in fleet_client._read_spool(spool)] == ["new"]

    @pytest.mark.skipif(os.name != "posix", reason="descriptor-based enforcement is POSIX-only")
    def test_swapped_spool_directory_never_receives_the_rewrite(self, home, tmp_path):
        """#1157 round 3: the atomic rewrite must address the spool through
        the validated directory descriptor. A directory swapped in between
        validation and the rewrite never receives temp files or the replace."""
        spool = _spool()
        real_dir = spool.parent
        real_dir.mkdir(parents=True)
        os.chmod(real_dir, 0o700)
        spool.write_text(json.dumps({"run_id": "old"}) + "\n", encoding="utf-8")
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        os.chmod(decoy, 0o700)
        sentinel = decoy / spool.name
        sentinel.write_text("do not touch\n", encoding="utf-8")

        with fleet_client._spool_lock(spool) as dir_fd:
            assert dir_fd is not None
            evacuated = tmp_path / "evacuated"
            # Swap the directory out from under the validated descriptor:
            # the old path now resolves to the decoy, dir_fd still points at
            # the evacuated real directory.
            real_dir.rename(evacuated)
            decoy.rename(real_dir)
            try:
                fleet_client._write_spool_atomic(spool, [{"run_id": "new"}], dir_fd=dir_fd)
            finally:
                real_dir.rename(decoy)
                evacuated.rename(real_dir)

        assert sorted(p.name for p in decoy.iterdir()) == [sentinel.name]
        assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
        rewritten = json.loads((real_dir / spool.name).read_text(encoding="utf-8"))
        assert rewritten["run_id"] == "new"
        assert not list(real_dir.glob("*.tmp")), "a temp file was left behind in the real spool directory"

    @pytest.mark.skipif(os.name != "posix", reason="descriptor-based enforcement is POSIX-only")
    def test_unenforceable_dir_privacy_refuses_the_write(self, home, monkeypatch):
        """Privacy enforcement through the descriptor is not best-effort
        (#1157 round 2): when 0700 cannot be applied (or cannot be verified),
        the spool write is refused instead of warned about."""
        spool = _spool()
        spool.parent.mkdir(parents=True)

        def refusing_fchmod(fd, mode):
            raise PermissionError("simulated immutable directory mode")

        monkeypatch.setattr(os, "fchmod", refusing_fchmod)
        with pytest.raises(fleet_client.FleetClientError):
            with fleet_client._spool_lock(spool):
                pass
        assert list(spool.parent.iterdir()) == [], "the lock file was created despite refused privacy"

    @pytest.mark.skipif(os.name != "posix", reason="descriptor-based enforcement is POSIX-only")
    def test_unverifiable_dir_mode_refuses_the_write(self, home, monkeypatch):
        """A fchmod that applies the wrong bits must be caught by the
        post-fstat verification (#1157 round 2), not waved through."""
        spool = _spool()
        spool.parent.mkdir(parents=True)
        real_fchmod = os.fchmod

        def weakening_fchmod(fd, mode):
            real_fchmod(fd, 0o755)  # enforcement silently lands permissive

        monkeypatch.setattr(os, "fchmod", weakening_fchmod)
        with pytest.raises(fleet_client.FleetClientError):
            with fleet_client._spool_lock(spool):
                pass

    @pytest.mark.skipif(os.name != "posix", reason="descriptor-based enforcement is POSIX-only")
    def test_report_event_refused_when_dir_privacy_cannot_be_enforced(self, home, monkeypatch):
        def refusing_fchmod(fd, mode):
            raise PermissionError("simulated immutable directory mode")

        monkeypatch.setattr(os, "fchmod", refusing_fchmod)
        assert fleet_client.report_event({"run_id": "r1", "state": "run.started"}) is False
        assert not _spool().exists(), "an event was spooled although privacy could not be enforced"

    @pytest.mark.skipif(os.name != "posix", reason="symlink semantics under test are POSIX-only")
    def test_symlinked_spool_directory_is_refused(self, home, tmp_path):
        victim = tmp_path / "victim-dir"
        victim.mkdir()
        spool = _spool()
        spool.parent.parent.mkdir(parents=True, exist_ok=True)
        spool.parent.symlink_to(victim, target_is_directory=True)
        assert fleet_client.report_event({"run_id": "r1", "state": "run.started"}) is False
        assert list(victim.iterdir()) == [], "writes followed a symlinked spool directory"


class TestHubRedirectOrigin:
    """Redirects stay on the request's origin, with default ports
    normalized so http://hub and http://hub:80 are the same origin
    (#1157 round 2)."""

    def _redirect(self, from_url: str, to_url: str):
        handler = fleet_client._HubRedirectHandler()
        request = urllib.request.Request(from_url, method="GET")
        headers = email.message_from_string("Content-Type: text/plain\r\n\r\n")
        return handler.redirect_request(request, fp=None, code=302, msg="Found", headers=headers, newurl=to_url)

    def test_default_port_counts_as_same_origin(self):
        assert fleet_client._same_origin("http://hub.example/a", "http://hub.example:80/b")
        assert fleet_client._same_origin("http://hub.example:80/a", "http://hub.example/b")
        assert fleet_client._same_origin("https://hub.example/a", "https://hub.example:443/b")

    def test_cross_origin_still_refused(self):
        cases = [
            ("http://hub.example/a", "https://hub.example/a"),
            ("http://hub.example/a", "http://hub.example:8080/a"),
            ("http://hub.example/a", "http://other.example/a"),
            ("https://hub.example:443/a", "https://hub.example:8443/a"),
        ]
        for first, second in cases:
            assert not fleet_client._same_origin(first, second), (first, second)

    def test_handler_follows_default_port_redirect(self):
        redirected = self._redirect("http://hub.example:80/events", "http://hub.example/events")
        assert redirected is not None
        assert redirected.full_url == "http://hub.example/events"

    def test_handler_refuses_cross_origin_redirect(self):
        assert self._redirect("http://hub.example/events", "http://evil.example/events") is None
        assert self._redirect("http://hub.example/events", "https://hub.example/events") is None


class TestSpoolPathFallbackWithoutDirFd:
    """Platforms without descriptor-relative file APIs (Windows: empty
    ``os.supports_dir_fd``) must take the documented path-based fallback
    instead of failing inside ``dir_fd=`` calls and losing undelivered
    events to a swallowed exception (#1157 round 3 regression)."""

    @pytest.fixture()
    def no_dir_fd(self, monkeypatch):
        monkeypatch.setattr(os, "supports_dir_fd", set())

    def test_platform_gate_forces_the_fallback(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert fleet_client._dir_fd_supported() is False

    def test_ensure_private_dir_returns_none_when_dir_fd_unsupported(self, tmp_path, no_dir_fd):
        spool_dir = tmp_path / "spool-dir"
        assert fleet_client._ensure_private_dir(spool_dir) is None
        assert spool_dir.is_dir()
        if os.name == "posix":
            assert stat.S_IMODE(os.stat(spool_dir).st_mode) == 0o700

    def test_spool_append_and_atomic_rewrite_survive_without_dir_fd(self, tmp_path, no_dir_fd):
        spool = tmp_path / "fleet-spool" / "node.jsonl"
        with fleet_client._spool_lock(spool) as dir_fd:
            assert dir_fd is None, "an unsupported platform must yield the path fallback, not a descriptor"
            fleet_client._spool_append_locked(spool, {"run_id": "first"}, dir_fd=dir_fd)
            assert [e["run_id"] for e in fleet_client._read_spool(spool)] == ["first"]
            fleet_client._write_spool_atomic(spool, [{"run_id": "second"}], dir_fd=dir_fd)
        assert [e["run_id"] for e in fleet_client._read_spool(spool)] == ["second"]
        assert not list(spool.parent.glob("*.tmp")), "the fallback rewrite left a temp file behind"

    def test_report_event_spools_through_the_fallback(self, home, no_dir_fd):
        assert fleet_client.report_event({"run_id": "r1", "state": "run.started"}) is False
        assert [e["run_id"] for e in fleet_client._read_spool(_spool())] == ["r1"]

    def test_report_event_logs_when_the_spool_write_fails(self, home, monkeypatch, caplog):
        def refusing_open(path):
            raise fleet_client.FleetClientError(
                f"fleet spool directory {path} could not be opened safely: simulated Windows PermissionError"
            )

        monkeypatch.setattr(fleet_client, "_ensure_private_dir", refusing_open)
        with caplog.at_level(logging.WARNING, logger="brigade.fleet"):
            assert fleet_client.report_event({"run_id": "r1", "state": "run.started"}) is False
        assert any(
            record.levelno == logging.WARNING and "could not be opened safely" in record.getMessage()
            for record in caplog.records
        ), "a failed spool write was swallowed silently"

    def test_report_event_propagates_interrupt_when_spooling_fails(self, home, monkeypatch):
        def interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(fleet_client, "_post_events", interrupt)
        monkeypatch.setattr(fleet_client, "_spool_append_locked", lambda *_args, **_kwargs: False)
        with pytest.raises(KeyboardInterrupt):
            fleet_client.report_event({"run_id": "r1", "state": "run.started"})

    def test_report_event_propagates_interrupt_when_spool_setup_fails(self, home, monkeypatch):
        def interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt

        def fail_spool_setup(*_args, **_kwargs):
            raise fleet_client.FleetClientError("simulated spool setup failure")

        monkeypatch.setattr(fleet_client, "_post_events", interrupt)
        monkeypatch.setattr(fleet_client, "_ensure_private_dir", fail_spool_setup)
        with pytest.raises(KeyboardInterrupt):
            fleet_client.report_event({"run_id": "r1", "state": "run.started"})


def _hold_spool_lock(path, connection):
    with fleet_client._spool_lock(Path(path)):
        connection.send("locked")
        connection.recv()
    connection.close()


def test_spool_lock_timeout_bounds_thread_contention(tmp_path):
    import threading
    import time

    path = tmp_path / "spool" / "node.jsonl"
    ready, release = threading.Event(), threading.Event()

    def hold():
        with fleet_client._spool_lock(path):
            ready.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert ready.wait(5)
        started = time.monotonic()
        with pytest.raises(fleet_client.FleetClientError, match="^fleet spool lock timed out$"):
            with fleet_client._spool_lock(path, timeout=0.04):
                pytest.fail("entered held lock")
        assert time.monotonic() - started < 1
    finally:
        release.set()
        holder.join(5)
    assert not holder.is_alive()
    with fleet_client._spool_lock(path, timeout=0):
        pass


def test_spool_lock_timeout_bounds_process_contention_and_closes_descriptors(tmp_path, monkeypatch):
    import multiprocessing

    path = tmp_path / "spool" / "node.jsonl"
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    holder = context.Process(target=_hold_spool_lock, args=(str(path), child))
    holder.start()
    descriptors = []
    real_open = fleet_client.os.open

    def record_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        descriptors.append(fd)
        return fd

    try:
        assert parent.poll(10) and parent.recv() == "locked"
        monkeypatch.setattr(fleet_client.os, "open", record_open)
        with pytest.raises(fleet_client.FleetClientError, match="^fleet spool lock timed out$"):
            with fleet_client._spool_lock(path, timeout=0.04):
                pytest.fail("entered held process lock")
        assert descriptors
        for fd in set(descriptors):
            with pytest.raises(OSError):
                os.fstat(fd)
        assert fleet_client._SPOOL_PROCESS_LOCK.acquire(blocking=False)
        fleet_client._SPOOL_PROCESS_LOCK.release()
    finally:
        parent.send("release")
        holder.join(10)
        if holder.is_alive():
            holder.terminate()
            holder.join(5)
        parent.close()
        child.close()
    assert holder.exitcode == 0
    with fleet_client._spool_lock(path, timeout=0):
        pass


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), True, "1"])
def test_spool_lock_rejects_invalid_timeout_before_storage(tmp_path, timeout):
    path = tmp_path / "new" / "node.jsonl"
    with pytest.raises(fleet_client.FleetClientError, match="^invalid fleet spool lock timeout$"):
        with fleet_client._spool_lock(path, timeout=timeout):
            pytest.fail("accepted invalid timeout")
    assert not path.parent.exists()


@pytest.mark.parametrize("timeout", [None, 0, 0.12])
def test_spool_lock_windows_flags_and_bounded_retries(tmp_path, monkeypatch, timeout):
    import errno
    from types import SimpleNamespace

    clock = [0.0]
    calls, sleeps = [], []
    windows = SimpleNamespace(LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=3)

    def locking(fd, mode, size):
        calls.append((mode, size))
        if mode == windows.LK_UNLCK:
            return
        if timeout is None and len(calls) > 1:
            return
        raise OSError(errno.EACCES, "busy")

    def sleep(seconds):
        assert 0 < seconds <= 0.05
        sleeps.append(seconds)
        clock[0] += seconds

    windows.locking = locking
    monkeypatch.setattr(fleet_client, "fcntl", None)
    monkeypatch.setattr(fleet_client, "msvcrt", windows)
    monkeypatch.setattr(fleet_client, "_ensure_private_dir", lambda path: None)
    monkeypatch.setattr(fleet_client.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(fleet_client.time, "sleep", sleep)
    path = tmp_path / "node.jsonl"
    if timeout is None:
        with fleet_client._spool_lock(path):
            pass
        assert calls == [(windows.LK_LOCK, 1), (windows.LK_LOCK, 1), (windows.LK_UNLCK, 1)]
        assert sleeps == []
    else:
        with pytest.raises(fleet_client.FleetClientError, match="^fleet spool lock timed out$"):
            with fleet_client._spool_lock(path, timeout=timeout):
                pytest.fail("entered held Windows lock")
        assert all(mode == windows.LK_NBLCK and size == 1 for mode, size in calls)
        assert clock[0] == pytest.approx(timeout)
        assert fleet_client._SPOOL_PROCESS_LOCK.acquire(blocking=False)
        fleet_client._SPOOL_PROCESS_LOCK.release()


def test_spool_lock_windows_bounded_success_unlocks(tmp_path, monkeypatch):
    from types import SimpleNamespace

    calls = []
    windows = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=3, locking=lambda fd, mode, size: calls.append((mode, size)))
    monkeypatch.setattr(fleet_client, "fcntl", None)
    monkeypatch.setattr(fleet_client, "msvcrt", windows)
    monkeypatch.setattr(fleet_client, "_ensure_private_dir", lambda path: None)
    with fleet_client._spool_lock(tmp_path / "node.jsonl", timeout=0):
        pass
    assert calls == [(windows.LK_NBLCK, 1), (windows.LK_UNLCK, 1)]


_INSPECT_SETTINGS = {"node_id": "fake-node", "hub_url": "https://hub.example.invalid", "token": "fake-token"}


def _inspect_with(monkeypatch, payload):
    monkeypatch.setattr(fleet_client, "_post_claim_blocking", lambda url, token, body, *, timeout: (200, payload))
    monkeypatch.setattr(fleet_client, "_run_with_deadline", lambda fn, **kw: fn())
    return fleet_client.inspect_claim("fake-target", **_INSPECT_SETTINGS)


@pytest.mark.parametrize(
    "payload",
    [
        {"inspected": True},
        *({"inspected": True, "claim": value} for value in ([], "", "x", True, False, 0, 1, [{"owner_node": "n"}])),
        *(
            {"inspected": True, "claim": claim}
            for claim in ({}, {"target": "t"}, {"owner_node": None}, {"owner_node": 5}, {"owner_node": ""})
        ),
    ],
)
def test_malformed_inspect_response_is_retryable_hub_unavailable(monkeypatch, payload):
    decision = _inspect_with(monkeypatch, payload)
    assert decision.granted is False
    assert decision.reason == "hub-unavailable"
    assert decision.claim is None


@pytest.mark.parametrize(
    "owner_node",
    ["unknown", " ", "bad id", "a\x00b", "a\x1fb", "a\x7fb", "node\n", "-lead", ".lead", "_lead", "x" * 129, "né"],
)
def test_invalid_inspect_owner_is_retryable_hub_unavailable(monkeypatch, owner_node):
    decision = _inspect_with(monkeypatch, {"inspected": True, "claim": {"owner_node": owner_node}})
    assert decision.granted is False
    assert decision.reason == "hub-unavailable"
    assert decision.claim is None


@pytest.mark.parametrize("owner_node", ["a", "fake-node", "Node_1.x-2", "x" * 128])
def test_valid_inspect_owner_is_granted(monkeypatch, owner_node):
    claim = {"owner_node": owner_node}
    decision = _inspect_with(monkeypatch, {"inspected": True, "claim": claim})
    assert decision.granted is True and decision.reason == "ok"
    assert decision.claim == claim


@pytest.mark.parametrize(
    "claim",
    [None, {"owner_node": "other-node"}, {"owner_node": "fake-node"}, {"target": "t", "owner_node": "n"}],
)
def test_wellformed_inspect_response_is_granted(monkeypatch, claim):
    decision = _inspect_with(monkeypatch, {"inspected": True, "claim": claim})
    assert decision.granted is True and decision.reason == "ok"
    assert decision.claim == claim


def test_owned_inspect_response_passes_through_lock_run_dir_and_generation(monkeypatch):
    claim = {"owner_node": "fake-node", "generation": 3}
    decision = _inspect_with(
        monkeypatch,
        {"inspected": True, "claim": claim, "owned": True, "generation": 3, "lock_run_dir": "/fake/run"},
    )
    assert decision.granted is True and decision.reason == "ok"
    assert decision.claim == claim
    assert decision.lock_run_dir == "/fake/run"
    assert decision.generation == 3
