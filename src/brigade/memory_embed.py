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


def identity(config: dict) -> str:
    fields = [config["name"], config["model_digest"], config["dimensions"], 1, config["query_prefix_version"]]
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
    if (
        not texts
        or len(texts) > 4
        or sum(len(t.encode()) for t in texts) > 32768
        or blocked_rule(texts, blocking_only=True)
    ):
        raise ValueError("embedding payload refused")
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
        conn.connect()
        assert conn.sock is not None
        conn.sock.settimeout(remaining())
        conn.request(
            "POST", url.path or "/", payload, {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        conn.sock.settimeout(remaining())
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError("embedding endpoint refused")
        limit = config["dimensions"] * len(texts) * 32 + 8193
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
        if len(body) > config["dimensions"] * len(texts) * 32 + 8192:
            raise ValueError("embedding response too large")
        data = json.loads(body)
        digest = data.get("model_digest", response.getheader("x-model-digest"))
        header = response.getheader("x-model-digest")
        if digest != config["model_digest"] or (header is not None and header != digest):
            raise ValueError("digest-mismatch")
        if config["shape"] == "api/embed":
            rows = data["embeddings"]
        else:
            indexed = data["data"]
            if sorted(r["index"] for r in indexed) != list(range(len(texts))):
                raise ValueError("invalid vector order")
            rows = [r["embedding"] for r in sorted(indexed, key=lambda r: r["index"])]
        if len(rows) != len(texts):
            raise ValueError("invalid vector count")
        result = []
        for row in rows:
            if len(row) != config["dimensions"] or not all(
                isinstance(x, (int, float)) and math.isfinite(x) for x in row
            ):
                raise ValueError("invalid vector")
            norm = math.sqrt(sum(float(x) ** 2 for x in row))
            if not math.isfinite(norm) or not norm:
                raise ValueError("invalid vector norm")
            result.append([x / norm for x in row])
        return result
    finally:
        conn.close()


def step(client: hubc.MemoryHubClient, config: dict, token: str) -> int:
    lease = client.call("embed_lease", embedder_id=identity(config), max=4, client_event_id=hubc.event_id())
    jobs = lease.get("jobs", [])
    if not jobs:
        return 0
    if lease.get("model_digest") != config["model_digest"] or lease.get("dimensions") != config["dimensions"]:
        raise ValueError("lease identity mismatch")
    if any(job.get("scan_state") not in {"clean", "flagged"} for job in jobs):
        raise ValueError("pending document refused")
    rows = local_embed(config, [job["text"] for job in jobs], token)
    vectors = [
        {"content_hash": job["content_hash"], "vector": base64.b64encode(struct.pack(f"<{len(row)}f", *row)).decode()}
        for job, row in zip(jobs, rows, strict=True)
    ]
    client.call(
        "embed_submit",
        lease_id=lease["lease_id"],
        model_digest=config["model_digest"],
        vectors=vectors,
        client_event_id=hubc.event_id(),
    )
    return len(rows)


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
        while True:
            count = step(client, config, token)
            print(f"embedded {count} document(s)", flush=True)
            if args.once:
                return 0
            time.sleep(1)
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
