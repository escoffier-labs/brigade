"""Opt-in, repository-bound HTTPS metadata reads for Actions collectors.

The caller supplies authorization in memory. No credential discovery, ambient
proxy, redirects, retries or alternate hosts are used. Socket operations use a
finite timeout, which is not a hard wall-clock deadline: DNS resolution and a
peer sending bytes slowly can extend the total duration. Collectors separately
reject responses that arrive after their elapsed budget.

Successful bodies are bounded raw metadata bytes. Existing collectors own JSON
validation and sanitization. Error bodies and network exception text are dropped.
"""

from __future__ import annotations

import http.client
import math
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from brigade.claude_actions_evidence import MAX_PROVIDER_ID, _repository
from brigade.claude_review_import import GetResponse

_MAX_BYTES = 1024 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9_.~+/-]{1,4096}=*")
_ROUTE = re.compile(
    r"(?:workflows/(?P<workflow>[1-9][0-9]{0,18})(?P<runs>/runs)?"
    r"|runs/(?P<run>[1-9][0-9]{0,18})"
    r"(?:/attempts/(?P<attempt>[1-9][0-9]{0,18})(?P<jobs>/jobs)?|(?P<artifacts>/artifacts)))"
)


class TransportError(RuntimeError):
    """Metadata could not be read. Contains no original network exception."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> None:
        # Returning None lets HTTPErrorProcessor report the original status.
        # Do not parse Location or construct a request to its destination.
        return None

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


def _bounded_integer(value: object, ceiling: int) -> int:
    if type(value) is not int or not 0 < value <= ceiling:
        raise ValueError("invalid read bound")
    return value


class GitHubActionsTransport:
    """GET only numeric metadata endpoints in one canonical GitHub repository.

    ``timeout_seconds`` defaults to 10 and accepts finite seconds in (0, 300].
    ``get`` accepts byte caps in [1, 1 MiB]. It reads at most cap + 1 bytes to
    determine truncation, returning at most cap bytes. List routes accept only
    ``page`` and ``per_page`` in [1, 100], as integers or canonical ASCII decimals.
    Workflow metadata and run-attempt routes accept no pagination parameters.

    The token is optional, never loaded from the environment, and omitted from
    repr and errors. Only HTTP 200 bodies are retained. Other HTTP statuses are
    returned with empty bodies. Network failures raise a redacted TransportError.
    """

    def __init__(
        self,
        *,
        repository: str,
        token: str | None = None,
        timeout_seconds: float = 10,
    ) -> None:
        canonical = _repository(repository)
        if canonical is None:
            raise ValueError("invalid repository")
        if (
            type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds <= 300
            or not math.isfinite(timeout_seconds)
        ):
            raise ValueError("invalid socket timeout")
        if token is not None and (
            type(token) is not str or not 1 <= len(token) <= 4096 or _TOKEN.fullmatch(token) is None
        ):
            raise ValueError("invalid bearer token")
        self._base = f"/repos/{canonical}/actions/"
        self._token = token
        self._timeout_seconds = timeout_seconds

    def get(self, path: str, *, params: Mapping[str, str | int], max_bytes: int) -> GetResponse:
        """Validate the entire request before I/O, then issue one explicit GET."""
        cap = _bounded_integer(max_bytes, _MAX_BYTES)
        if type(path) is not str or not path.startswith(self._base):
            raise ValueError("invalid metadata route")
        route = _ROUTE.fullmatch(path[len(self._base) :])
        if route is None or any(
            int(value) > MAX_PROVIDER_ID for value in (route["workflow"], route["run"], route["attempt"]) if value
        ):
            raise ValueError("invalid metadata route")
        if not isinstance(params, Mapping) or len(params) > 2:
            raise ValueError("invalid pagination")
        pagination: dict[str, int] = {}
        for name, value in params.items():
            if name not in ("page", "per_page") or not any((route["runs"], route["jobs"], route["artifacts"])):
                raise ValueError("invalid pagination")
            if type(value) is str:
                if re.fullmatch(r"[1-9][0-9]{0,2}", value) is None:
                    raise ValueError("invalid pagination")
                value = int(value)
            pagination[name] = _bounded_integer(value, 100)
        url = "https://api.github.com" + path
        if pagination:
            url += "?" + urllib.parse.urlencode(pagination)
        headers = {"Accept": "application/vnd.github+json", "Accept-Encoding": "identity", "User-Agent": "brigade-cli"}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _NoRedirect(),
                urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            )
            with opener.open(request, timeout=self._timeout_seconds) as response:
                status = response.status
                if status != 200:
                    return GetResponse(status, b"")
                body = response.read(cap + 1)
                # read(amt) returns a short body without error when the peer
                # closes before Content-Length. Only a cap overrun is truncation.
                if len(body) <= cap and getattr(response, "length", None):
                    raise TransportError("metadata transport unavailable")
                return GetResponse(status, body[:cap], truncated=len(body) > cap)
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                error.close()
            except (OSError, http.client.HTTPException):
                pass
            return GetResponse(status, b"")
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            pass
        # Raise outside the except block so __context__ cannot retain secrets.
        raise TransportError("metadata transport unavailable")
