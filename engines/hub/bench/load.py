#!/usr/bin/env python3
"""Development-only S6a load driver. No retries and no runtime dependencies."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import platform
import queue
import statistics
import threading
import time
from urllib.parse import urlsplit

UNAVAILABLE = "not available until S4a/S9"
HOST = str(ipaddress.IPv4Address(2130706433))


def percentiles(values):
    ordered = sorted(values)
    return {
        f"p{p}": ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)] if ordered else None
        for p in (50, 90, 95, 99, 100)
    }


def parse_metrics(raw):
    """Decode a /metrics body. Metrics text maps each sample line to a float.

    A NaN or infinite value is stored as None, since it means "unavailable" and
    JSON cannot carry it. A JSON object body is kept as is.
    """
    text = raw.decode("utf-8", errors="replace")
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    samples = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, number = line.rpartition(" ")
        try:
            parsed = float(number)
        except ValueError:
            continue
        samples[name] = parsed if math.isfinite(parsed) else None
    return samples


# Short column key -> exported metric. The key drops the brigade_hub_ prefix.
COLUMNS = {
    "queue_depth": (
        "writer_queue_depth",
        "reader_queue_depth",
        "cpu_queue_depth",
    ),
    "batch_sizes": ("writer_batch_size",),
    "checkpoint_stalls": ("checkpoint_busy_total", "checkpoint_stall_frames"),
}


def telemetry_column(samples, column):
    """Summarize one column from the collected /metrics samples.

    A metric with no finite value in any sample is left out, and a column with
    none returns UNAVAILABLE. Values are the sampled readings in order, so a
    gauge such as batch size is as fine as the sample interval.
    """
    found = {}
    for key in COLUMNS[column]:
        values = []
        for sample in samples:
            value = sample["value"]
            number = value.get(f"brigade_hub_{key}") if isinstance(value, dict) else None
            if isinstance(number, (int, float)) and math.isfinite(number):
                values.append(number)
        if values:
            found[key] = {
                "metric": f"brigade_hub_{key}",
                "samples": len(values),
                "min": min(values),
                "max": max(values),
                "first": values[0],
                "last": values[-1],
                "values": values,
            }
    return found or UNAVAILABLE


def endpoint(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("expected an http(s) endpoint without credentials")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("endpoint must have no path, query or fragment")
    if parsed.scheme == "http" and parsed.hostname != HOST:
        raise ValueError("plain HTTP must use the single numeric loopback source")
    return parsed


class Client:
    def __init__(self, url, token, connection, timeout):
        self.url = endpoint(url)
        self.token, self.connection, self.timeout = token, connection, timeout
        self.conn = None

    def close(self):
        if self.conn:
            self.conn.close()
        self.conn = None

    def request(self, method, path, body):
        if self.conn is None:
            cls = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
            source = (HOST, 0) if self.url.scheme == "http" else None
            self.conn = cls(
                self.url.hostname,
                self.url.port,
                timeout=self.timeout,
                source_address=source,
            )
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.connection == "close":
            headers["Connection"] = "close"
        status = None
        try:
            self.conn.request(method, path, body, headers)
            response = self.conn.getresponse()
            status = response.status
            raw = response.read()  # A truncated body is a transport error, even after HTTP 200.
            retry_after = response.getheader("Retry-After")
            if self.connection == "close":
                self.close()
            return status, raw, retry_after
        except Exception as exc:
            exc.http_status = status
            self.close()
            raise


def run(args):
    endpoint(args.url)
    token = os.environ.get("BRIGADE_BENCH_TOKEN", "")
    # Optional {"node id": "token"} map. Worker i posts as node i modulo its size,
    # so a flood is not capped by one credential's in-flight and byte shares.
    nodes = sorted(json.loads(os.environ.get("BRIGADE_BENCH_NODE_TOKENS") or "{}").items()) or [("bench-node", token)]
    payload = Path(args.payload).read_bytes() if args.payload else None
    started = time.time()
    load_start = os.getloadavg()
    results = []
    lock = threading.Lock()
    jobs = queue.Queue(maxsize=args.backlog)
    start = time.monotonic() + 0.05
    finish = start + args.warmup + args.seconds
    run_id = f"bench-{time.time_ns()}"

    def attempt(client, node, number, due):
        body = payload
        if body is None and args.method == "GET":
            body = b""
        if body is None:
            events = [
                {
                    "node_id": node,
                    "run_id": run_id,
                    "repo": "bench-repo",
                    "seat": "worker",
                    "harness": "codex",
                    "state": "run.created",
                    "ts": "2026-01-01T00:00:00Z",
                    "sequence": sequence,
                    "digest": f"bench-{sequence}",
                }
                for sequence in range(number * args.batch_events, (number + 1) * args.batch_events)
            ]
            # One event keeps the original single-object body. Batches stay unique.
            body = json.dumps(events[0] if args.batch_events == 1 else events, sort_keys=True).encode("utf-8")
        begin = time.monotonic()
        record = {
            "attempt": number,
            "phase": "warmup" if due < start + args.warmup else "measure",
            "scheduled_s": due - start,
            "started_s": begin - start,
            "status": None,
            "reason": None,
            "error": None,
            "retry_after": None,
            "payload_bytes": len(body),
        }
        try:
            status, raw, retry = client.request(args.method, args.path, body)
            record.update(status=status, retry_after=retry)
            if status >= 400:
                try:
                    answer = json.loads(raw)
                    record["reason"] = answer.get("reason") if isinstance(answer, dict) else None
                except (ValueError, UnicodeError):
                    pass
                # Keep complete error bytes, including HTML 502/504 and responses without a reason.
                record["error"] = raw.decode("utf-8", errors="replace")
        except (OSError, http.client.HTTPException) as exc:
            # Exception text can contain endpoint names. Store the class and errno instead.
            record["status"] = getattr(exc, "http_status", None)
            record["error"] = type(exc).__name__
            record["errno"] = getattr(exc, "errno", None)
        end = time.monotonic()
        record.update(
            completed_s=end - start,
            service_ms=(end - begin) * 1000,
            latency_ms=(end - due) * 1000,
            dispatch_lag_ms=(begin - due) * 1000,
        )
        with lock:
            results.append(record)

    counter = 0

    def worker(index):
        nonlocal counter
        node, node_token = nodes[index % len(nodes)]
        client = Client(args.url, node_token, args.connection, args.timeout)
        try:
            if args.mode == "closed":
                time.sleep(max(0, start - time.monotonic()))
                while time.monotonic() < finish:
                    with lock:
                        number = counter
                        counter += 1
                    attempt(client, node, number, time.monotonic())
                    time.sleep(min(1 / args.rate, max(0, finish - time.monotonic())))
            else:
                while True:
                    job = jobs.get()
                    try:
                        if job is None:
                            return
                        attempt(client, node, *job)
                    finally:
                        jobs.task_done()
        finally:
            client.close()

    metrics = []
    stop = threading.Event()

    def sample():
        client = Client(args.url, os.environ.get("BRIGADE_BENCH_METRICS_TOKEN", token), "close", args.timeout)
        try:
            while not stop.is_set():
                try:
                    status, raw, _ = client.request("GET", "/metrics", None)
                    value = parse_metrics(raw) if status == 200 else None
                    metrics.append(
                        {
                            "elapsed_s": time.monotonic() - start,
                            "status": status,
                            "value": value,
                        }
                    )
                except (OSError, ValueError, http.client.HTTPException):
                    metrics.append(
                        {
                            "elapsed_s": time.monotonic() - start,
                            "status": None,
                            "value": None,
                        }
                    )
                stop.wait(args.sample_interval)
        finally:
            client.close()

    sampler = threading.Thread(target=sample)
    sampler.start()
    workers = [threading.Thread(target=worker, args=(index,)) for index in range(args.clients)]
    for thread in workers:
        thread.start()
    if args.mode == "open":
        # Fixed arrivals independent of response times. A full client queue is an explicit attempt.
        for number in range(math.ceil((args.warmup + args.seconds) * args.rate)):
            due = start + number / args.rate
            if due >= finish:
                break
            time.sleep(max(0, due - time.monotonic()))
            try:
                jobs.put_nowait((number, due))
            except queue.Full:
                results.append(
                    {
                        "attempt": number,
                        "phase": "warmup" if due < start + args.warmup else "measure",
                        "scheduled_s": due - start,
                        "started_s": None,
                        "completed_s": None,
                        "status": None,
                        "reason": "client-backlog-full",
                        "error": "not sent",
                        "retry_after": None,
                        "payload_bytes": None,
                        "service_ms": None,
                        "latency_ms": None,
                        "dispatch_lag_ms": None,
                    }
                )
        for _ in workers:
            jobs.put(None)
    for thread in workers:
        thread.join()
    stop.set()
    sampler.join()
    measured = [r for r in results if r["phase"] == "measure"]
    latencies = [r["latency_ms"] for r in measured if r["latency_ms"] is not None]
    errors = [r for r in measured if r["error"] is not None]
    # Open-loop workers drain queued requests after the window closes. Throughput
    # counts only completions inside the window; later ones keep their latency.
    window_end = args.warmup + args.seconds
    in_window = [r for r in measured if r["completed_s"] is not None and r["completed_s"] <= window_end]
    drained = [r["completed_s"] for r in measured if r["completed_s"] is not None]
    report = {
        "schema_version": 1,
        "provisional": True,
        "started_unix_s": started,
        "machine": {
            "role": "reference load host",
            "os": platform.system(),
            "arch": platform.machine(),
            "cpu_count": os.cpu_count(),
            "client_affinity": sorted(os.sched_getaffinity(0)),
            "memory_bytes": os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"),
        },
        "host_load_average": {"start": load_start, "end": os.getloadavg()},
        "conditions": {
            "target": args.target,
            "allotment": args.allotment,
            "transport": "operator-tls" if args.url.startswith("https:") else "single-loopback-source",
            "tls_gated": False,
            "mode": args.mode,
            "connection": args.connection,
            "seconds": args.seconds,
            "warmup_seconds": args.warmup,
            "clients": args.clients,
            "client_backlog": args.backlog,
            "offered_rate": args.rate if args.mode == "open" else None,
            "closed_think_seconds": 1 / args.rate if args.mode == "closed" else None,
            "timeout_seconds": args.timeout,
            "sample_interval_seconds": args.sample_interval,
            "method": args.method,
            "path": args.path,
            "payload": {
                "kind": "file" if payload is not None else "unique-event-v1",
                "events_per_post": None if payload is not None else args.batch_events,
                "nodes": len(nodes),
                "sha256": hashlib.sha256(payload).hexdigest() if payload is not None else None,
            },
            "database": db_info(args.database),
            "flags": args.flags,
        },
        "summary": {
            "attempts": len(measured),
            "errors": len(errors),
            "completed_per_second": len(in_window) / args.seconds,
            "completed_after_window": len(latencies) - len(in_window),
            "drain_seconds": max(0.0, max(drained) - window_end) if drained else 0.0,
            "latency_ms": percentiles(latencies),
            "mean_service_ms": statistics.mean(r["service_ms"] for r in measured if r["service_ms"] is not None)
            if latencies
            else None,
        },
        "telemetry": {
            "queue_depth": telemetry_column(metrics, "queue_depth"),
            "batch_sizes": telemetry_column(metrics, "batch_sizes"),
            "checkpoint_stalls": telemetry_column(metrics, "checkpoint_stalls"),
            "samples": metrics,
        },
        "attempts": sorted(results, key=lambda r: r["attempt"]),
    }
    return report


def db_info(path):
    if not path:
        return {"status": "not supplied", "main_bytes": None, "wal_bytes": None}
    db = Path(path)
    return {
        "status": "observed after run",
        "main_bytes": db.stat().st_size,
        "wal_bytes": Path(str(db) + "-wal").stat().st_size if Path(str(db) + "-wal").exists() else 0,
    }


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--target", choices=("python", "rust-proxy", "rust-native"), required=True)
    p.add_argument("--allotment", choices=("reference", "1cpu-512m"), default="reference")
    p.add_argument("--mode", choices=("closed", "open"), default="open")
    p.add_argument("--connection", choices=("close", "keep-alive"), default="close")
    p.add_argument("--seconds", type=float, default=2)
    p.add_argument("--warmup", type=float, default=1)
    p.add_argument("--rate", type=float, default=2)
    p.add_argument("--clients", type=int, default=1)
    p.add_argument("--backlog", type=int, default=64)
    p.add_argument("--timeout", type=float, default=5)
    p.add_argument("--sample-interval", type=float, default=1)
    p.add_argument("--path", default="/events")
    p.add_argument("--method", choices=("GET", "POST"), default="POST")
    p.add_argument("--payload")
    p.add_argument("--batch-events", type=int, default=1, help="Unique events per generated POST body, 1 to 1000")
    p.add_argument("--database")
    p.add_argument("--flags", default="unspecified")
    return p


def main():
    p = parser()
    args = p.parse_args()
    for name in ("seconds", "rate", "clients", "backlog", "timeout", "sample_interval"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            p.error(f"{name} must be finite and positive")
    if not 1 <= args.batch_events <= 1000:
        p.error("batch-events must be between 1 and 1000")
    if not math.isfinite(args.warmup) or args.warmup < 0:
        p.error("warmup must be finite and nonnegative")
    args.output.write_text(json.dumps(run(args), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
