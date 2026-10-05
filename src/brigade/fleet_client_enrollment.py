"""Admin-only dashboard control client with bounded, secret-safe transport."""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import fleet_client
from . import fleet_hub_enrollment as enrollment

RESPONSE_LIMIT = 131072


class DashboardClientError(fleet_client.FleetClientError):
    """Sanitized errors for credential-bearing requests."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def validate_base_url(value: str) -> str:
    try:
        if not isinstance(value, str) or re.search(r"[\s\x00-\x1f\x7f-\x9f\\]", value):
            raise ValueError()
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError()
        origin = enrollment.canonical_origin(parsed.scheme, parsed.netloc)
        fleet_client._require_encrypted_or_loopback_hub(origin)
        return origin
    except (ValueError, fleet_client.FleetClientError):
        raise DashboardClientError("dashboard URL must be an HTTPS origin or a loopback HTTP origin") from None


def _settings() -> tuple[str, str]:
    try:
        settings = fleet_client.load_fleet_settings()
    except (OSError, ValueError, fleet_client.FleetClientError):
        raise DashboardClientError("dashboard admin configuration unavailable") from None
    hub = validate_base_url(settings["hub_url"])
    admin = settings["admin_token"]
    if not admin or any(ord(c) < 32 or ord(c) == 127 for c in admin):
        raise DashboardClientError("dashboard actions require a configured admin token")
    return hub, admin


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError()
    return result


def _request(hub: str, admin: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        hub + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {admin}", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def read() -> Any:
        try:
            with opener.open(req, timeout=10) as response:
                raw = response.read(RESPONSE_LIMIT + 1)
                if len(raw) > RESPONSE_LIMIT or response.status not in {200, 201}:
                    raise ValueError()
                return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        except urllib.error.HTTPError as exc:
            try:
                exc.read(RESPONSE_LIMIT + 1)
            finally:
                exc.close()
            raise ValueError() from None

    try:
        return fleet_client._run_with_deadline(read, timeout=10)
    except Exception:
        raise DashboardClientError("dashboard request failed") from None


def _timestamp(value: Any, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError()


def enroll(*, base_url: str | None = None, label: str | None = None) -> str:
    hub, admin = _settings()
    base = validate_base_url(base_url) if base_url is not None else hub
    try:
        label = enrollment.label_value(label)
    except enrollment.EnrollmentError:
        raise DashboardClientError("dashboard label must be at most 64 characters without controls") from None
    result = _request(hub, admin, "POST", "/dashboard/enrollment", {} if label is None else {"label": label})
    try:
        if (
            not isinstance(result, dict)
            or set(result) != {"code", "expires_at"}
            or not enrollment.valid_credential(result["code"])
        ):
            raise ValueError()
        _timestamp(result["expires_at"])
    except (ValueError, TypeError, KeyError, OverflowError):
        raise DashboardClientError("invalid dashboard response") from None
    return base + "/enroll?" + urllib.parse.urlencode({"code": result["code"]})


def sessions(*, include_all: bool = False, after: str | None = None) -> dict[str, Any]:
    if after is not None and not enrollment.valid_session_id(after):
        raise DashboardClientError("invalid dashboard session cursor")
    hub, admin = _settings()
    params = {"all": "1"} if include_all else {}
    if after is not None:
        params["after"] = after
    result = _request(
        hub, admin, "GET", "/dashboard/sessions" + ("?" + urllib.parse.urlencode(params) if params else "")
    )
    try:
        if not isinstance(result, dict) or set(result) != {"sessions", "next_after"}:
            raise ValueError()
        rows, cursor = result["sessions"], result["next_after"]
        if (
            not isinstance(rows, list)
            or len(rows) > 100
            or (cursor is not None and not enrollment.valid_session_id(cursor))
        ):
            raise ValueError()
        previous = after or ""
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(enrollment.PUBLIC_COLUMNS.split(", ")):
                raise ValueError()
            if (
                not enrollment.valid_session_id(row["session_id"])
                or row["session_id"] <= previous
                or row["scope"] != "read-only"
            ):
                raise ValueError()
            previous = row["session_id"]
            if enrollment.label_value(row["label"]) != row["label"]:
                raise ValueError()
            for field in ("created_at", "expires_at", "revoked_at"):
                _timestamp(row[field], nullable=field == "revoked_at")
        if cursor is not None and (not rows or len(rows) != 100 or cursor != previous):
            raise ValueError()
    except (ValueError, TypeError, KeyError, OverflowError):
        raise DashboardClientError("invalid dashboard response") from None
    return result


def revoke(session_id: str) -> dict[str, Any]:
    if not enrollment.valid_session_id(session_id):
        raise DashboardClientError("invalid dashboard session id")
    hub, admin = _settings()
    result = _request(hub, admin, "POST", "/dashboard/sessions", {"action": "revoke", "session_id": session_id})
    if (
        not isinstance(result, dict)
        or set(result) != {"revoked", "session_id"}
        or result["revoked"] is not True
        or result["session_id"] != session_id
    ):
        raise DashboardClientError("invalid dashboard response")
    return result
