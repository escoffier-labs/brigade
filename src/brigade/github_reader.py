"""Injected node-local GitHub GET transport, with no Hub wiring.

The required runner owns process execution, bounded stdout/stderr capture and a
blocking timeout. It must apply the supplied environment overrides to the child.
An override of None removes a variable. It must not log the environment or
output. No shell, credentials or identity
discovery are used here. One BudgetLedger file serializes all its readers.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol
from urllib.parse import quote, unquote, urlencode

from brigade.claude_review_import import GetResponse
from brigade.github_read_ledger import BudgetLedger

_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
_PATH = re.compile(r"/[A-Za-z0-9_./%~-]{1,1024}")
_CLASS = re.compile(r"[a-z][a-z0-9_-]{0,39}")
_SECRET = re.compile(r"token|secret|password|credential|authorization|api.?key", re.I)
_HEADERS = {"etag", "x-ratelimit-remaining", "x-ratelimit-reset", "x-poll-interval", "retry-after", "link"}
_STATS = {
    "requests",
    "hits",
    "misses",
    "cache_hits",
    "cache_misses",
    "denied",
    "fallbacks",
    "local_fallbacks",
    "service_errors",
    "hit_rate",
    "cache_served_responses",
    "uncached_outcomes",
    "stale",
    "bypass",
    "unknown",
    "coalesced",
    "eligible_requests",
    "backend_requests",
    "saved_github_requests",
    "eligible_cache_requests",
    "eligible_cache_hit_rate",
    "cache_hit_rate",
    "errors",
    "cache_stale",
    "cache_unknown",
    "cache_bypass",
    "cacheable_requests",
    "cacheable_hit_rate",
    "bypass_rate",
    "avg_duration_ms",
}
_CACHE_STATS = {"total_entries", "fresh_entries", "expired_entries", "body_bytes"}
_ENVELOPE_OVERHEAD = 65536
_CACHE_MAX_ENTRIES = 128
_CACHE_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class CommandResult:
    """Runner output. Truncation means the process output cap was reached.

    Stderr is deliberately absent: it can contain private URLs or credentials.
    A nonzero return code is sufficient to classify unavailable relay results.
    """

    returncode: int
    stdout: bytes
    truncated: bool = False


class CommandRunner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, env: Mapping[str, str | None], max_bytes: int, timeout: float
    ) -> CommandResult:
        """Execute argv with capped output. None environment values remove keys."""
        ...


class SubprocessRunner:
    """Optional live runner using Brigade's bounded subprocess primitive.

    Callers must put native gh and octopool executables on PATH, without a gh
    shim. Environment overrides apply only to the child. Stderr is discarded.
    Construction makes no requests or authentication changes.
    """

    def __call__(
        self, argv: Sequence[str], *, env: Mapping[str, str | None], max_bytes: int, timeout: float
    ) -> CommandResult:
        from brigade.dot.process import CommandRunner as BoundedRunner

        if type(max_bytes) is not int or not 0 < max_bytes <= 32 * 1024 * 1024 + _ENVELOPE_OVERHEAD:
            raise ValueError("positive bounded output cap required")
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("positive bounded timeout required")
        child_env = dict(os.environ)
        for key, value in env.items():
            if value is None:
                child_env.pop(key, None)
            else:
                child_env[key] = value
        output = BoundedRunner().run(list(argv), env=child_env, timeout=timeout, max_output=max_bytes)
        if output.timed_out:
            return CommandResult(124, b"")
        return CommandResult(output.returncode if output.returncode is not None else 1, output.stdout, output.overflow)


@dataclass(frozen=True)
class ReadResult(GetResponse):
    """Importer-compatible response with explicit admission and routing state.

    A validated 304 is resolved to a 200 body for existing importers. Its actual
    HTTP status remains in upstream_status and the local ledger.
    """

    state: str = "ok"
    route: str = "gh"
    upstream_status: int | None = None
    retry_at: float | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


def _integer(value: object) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value):
        return int(value)
    return None


def _headers(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key.lower(): item
        for key, item in value.items()
        if isinstance(key, str)
        and key.lower() in _HEADERS
        and isinstance(item, str)
        and len(item) <= 8192
        and not any(c in item for c in "\r\n\x00")
    }


def _stats_summary(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in _STATS and type(item) in (int, float) and math.isfinite(item) and item >= 0:
            result[key] = item
        elif key in {"totals", "summary", "cache", "eligible", "pool_usage"} and isinstance(item, dict):
            # Fixed containers only. Never preserve caller/client/identity keys.
            allowed = _STATS | _CACHE_STATS if key == "cache" else _STATS
            counters = {
                name: number
                for name, number in item.items()
                if name in allowed and type(number) in (int, float) and math.isfinite(number) and number >= 0
            }
            if counters:
                result[key] = counters
    return result


def _http(output: bytes) -> tuple[int, dict[str, str], bytes]:
    separator = b"\r\n\r\n" if b"\r\n\r\n" in output else b"\n\n"
    head, found, body = output.partition(separator)
    if not found or len(head) > _ENVELOPE_OVERHEAD:
        raise ValueError("missing HTTP headers")
    lines = head.decode("ascii").splitlines()
    match = re.fullmatch(r"HTTP/[0-9.]+ ([1-5][0-9]{2})(?: .*)?", lines[0])
    if match is None:
        raise ValueError("missing HTTP status")
    headers = {}
    for line in lines[1:]:
        key, colon, value = line.partition(":")
        if not colon:
            raise ValueError("malformed HTTP headers")
        headers[key.lower()] = value.strip()
    return int(match[1]), _headers(headers), body


def _octopool(output: bytes) -> tuple[int, dict[str, str], bytes, bool]:
    # Response envelope and encodings: https://docs.octopool.dev/relay.html#response-envelope
    envelope = json.loads(output)
    status = envelope.get("status")
    if type(status) is not int or not 100 <= status <= 599:
        raise ValueError("missing relay status")
    body = envelope.get("body")
    encoding = envelope.get("body_encoding")
    if encoding == "json":
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    elif encoding == "text" and (body is None or isinstance(body, str)):
        data = (body or "").encode()
    elif encoding == "base64" and isinstance(body, str):
        data = base64.b64decode(body, validate=True)
    else:
        raise ValueError("unsupported relay body encoding")
    relay = envelope.get("relay", {})
    if not isinstance(relay, dict):
        raise ValueError("invalid relay metadata")
    inconclusive = bool(relay.get("stale_ok") or relay.get("fallback_local") or envelope.get("fallback_local"))
    return status, _headers(envelope.get("headers")), data, inconclusive


class GitHubReader:
    """GET-only transport bound to one independently classified repository.

    Private and unknown repositories never invoke Octopool, including probes or
    stats. Public fallback is opt-in since importers prohibit identity fallback.
    Credential classes are nonsecret labels, never token values or user names.
    All readers sharing a credential should use the same ledger and cap policy.
    """

    def __init__(
        self,
        repository: str,
        *,
        visibility: Literal["public", "private", "unknown"],
        runner: CommandRunner,
        ledger: BudgetLedger,
        credential_class: str = "user",
        hourly_caps: Mapping[str, int] | None = None,
        allow_public_fallback: bool = False,
        clock: Callable[[], float] = time.time,
        timeout: float = 30,
        cache: OrderedDict[str, tuple[str, bytes]] | None = None,
    ):
        if not _REPO.fullmatch(repository) or repository.split("/")[1] in {".", ".."}:
            raise ValueError("canonical owner/repository required")
        if visibility not in {"public", "private", "unknown"}:
            raise ValueError("repository visibility required")
        if not _CLASS.fullmatch(credential_class) or credential_class == "octopool":
            raise ValueError("nonsecret credential class required")
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("positive bounded timeout required")
        self.caps = {credential_class: 5000, "octopool": 5000}
        for key, cap in (hourly_caps or {}).items():
            if not _CLASS.fullmatch(key) or type(cap) is not int or not 0 <= cap <= 100000:
                raise ValueError("bounded hourly caps and nonsecret class labels required")
            self.caps[key] = cap
        self.repository = repository
        self.visibility = visibility
        self.runner = runner
        self.ledger = ledger
        self.credential_class = credential_class
        self.allow_public_fallback = allow_public_fallback
        self.clock = clock
        self.timeout = timeout
        self._health: tuple[float, bool] | None = None
        self._cache: OrderedDict[str, tuple[str, bytes]] = cache if cache is not None else OrderedDict()
        self._stats: dict[str, Any] | None = None

    def _run(self, argv: list[str], cap: int, fresh: bool = False) -> CommandResult:
        result = self.runner(
            argv,
            env={"OCTOPOOL_NO_FALLBACK": "1", "OCTOPOOL_FRESH": "1" if fresh else None},
            max_bytes=cap,
            timeout=self.timeout,
        )
        if len(result.stdout) > cap:
            return CommandResult(result.returncode, result.stdout[:cap], True)
        return result

    def _healthy(self, now: float) -> bool:
        if self._health is None or not 0 <= now - self._health[0] < 600:
            try:
                result = self._run(["octopool", "whoami"], _ENVELOPE_OVERHEAD)
                healthy = result.returncode == 0 and not result.truncated
            except Exception:
                healthy = False
            self._health = now, healthy
        return self._health[1]

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, str | int],
        max_bytes: int,
        max_age: int = 120,
        merge_gating_reason: str | None = None,
    ) -> ReadResult:
        """Read one page. Pagination and its completion budgets stay with callers.

        Rejected, deferred and exhausted calls do not issue an API request. No
        hidden retries or sleeps are performed. Inspect state and retry_at.
        """
        fresh = merge_gating_reason is not None
        if fresh and (
            not isinstance(merge_gating_reason, str)
            or not merge_gating_reason.strip()
            or len(merge_gating_reason) > 200
        ):
            raise ValueError("name the merge-gating reason")
        if type(max_age) is not int or not 0 <= max_age <= 86400 or (max_age == 0 and not fresh):
            raise ValueError("max_age zero requires a merge-gating reason")
        if type(max_bytes) is not int or not 0 < max_bytes <= 16 * 1024 * 1024:
            raise ValueError("positive bounded byte cap required")
        prefix = f"/repos/{self.repository}"
        if (
            not _PATH.fullmatch(path)
            or re.search(r"%(?![A-Fa-f0-9]{2})", path)
            or not (path == prefix or path.startswith(prefix + "/"))
        ):
            raise ValueError("GET path must stay inside the bound repository")
        # Send the original path, with canonical escaping within each segment.
        # Literal percent is legal, but a second decode must not change routing
        # or introduce traversal, controls, backslashes or another escape layer.
        decoded = unquote(path, errors="strict")
        canonical = "/".join(quote(unquote(part, errors="strict"), safe="") for part in path.split("/"))
        escaped = re.sub(r"%[A-Fa-f0-9]{2}", lambda match: match[0].upper(), path)
        # Replacement decoding lets literal percent text such as %beta survive
        # invalid UTF-8 bytes while still exposing ASCII/UTF-8 routing hazards.
        nested = unquote(decoded, errors="replace")
        if (
            canonical != escaped
            or "%25" in decoded.lower()
            or not decoded.isprintable()
            or not nested.isprintable()
            or "\\" in decoded + nested
            or any(decoded.count(c) != nested.count(c) for c in "/?#")
            or any(part in {"", ".", ".."} for value in (decoded, nested) for part in value[1:].split("/"))
        ):
            raise ValueError("GET path must stay inside the bound repository")
        query: dict[str, str | int] = {}
        if len(params) > 32:
            raise ValueError("too many query parameters")
        for key, value in params.items():
            if (
                not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,63}", key)
                or _SECRET.search(key)
                or type(value) not in (str, int)
                or len(str(value)) > 1024
                or any(c in str(value) for c in "\r\n\x00")
            ):
                raise ValueError("bounded nonsecret GET parameters required")
            query[key] = value
        endpoint = path + ("?" + urlencode(sorted(query.items())) if query else "")
        endpoint_key = hashlib.sha256(endpoint.encode()).hexdigest()
        queue_acquired = False
        try:
            with self.ledger.serialized(timeout=3 * self.timeout + 10) as conn:
                queue_acquired = True
                now = self.clock()
                # Admission precedes the health probe. An exhausted relay allowance
                # must not spend a probe or silently move to native credentials.
                unhealthy = self._health is not None and 0 <= now - self._health[0] < 600 and not self._health[1]
                route = (
                    "octopool"
                    if self.visibility == "public" and not (unhealthy and self.allow_public_fallback)
                    else "gh"
                )
                credential = "octopool" if route == "octopool" else self.credential_class
                if self.ledger.admission(conn, credential, route, endpoint_key, now, self.caps[credential]):
                    return self._read(conn, path, query, endpoint, endpoint_key, route, max_bytes, max_age, fresh)
                route = "octopool" if self.visibility == "public" and self._healthy(now) else "gh"
                fallback = self.visibility == "public" and route == "gh"
                if fallback and not self.allow_public_fallback:
                    self.ledger.record(
                        conn,
                        now=now,
                        credential="octopool",
                        route="octopool",
                        status=None,
                        state="inconclusive",
                        attempted=False,
                    )
                    return ReadResult(502, b"", state="inconclusive", route="octopool")
                result = self._read(
                    conn, path, query, endpoint, endpoint_key, route, max_bytes, max_age, fresh, fallback=fallback
                )
                if route == "octopool" and result.state == "inconclusive" and self.allow_public_fallback:
                    return self._read(
                        conn, path, query, endpoint, endpoint_key, "gh", max_bytes, max_age, fresh, fallback=True
                    )
                return result
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            busy = code in {5, 6} if code is not None else "locked" in str(exc).lower()
            if queue_acquired or not busy:
                raise
            # The writer owns the ledger, so no deferred row can be recorded.
            return ReadResult(503, b"", state="queue-busy", route="none")

    def _read(
        self,
        conn: sqlite3.Connection,
        path: str,
        params: Mapping[str, str | int],
        endpoint: str,
        key: str,
        route: str,
        cap: int,
        max_age: int,
        fresh: bool,
        *,
        fallback: bool = False,
    ) -> ReadResult:
        credential = "octopool" if route == "octopool" else self.credential_class
        now = self.clock()
        blocked = self.ledger.admission(conn, credential, route, key, now, self.caps[credential])
        if blocked:
            state, retry_at = blocked
            self.ledger.record(
                conn, now=now, credential=credential, route=route, status=None, state=state, attempted=False
            )
            return ReadResult(
                503 if state == "budget-exhausted" else 429, b"", state=state, route=route, retry_at=retry_at
            )
        age = 0 if fresh else max_age
        if route == "octopool":
            # CLI docs, "octopool request": https://docs.octopool.dev/cli.html
            # The raw request command prints the full relay envelope, unlike gh.
            argv = [
                "octopool",
                "request",
                "--method",
                "GET",
                "--path",
                path,
                "--header",
                f"cache-control=max-age={age}",
            ]
            for name, value in sorted(params.items()):
                argv += ["--query", f"{name}={value}"]
        else:
            argv = [
                "gh",
                "api",
                "--hostname",
                "github.com",
                "--method",
                "GET",
                "--include",
                endpoint,
                "--header",
                f"Cache-Control: max-age={age}",
            ]
            if endpoint in self._cache:
                argv += ["--header", f"If-None-Match: {self._cache[endpoint][0]}"]
        status: int | None = None
        headers: dict[str, str] = {}
        data = b""
        truncated = False
        state = "ok"
        try:
            output = self._run(argv, cap * 2 + _ENVELOPE_OVERHEAD, fresh)
            truncated = output.truncated
            if route == "octopool":
                if output.returncode != 0 or truncated:
                    self._health = self.clock(), False
                    state = "inconclusive"
                else:
                    status, headers, data, uncertain = _octopool(output.stdout)
                    if status in {401, 403, 404, 424} or uncertain:
                        state = "inconclusive"
            else:
                status, headers, data = _http(output.stdout)
            if status is not None and state == "ok":
                state = {403: "access-denied", 404: "unavailable", 429: "rate-limited"}.get(status, "ok")
                if status == 403 and ("retry-after" in headers or _integer(headers.get("x-ratelimit-remaining")) == 0):
                    state = "rate-limited"
                if status not in {200, 304, 403, 404, 429}:
                    state = "http-unavailable"
        except Exception:
            if route == "octopool":
                self._health = self.clock(), False
            state = "inconclusive" if route == "octopool" else "transport-unavailable"
        etag_hit = status == 304 and route == "gh" and endpoint in self._cache
        if status == 304:
            if etag_hit:
                data = self._cache[endpoint][1]
                self._cache.move_to_end(endpoint)
                state = "not-modified"
            else:
                state = "inconclusive"
        if route == "gh" and status != 304:
            self._cache.pop(endpoint, None)
            if status == 200 and state == "ok" and not truncated and len(data) <= cap and headers.get("etag"):
                self._cache[endpoint] = headers["etag"], data
                while (
                    len(self._cache) > _CACHE_MAX_ENTRIES
                    or sum(len(body) for _, body in self._cache.values()) > _CACHE_MAX_BYTES
                ):
                    self._cache.popitem(last=False)
        remaining = _integer(headers.get("x-ratelimit-remaining"))
        reset = _integer(headers.get("x-ratelimit-reset"))
        self._holds(conn, credential, route, key, headers, remaining, reset)
        self.ledger.record(
            conn,
            now=self.clock(),
            credential=credential,
            route=route,
            status=status,
            state=state,
            etag_hit=etag_hit,
            remaining=remaining,
            reset=reset,
            fallback=fallback,
        )
        if state in {"inconclusive", "transport-unavailable"}:
            return ReadResult(
                502 if state == "inconclusive" else 503,
                b"",
                truncated,
                state=state,
                route=route,
                upstream_status=status,
            )
        return ReadResult(
            200 if etag_hit else 429 if state == "rate-limited" else status or 503,
            data[:cap],
            truncated or len(data) > cap,
            state=state,
            route=route,
            upstream_status=status,
            headers=headers,
        )

    def _holds(
        self,
        conn: sqlite3.Connection,
        credential: str,
        route: str,
        key: str,
        headers: Mapping[str, str],
        remaining: int | None,
        reset: int | None,
    ) -> None:
        now = self.clock()
        poll = _integer(headers.get("x-poll-interval"))
        if poll is not None and poll > 0:
            self.ledger.hold(conn, credential, route, key, now + poll, "deferred")
        retry = headers.get("retry-after")
        if retry is not None:
            seconds = _integer(retry)
            try:
                until = now + seconds if seconds is not None else parsedate_to_datetime(retry).timestamp()
            except (ValueError, TypeError, OverflowError):
                until = now
            if until > now:
                self.ledger.hold(conn, credential, route, "", until, "deferred")
        if remaining == 0 and reset is not None and reset > now:
            self.ledger.hold(conn, credential, route, "", reset, "budget-exhausted")

    def observation(self, *, refresh_octopool_stats: bool = False) -> dict[str, Any]:
        """Return a serializable node observation fragment, without publishing it.

        Optional stats are filtered to numeric counters. Caller identities,
        private server URLs and raw diagnostics never enter this envelope.
        """
        with self.ledger.serialized() as conn:
            now = self.clock()
            if refresh_octopool_stats and self.visibility == "public" and self._healthy(now):
                self._stats = None
                try:
                    output = self._run(["octopool", "stats", "--json"], _ENVELOPE_OVERHEAD)
                    if output.returncode == 0 and not output.truncated:
                        self._stats = _stats_summary(json.loads(output.stdout))
                except Exception:
                    pass
            return {"github_reads": self.ledger.summary(conn, now), "octopool_stats": self._stats}
