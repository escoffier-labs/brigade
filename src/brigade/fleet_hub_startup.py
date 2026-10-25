"""Fleet Hub startup configuration, identity admission and server lifecycle.

Domain operations are resolved through fleet_hub at call time. The public
entry points there retain their signatures and replaceable server seams.
"""

from __future__ import annotations

import argparse
import dataclasses
import ipaddress
import os
import sqlite3
import sys
import threading
from datetime import timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import fleet_command_deck, fleet_hub_events, fleet_hub_sealed


def _load_token(args: argparse.Namespace) -> str:
    from . import fleet_hub as hub

    token = os.environ.get("BRIGADE_FLEET_TOKEN", "").strip()
    if token:
        return token
    token_file = getattr(args, "token_file", None)
    if token_file is not None:
        try:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise hub.FleetHubError(f"could not read token file: {exc}") from exc
        if token:
            return token
    raise hub.FleetHubError("no fleet hub token: set BRIGADE_FLEET_TOKEN or pass --token-file")


def _is_loopback_address(address: str) -> bool:
    """True for IPv4, IPv6, and IPv4-mapped IPv6 loopback addresses."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if parsed.is_loopback:
        return True
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped.is_loopback
    return False


def make_server(
    host: str,
    port: int,
    db_path: Path,
    token: str,
    *,
    allow_admin_writes: bool = False,
    deck_config_path: Path | None = None,
    trust_tailscale_identity: bool = False,
    trust_forwarded_proto: bool = False,
    storage_policy: fleet_hub_sealed.StoragePolicy | None = None,
    require_sealed: bool = False,
) -> ThreadingHTTPServer:
    """Build (but do not serve) the hub HTTPServer; used by tests.

    The database is created/migrated exactly once here, before the socket
    exists, so no request can ever arrive first (#1161). Request handlers then
    use non-migrating connections (``open_db``), keeping token lookup live per
    request. Raises ``hub.FleetHubError``/``sqlite3.Error`` on an unusable or
    too-new database. ``deck_config_path`` is loaded exactly once, before the
    socket is bound; an invalid file raises ``DeckConfigError`` without
    creating anything.
    """
    from . import fleet_hub as hub

    if trust_forwarded_proto and not hub._is_loopback_address(host):
        raise hub.FleetHubError("--trust-forwarded-proto requires a numeric loopback --host")
    if trust_tailscale_identity and not hub._is_loopback_address(host):
        raise hub.FleetHubError("--trust-tailscale-identity requires a loopback --host")
    if deck_config_path is not None:
        deck_config = fleet_command_deck.load_config(Path(deck_config_path))
    else:
        deck_config = fleet_command_deck.DeckConfig()
    try:
        # Explicit policies override numeric settings, but the sealing flag
        # must still be validated and may only strengthen enforcement.
        environment = (
            os.environ
            if storage_policy is None
            else {"BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED": os.environ.get("BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED", "")}
        )
        environment_policy = fleet_hub_sealed.policy_from_environment(environment)
        policy = storage_policy or environment_policy
    except ValueError as exc:
        raise hub.FleetHubError(str(exc)) from exc
    if require_sealed or environment_policy.require_sealed:
        policy = dataclasses.replace(policy, require_sealed=True)
    # Upgrade schema without applying an unvalidated runtime retention policy.
    # Direct-library hub.init_db callers retain their historical pruning default.
    conn = hub.init_db(Path(db_path), storage_policy=policy, prune_messages=False)
    from . import fleet_admin_stats, fleet_hub_identity, fleet_hub_messages

    try:
        first_batch = True
        while True:
            # Identity replacement and destructive retention share one writer
            # transaction. Revalidate after each commit between purge batches.
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    fleet_hub_identity.validate_storage_policy(fleet_hub_identity.identity(conn), policy)
                except (ValueError, TypeError, AttributeError, RecursionError) as exc:
                    raise hub.FleetHubError(f"invalid startup identity storage policy: {exc}") from exc
                if first_batch:
                    fleet_admin_stats._prune(conn, fleet_admin_stats.datetime.now(timezone.utc))
                    fleet_hub_messages._prune_expired(conn, storage_policy=policy)
                    conn.execute(
                        "DELETE FROM fleet_questions WHERE created_at<?",
                        (fleet_hub_messages._expiry_cutoff(policy),),
                    )
                    # _prune_expired already purged one bounded content batch.
                    purged = fleet_hub_sealed.PURGE_BATCH_SIZE
                else:
                    purged = fleet_hub_sealed.purge_content(conn)
            first_batch = False
            if purged < fleet_hub_sealed.PURGE_BATCH_SIZE:
                break
    finally:
        conn.close()

    return hub.BoundedThreadingHTTPServer(
        (host, port),
        hub.make_handler(
            token,
            Path(db_path),
            allow_admin_writes=allow_admin_writes,
            deck_config=deck_config,
            trust_tailscale_identity=trust_tailscale_identity,
            trust_forwarded_proto=trust_forwarded_proto,
            storage_policy=policy,
        ),
        db_path=Path(db_path),
    )


def run(
    *,
    host: str | None,
    port: int,
    db_path: Path,
    token_file: Path | None,
    allow_admin_writes: bool = False,
    deck_config_path: Path | None = None,
    trust_tailscale_identity: bool = False,
    trust_forwarded_proto: bool = False,
    storage_policy: fleet_hub_sealed.StoragePolicy | None = None,
    require_sealed: bool = False,
) -> int:
    from . import fleet_hub as hub

    if not host:
        print("error: --host is required (the hub never binds all interfaces by default)", file=sys.stderr)
        return 2
    try:
        token = hub._load_token(argparse.Namespace(token_file=token_file))
    except hub.FleetHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        # MCP Events stay off unless the operator names callback hosts in the environment.
        fleet_hub_events.configure(fleet_hub_events.config_from_environment(os.environ))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        server = hub.make_server(
            host,
            port,
            Path(db_path).expanduser(),
            token,
            allow_admin_writes=allow_admin_writes,
            deck_config_path=deck_config_path,
            trust_tailscale_identity=trust_tailscale_identity,
            trust_forwarded_proto=trust_forwarded_proto,
            storage_policy=storage_policy,
            require_sealed=require_sealed,
        )
    except (hub.FleetHubError, sqlite3.Error, fleet_command_deck.DeckConfigError) as exc:
        # Startup migration is the one place the hub touches the schema: if
        # it cannot be done, refuse to serve rather than 500-ing every request.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    bound_host, bound_port = str(server.server_address[0]), int(server.server_address[1])
    mode = "admin writes allowed" if allow_admin_writes else "node tokens required for writes"
    tailscale_mode = "tailscale identity trusted" if trust_tailscale_identity else "tailscale identity ignored"
    print(
        f"brigade fleet hub listening on {bound_host}:{bound_port} (db {Path(db_path).expanduser()}; {mode}; {tailscale_mode})"
    )
    from . import fleet_admin_stats, fleet_hub_grokbot

    sweeper_stop = threading.Event()
    sweeper_thread = fleet_hub_grokbot.start_expiry_sweeper(Path(db_path).expanduser(), stop=sweeper_stop)
    from .fleet_hub_events_dispatch import start_dispatcher

    events_thread = start_dispatcher(Path(db_path).expanduser(), sweeper_stop)
    stats_thread = fleet_admin_stats.start_retention_sweeper(
        Path(db_path).expanduser(),
        stop=sweeper_stop,
        storage_policy=getattr(getattr(server, "RequestHandlerClass", None), "message_storage_policy", None),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sweeper_stop.set()
        sweeper_thread.join(timeout=5)
        stats_thread.join(timeout=5)
        events_thread.join(timeout=5)
        server.server_close()
    return 0
