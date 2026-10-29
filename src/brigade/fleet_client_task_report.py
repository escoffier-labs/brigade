"""Publish a v2 cloud task report and confirm the Hub's stored readback."""

from __future__ import annotations

import json
import urllib.request
from typing import TYPE_CHECKING

from . import fleet_client as _client, fleet_client_messages
from .fleet_client import SessionWriteResult
from .fleet_client_message_credentials import FleetMessageHubError, MessageAuthority, MessageAuthorityError

if TYPE_CHECKING:
    from .fleet_session_presence import SessionSnapshot


def publish_task_session(snapshot: SessionSnapshot, *, expected_node: str) -> SessionWriteResult:
    """Confirm a v2 readback under one captured host node authority. No retries."""
    attempted = False
    try:
        authority = MessageAuthority.capture(node_credential=True)
        if not _client._node_id_is_claimable(expected_node) or authority.node_id != expected_node:
            return SessionWriteResult(False, "node_identity_mismatch")
        _client._require_encrypted_or_loopback_hub(authority.hub)
        principal = fleet_client_messages.identity(authority=authority, timeout=_client.REPORT_TIMEOUT_SECONDS)
        if principal != {"principal": expected_node}:
            return SessionWriteResult(False, "node_identity_mismatch")
        # Re-read only to refuse changed configuration, never to switch endpoint
        # or credential between authentication and publication.
        authority.validate()
        body = _client._session_request_body(snapshot, action="upsert")
        request = urllib.request.Request(
            authority.hub.rstrip("/") + "/sessions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {authority.token}"},
            method="POST",
        )
        attempted = True
        with _client._hub_open(request, timeout=_client.REPORT_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                return SessionWriteResult(False, f"http_status:{response.status}")
            payload = json.loads(_client._read_bounded_response(response, limit=64 * 1024))
        authority.validate()
        session = payload.get("session") if isinstance(payload, dict) else None
        if not isinstance(session, dict):
            return SessionWriteResult(False, "invalid_session_response")
        expected = {
            "node_id": expected_node,
            "harness": "dot",
            "session_id": snapshot.session_id,
            "repo_identity": snapshot.repo_identity or None,
            "cloud_context": dict(snapshot.cloud_context or {}),
            "ttl_seconds": snapshot.ttl_seconds,
        }
        # JSON equality is type-strict (True is not sequence 1), includes the
        # complete replacement context, and refuses old Hubs dropping v2 facts.
        actual = {key: session.get(key) for key in expected}
        if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
            return SessionWriteResult(False, "invalid_session_response")
        return SessionWriteResult(True)
    except MessageAuthorityError:
        return SessionWriteResult(False, "credential_changed" if attempted else "node_auth_required")
    except FleetMessageHubError as exc:
        return SessionWriteResult(False, f"http_status:{exc.status}" if exc.status else "identity_unconfirmed")
    except Exception as exc:
        return SessionWriteResult(False, _client._classify_session_write_error(exc))
