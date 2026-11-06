"""Pull embedding jobs without opening the hub memory database."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import ipaddress
import json
import math
import struct
import sys
import time
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import memory_hub_client as hubc
from .memory_content_scan import blocked_rule


def register(sub: argparse._SubParsersAction) -> None:
    worker = sub.add_parser("embed-worker", help="Pull hub embedding leases and embed on a local server.")
    worker.add_argument("--target", "-t", type=Path, default=Path("."))
    worker.add_argument("--config", type=Path, required=True, help="Local hub.toml with [memory.embedder].")
    worker.add_argument("--once", action="store_true", help="Process at most one lease and exit.")
    reindex = sub.add_parser("reindex", help="Queue missing vectors or drop exactly one embedder's derived rows.")
    reindex.add_argument("--target", "-t", type=Path, default=Path("."))
    reindex.add_argument("--drop", metavar="EMBEDDER_ID")
    reindex.add_argument("--admin-token-file", type=Path, required=True)


class EndpointError(ValueError):
    """The local endpoint failed. The hub releases the job without counting an attempt."""

    def __init__(self, message: str, reason: str = "embedder-unavailable") -> None:
        super().__init__(message)
        self.reason = reason


# Mirrors the hub's TRANSIENT list: reasons that release a job without counting an attempt.
TRANSIENT_REASONS = frozenset(
    {
        "embedder-busy",
        "embedder-unavailable",
        "embedder-timeout",
        "embedder-refused",
        "endpoint-refused",
        "scan-unavailable",
        "scan-pending",
        "digest-mismatch",
    }
)
BACKOFF_START = 1.0
BACKOFF_MAX = 60.0


class DocumentRefused(ValueError):
    """This text was refused. The hub retries it alone, backs off, then parks it."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _required(config: dict, key: str, kind: type) -> Any:
    # Mirrors the hub's Config::parse: these keys have no default there either.
    value = config.get(key)
    if kind is int:
        valid = isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 1_000_000
    else:
        valid = isinstance(value, str) and bool(value)
    if not valid:
        raise ValueError(f"memory.embedder.{key} is required")
    return value


def identity(config: dict) -> str:
    fields = [
        _required(config, "name", str),
        _required(config, "model_digest", str),
        _required(config, "dimensions", int),
        1,
        _required(config, "query_prefix_version", int),
    ]
    return hashlib.sha256(json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def secret(path: Path) -> str:
    with path.open("r", encoding="utf-8") as stream:
        stat = stream.fileno()
        import os
        import stat as stat_module

        info = os.fstat(stat)
        if (
            not stat_module.S_ISREG(info.st_mode)
            or info.st_size > 8192
            or (os.name != "nt" and info.st_mode & 0o777 != 0o600)
        ):
            raise ValueError("credential file must be a regular mode-0600 file")
        token = stream.read(8193).strip()
    if not token or any(ord(c) < 32 or ord(c) > 126 for c in token):
        raise ValueError("invalid credential file")
    return token


def local_embed(config: dict, texts: list[str], token: str) -> list[list[float]]:
    endpoint = config["endpoint"]
    url = urlsplit(endpoint)
    try:
        local = ipaddress.ip_address(url.hostname or "").is_loopback
    except ValueError:
        local = False
    # Pull mode executes on the model host. Only its numeric loopback listener is admitted.
    if not local or endpoint not in config["allowlist"] or url.username or url.password or url.query or url.fragment:
        raise ValueError("embed-worker requires an allowlisted loopback endpoint")
    if url.scheme not in {"http", "https"} or config["shape"] not in {"api/embed", "v1/embeddings"}:
        raise ValueError("invalid embedding transport")
    if not texts or len(texts) > 4 or sum(len(t.encode()) for t in texts) > 32768:
        raise DocumentRefused("embedding payload refused", "embedder-batch-invalid")
    if blocked_rule(texts, blocking_only=True):
        raise DocumentRefused("embedding payload refused", "content-blocked")
    payload = json.dumps({"model": config["name"], "input": texts}).encode()
    connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    conn = connection_type(url.hostname, url.port, timeout=30)
    deadline = time.monotonic() + 30

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("embedding deadline exceeded")
        return seconds

    try:
        return _exchange(conn, url.path or "/", payload, token, config, len(texts), remaining)
    except (DocumentRefused, EndpointError):
        raise
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as exc:
        raise EndpointError("embedding endpoint unavailable") from exc
    finally:
        conn.close()


def _exchange(conn: Any, path: str, payload: bytes, token: str, config: dict, count: int, remaining: Any) -> list:
    conn.connect()
    assert conn.sock is not None
    conn.sock.settimeout(remaining())
    conn.request("POST", path, payload, {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    conn.sock.settimeout(remaining())
    response = conn.getresponse()
    if response.status in {400, 413, 422}:
        raise DocumentRefused("embedding endpoint refused", "embedder-rejected")
    if response.status != 200:
        raise EndpointError(
            "embedding endpoint refused",
            "embedder-refused" if response.status in {401, 403} else "embedder-unavailable",
        )
    limit = config["dimensions"] * count * 32 + 8193
    chunks = []
    size = 0
    while size < limit:
        remaining_seconds = remaining()
        if conn.sock is not None:
            conn.sock.settimeout(remaining_seconds)
        chunk = response.read1(min(65536, limit - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    body = b"".join(chunks)
    if len(body) > config["dimensions"] * count * 32 + 8192:
        raise EndpointError("embedding response too large")
    try:
        return _parse_reply(body, response.getheader("x-model-digest"), config, count)
    except (DocumentRefused, EndpointError):
        raise
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        # A body of the wrong shape or type says the endpoint is wrong, not one
        # document. A JSON integer too large for a float is the same kind of
        # fault. None of them may escape the step and stop the worker.
        raise EndpointError("malformed embedding reply") from exc


def _parse_reply(body: bytes, header: str | None, config: dict, count: int) -> list:
    data = json.loads(body)
    digest = data.get("model_digest", header)
    if digest != config["model_digest"] or (header is not None and header != digest):
        raise EndpointError("digest-mismatch", "digest-mismatch")
    # A reply whose rows do not match the inputs one for one is refused as
    # vector-invalid, as the hub does. That reason is not transient, so a
    # document that keeps getting such replies backs off and parks.
    if config["shape"] == "api/embed":
        rows = data["embeddings"]
    else:
        indexed = data["data"]
        if sorted(r["index"] for r in indexed) != list(range(count)):
            raise DocumentRefused("invalid vector order", "vector-invalid")
        rows = [r["embedding"] for r in sorted(indexed, key=lambda r: r["index"])]
    if len(rows) != count:
        raise DocumentRefused("invalid vector count", "vector-invalid")
    result = []
    for row in rows:
        if len(row) != config["dimensions"] or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in row):
            raise DocumentRefused("invalid vector", "vector-invalid")
        # hypot does not overflow where squaring a large float does.
        norm = math.hypot(*row)
        if not math.isfinite(norm) or not norm:
            raise DocumentRefused("invalid vector norm", "vector-invalid")
        result.append([x / norm for x in row])
    return result


def _embed_each(config: dict, jobs: list[dict], token: str) -> tuple[list[tuple[dict, list[float]]], list[dict]]:
    """Embed the batch, or each document alone after a document refusal."""
    try:
        return list(zip(jobs, local_embed(config, [job["text"] for job in jobs], token), strict=True)), []
    except EndpointError as exc:
        return [], [{"content_hash": job["content_hash"], "reason": exc.reason} for job in jobs]
    except DocumentRefused as exc:
        if len(jobs) == 1:
            # `alone` tells the hub this document was already tried by itself.
            return [], [{"content_hash": jobs[0]["content_hash"], "reason": exc.reason, "alone": True}]
    stored: list[tuple[dict, list[float]]] = []
    failures: list[dict] = []
    for job in jobs:
        rows, failed = _embed_each(config, [job], token)
        stored += rows
        failures += failed
    return stored, failures


def step(client: hubc.MemoryHubClient, config: dict, token: str) -> tuple[int, int, bool]:
    """Lease once. Returns (documents leased, vectors stored, endpoint fault).

    The last item is true when nothing was stored and every failure was an
    endpoint or hub fault rather than a refusal of one document.
    """
    lease = client.call("embed_lease", embedder_id=identity(config), max=4, client_event_id=hubc.event_id())
    jobs = lease.get("jobs", [])
    if not jobs:
        return 0, 0, False
    if lease.get("model_digest") != config["model_digest"] or lease.get("dimensions") != config["dimensions"]:
        raise ValueError("lease identity mismatch")
    # Only clean receipts may leave the host. Anything else goes back unsent. A
    # pending scan is transient, while a flagged or blocked receipt is reported
    # as content-blocked so the hub parks it instead of releasing it forever.
    failures = [
        {
            "content_hash": job["content_hash"],
            "reason": "scan-pending" if job.get("scan_state") in (None, "pending") else "content-blocked",
        }
        for job in jobs
        if job.get("scan_state") != "clean"
    ]
    clean = [job for job in jobs if job.get("scan_state") == "clean"]
    stored, refused = _embed_each(config, clean, token) if clean else ([], [])
    failures += refused
    if stored:
        vectors = [
            {
                "content_hash": job["content_hash"],
                "vector": base64.b64encode(struct.pack(f"<{len(row)}f", *row)).decode(),
            }
            for job, row in stored
        ]
        client.call(
            "embed_submit",
            lease_id=lease["lease_id"],
            model_digest=config["model_digest"],
            vectors=vectors,
            client_event_id=hubc.event_id(),
        )
    if failures:
        for failure in failures:
            print(f"document {failure['content_hash'][:12]} not embedded: {failure['reason']}", file=sys.stderr)
        client.call("embed_failed", lease_id=lease["lease_id"], failures=failures, client_event_id=hubc.event_id())
    down = bool(failures) and not stored and all(f["reason"] in TRANSIENT_REASONS for f in failures)
    return len(jobs), len(stored), down


def serve(
    client: hubc.MemoryHubClient, config: dict, token: str, *, once: bool = False, sleep: Any = time.sleep
) -> int:
    """Lease until stopped. Per-document failures are reported and the loop goes on.

    While the endpoint is down the loop sleeps 1 s, doubling to 60 s, so a long
    queue is not leased and released in a tight loop. A good step resets it.
    """
    delay = BACKOFF_START
    while True:
        try:
            leased, stored, down = step(client, config, token)
        except hubc.HubUnavailable:
            if once:
                raise
            print("hub unavailable, retrying", file=sys.stderr, flush=True)
            sleep(5)
            continue
        except hubc.HubRefusal as exc:
            # An expired lease is retried. Any other refusal stops the worker.
            if once or exc.status != 409:
                raise
            continue
        print(f"embedded {stored} of {leased} document(s)", flush=True)
        if once:
            return 0
        if down:
            sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX)
            continue
        delay = BACKOFF_START
        # Otherwise sleep only when there was nothing to lease.
        if not leased:
            sleep(1)


def dispatch(args: argparse.Namespace) -> int:
    try:
        settings = hubc.load_settings(args.target.expanduser().resolve())
        if not settings.enabled:
            raise ValueError("hub memory is off")
        if args.memory_command == "reindex":
            client = hubc.MemoryHubClient(settings.hub_url, secret(args.admin_token_file.expanduser()))
            fields: dict[str, Any] = {"client_event_id": hubc.event_id()}
            if args.drop:
                fields["drop"] = args.drop
            print(json.dumps(client.call("reindex", **fields)))
            return 0
        path = args.config.expanduser().resolve()
        with path.open("rb") as stream:
            config = tomllib.load(stream)["memory"]["embedder"]
        token_path = Path(config["token_file"]).expanduser()
        token = secret(token_path if token_path.is_absolute() else path.parent / token_path)
        client = hubc.client_for(settings)
        return serve(client, config, token, once=args.once)
    except KeyboardInterrupt:
        return 0
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        http.client.HTTPException,
        hubc.HubConfigError,
        hubc.HubUnavailable,
        hubc.HubRefusal,
    ):
        print("error: embedding operation refused or unavailable", file=sys.stderr)
        return 1
