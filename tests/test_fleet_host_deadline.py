"""Host lookup must finish before the Rust hub starts."""

import hashlib
import threading
import time

import pytest

from brigade import fleet_hub_engine

try:
    from brigade import fleet_host_resolver
except ImportError:  # Exercise the same regression against the base revision.
    fleet_host_resolver = fleet_hub_engine


@pytest.mark.parametrize("failure", ["timeout", "error"])
def test_host_lookup_failure_stops_startup(tmp_path, monkeypatch, capsys, failure):
    deadline = 0.1
    release = threading.Event()
    monkeypatch.setattr(fleet_host_resolver, "RESOLVE_TIMEOUT", deadline, raising=False)

    def lookup(*args, **kwargs):
        if failure == "timeout":
            release.wait(2)
            return [(2, 1, 6, "", ("203.0.113.7", 0))]
        raise OSError("resolver unavailable")

    monkeypatch.setattr(fleet_host_resolver.socket, "getaddrinfo", lookup)
    started = []
    monkeypatch.setattr(fleet_hub_engine, "_supervise", lambda *a, **kw: started.append(True))
    hub = tmp_path / "hub"
    hub.write_text("#!/bin/sh\nexit 0\n")
    hub.chmod(0o755)
    start = time.monotonic()
    try:
        code = fleet_hub_engine.run(
            host="hub.invalid",
            port=3774,
            db_path=tmp_path / "hub.db",
            token_file=None,
            allow_admin_writes=False,
            require_sealed=False,
            deck_config_path=None,
            trust_tailscale_identity=False,
            trust_forwarded_proto=False,
            hub_binary=hub,
            hub_sha256=hashlib.sha256(hub.read_bytes()).hexdigest(),
            env={},
            healthy=lambda url: pytest.fail(f"unexpected probe: {url}"),
        )
        assert time.monotonic() - start < deadline + 2
        assert code == 2
        assert "--host" in capsys.readouterr().err
        assert not started
    finally:
        release.set()
