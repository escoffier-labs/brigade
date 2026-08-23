"""Fleet client: best-effort event reporting to the fleet hub (issue #1123).

``report_event`` POSTs one denormalized run event to the configured hub with a
short timeout (2s). On any failure the event is appended to a local spool
(``<brigade-home>/fleet-spool/<node_id>.jsonl``) and the caller proceeds;
reporting never raises into the journal writer. ``flush_spool`` re-POSTs
spooled events in order and truncates the spool only on full success; it is
called opportunistically before each successful report and by
``brigade fleet flush``.

Configuration (never stores secrets): ``~/.brigade/fleet.toml``::

    [fleet]
    hub_url = "http://<tailscale-ip>:3774"
    token_file = "/path/to/token"

``BRIGADE_FLEET_HUB_URL`` overrides ``hub_url``; the token comes from
``BRIGADE_FLEET_TOKEN`` or the ``token_file`` contents. When no hub is
configured the client is a no-op: zero behavior change for existing users.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import toml_compat as tomllib

FLEET_CONFIG_REL_PATH = Path(".brigade") / "fleet.toml"
SPOOL_DIRNAME = "fleet-spool"
REPORT_TIMEOUT_SECONDS = 2.0
FLUSH_TIMEOUT_SECONDS = 5.0


class FleetClientError(RuntimeError):
    """Operator-facing configuration problem (never raised on report paths)."""


def brigade_home() -> Path:
    """Brigade per-user home directory (override with BRIGADE_HOME for tests)."""
    override = os.environ.get("BRIGADE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".brigade"


def load_fleet_config() -> dict[str, str]:
    """Return {"hub_url": ..., "token": ...}; values may be absent."""
    hub_url = os.environ.get("BRIGADE_FLEET_HUB_URL", "").strip()
    token = os.environ.get("BRIGADE_FLEET_TOKEN", "").strip()
    config_path = brigade_home() / FLEET_CONFIG_REL_PATH.name
    if not hub_url and config_path.is_file():
        try:
            payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            payload = {}
        section = payload.get("fleet") if isinstance(payload, dict) else None
        if isinstance(section, dict):
            if not hub_url and isinstance(section.get("hub_url"), str):
                hub_url = section["hub_url"].strip()
            if not token:
                token_file = section.get("token_file")
                if isinstance(token_file, str) and token_file.strip():
                    try:
                        token = (
                            Path(token_file).expanduser().read_text(encoding="utf-8").strip()
                        )
                    except OSError:
                        token = ""
    return {"hub_url": hub_url, "token": token}


def spool_path(node_id: str) -> Path:
    safe_node_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in node_id) or "unknown"
    return brigade_home() / SPOOL_DIRNAME / f"{safe_node_id}.jsonl"


def resolve_node_id(base_path: Path | None = None) -> str:
    """Best-effort node id: nearest ancestor ``.brigade/node.toml``, else 'unknown'."""
    from . import node as node_mod

    start = base_path if base_path is not None else Path.cwd()
    identity = node_mod.find_identity_for_path(start)
    if identity is not None:
        return identity.node_id
    home_identity = node_mod.load_identity(brigade_home())
    if home_identity is not None:
        return home_identity.node_id
    return "unknown"


def _post_events(hub_url: str, token: str, body: dict[str, Any] | list[dict[str, Any]], *, timeout: float) -> None:
    request = urllib.request.Request(
        hub_url.rstrip("/") + "/events",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise FleetClientError(f"hub returned HTTP {response.status}")


def _spool_append(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def flush_spool(node_id: str, *, hub_url: str | None = None, token: str | None = None) -> int:
    """Re-POST spooled events in order; truncate only on full success.

    Returns the number of flushed events. Never raises for network failures.
    """
    config = load_fleet_config()
    hub = hub_url or config["hub_url"]
    tok = token or config["token"]
    path = spool_path(node_id)
    if not hub or not path.is_file():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    events: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    remaining: list[str] = []
    flushed = 0
    index = 0
    while index < len(events):
        batch = events[index : index + 100]
        try:
            _post_events(hub, tok, batch, timeout=FLUSH_TIMEOUT_SECONDS)
        except Exception:
            break
        index += len(batch)
        flushed += len(batch)
    if index < len(events):
        # Keep only events that were not delivered, in order.
        kept = [json.dumps(e, sort_keys=True) for e in events[index:]]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    return flushed


def report_event(
    event: dict[str, Any],
    *,
    base_path: Path | None = None,
    node_id: str | None = None,
) -> bool:
    """Report one event to the hub. Returns True when delivered.

    Best-effort by contract: no hub configured → False without side effects;
    delivery failure → the event lands in the local spool and False returns.
    Never raises into the caller (journal writer).
    """
    try:
        config = load_fleet_config()
        hub = config["hub_url"]
        if not hub:
            return False
        resolved_node_id = node_id or str(event.get("node_id") or "") or resolve_node_id(base_path)
        event = {**event, "node_id": resolved_node_id}
        token = config["token"]
        try:
            _post_events(hub, token, event, timeout=REPORT_TIMEOUT_SECONDS)
            flush_spool(resolved_node_id)
            return True
        except Exception:
            _spool_append(spool_path(resolved_node_id), event)
            return False
    except Exception:
        return False


def report_journal_event(envelope: dict[str, Any], *, journal_path: Path | None = None) -> bool:
    """Denormalize a run_event.v1 envelope into a fleet event and report it."""
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    event = {
        "run_id": envelope.get("run_id"),
        "repo": payload.get("repo"),
        "seat": payload.get("seat"),
        "harness": payload.get("harness"),
        "state": envelope.get("event_type"),
        "ts": envelope.get("recorded_at"),
        "sequence": envelope.get("sequence"),
        "digest": envelope.get("event_digest"),
    }
    return report_event(event, base_path=journal_path)


def fetch_status(*, hub_url: str | None = None, include_all: bool = False) -> list[dict[str, Any]]:
    """GET /status from the hub; raises FleetClientError when unreachable."""
    config = load_fleet_config()
    hub = hub_url or config["hub_url"]
    if not hub:
        raise FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    url = hub.rstrip("/") + ("/status?all=1" if include_all else "/status")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {config['token']}"})
    try:
        with urllib.request.urlopen(request, timeout=REPORT_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise FleetClientError(f"fleet hub status failed: {exc}") from exc
    runs = payload.get("runs") if isinstance(payload, dict) else None
    return list(runs) if isinstance(runs, list) else []
