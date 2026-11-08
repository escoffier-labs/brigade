#!/usr/bin/env python3
"""Conductor-run S6 deployment observation. Does not manage the hub or its host."""

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import load
from heartbeat import commit_p99, drive, metrics


def burn():
    while True:
        sum(range(10000))


def request(url, token, body):
    client = load.Client(url, token, "close", 25)
    try:
        status, raw, _ = client.request("POST", "/messages", json.dumps(body).encode())
        return status, json.loads(raw)
    finally:
        client.close()


def probes(url, token, recipient_token, stop, records):
    after = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        while not stop.is_set():
            wake = pool.submit(request, url, recipient_token, {"action": "list", "after": after, "wait_ms": 2000})
            time.sleep(0.1)
            started = time.monotonic()
            sent_status, sent = request(
                url,
                token,
                {
                    "action": "send",
                    "to": "bench-receiver",
                    "subject": "S6 deployment probe",
                    "body": "synthetic probe",
                    "client_id": "s6-" + str(time.time_ns()),
                },
            )
            status, answer = wake.result()
            messages = answer.get("messages", [])
            matched = sent.get("message", {}).get("id")
            records.append(
                {
                    "kind": "wake",
                    "send_status": sent_status,
                    "status": status,
                    "latency_ms": (time.monotonic() - started) * 1000,
                    "delivered": any(m["id"] == matched for m in messages),
                }
            )
            if messages:
                after = max(m["id"] for m in messages)
            client = load.Client(url, token, "close", 10)
            try:
                started = time.monotonic()
                status, raw, _ = client.request(
                    "POST",
                    "/claims",
                    json.dumps(
                        {
                            "action": "acquire",
                            "target": "s6-deployment-probe",
                            "node_id": "bench-node",
                            "holder": "s6-deployment-holder",
                            "ttl_seconds": 900,
                        }
                    ).encode(),
                )
                records.append(
                    {
                        "kind": "claim",
                        "status": status,
                        "latency_ms": (time.monotonic() - started) * 1000,
                        "error": raw.decode("utf-8") if status != 200 else None,
                    }
                )
            finally:
                client.close()
            stop.wait(0.1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--fanout-cpus", required=True, help="Allowed non-hub CPUs for synthetic workers")
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--workers", type=int, default=170)
    p.add_argument("--load-deadline", type=float, default=600)
    p.add_argument("--load-min", type=float, default=150)
    p.add_argument("--load-max", type=float, default=190)
    p.add_argument(
        "--batch-events",
        type=int,
        default=1,
        help="Unique events per heartbeat POST, 1 to 1000. The default 1 is the S6 single-event write target",
    )
    p.add_argument(
        "--nodes",
        type=int,
        default=1,
        help="Heartbeat nodes recorded in the summary. The default 1 matches bench-node, which signs every post",
    )
    args = p.parse_args()
    if not 1 <= args.batch_events <= 1000:
        p.error("batch-events must be between 1 and 1000")
    if args.nodes < 1:
        p.error("nodes must be at least 1")
    token = os.environ["BRIGADE_BENCH_TOKEN"]
    recipient_token = os.environ["BRIGADE_BENCH_RECEIVER_TOKEN"]
    # Require the telemetry credential before starting the synthetic fan-out.
    os.environ["BRIGADE_BENCH_METRICS_TOKEN"]
    args.output.mkdir(parents=True, exist_ok=False)
    first_metrics = {}
    last_metrics = {}
    burners = []
    stop = threading.Event()
    records = []
    probe = None
    event_summary = None
    try:
        for _ in range(args.workers):
            burners.append(
                subprocess.Popen(
                    ["taskset", "-c", args.fanout_cpus, sys.executable, str(Path(__file__).resolve()), "--burn"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        deadline = time.monotonic() + args.load_deadline
        while not args.load_min <= os.getloadavg()[0] <= args.load_max:
            if time.monotonic() >= deadline:
                raise RuntimeError("synthetic load did not reach the requested range")
            time.sleep(1)
        first_metrics = metrics(args.url, os.environ["BRIGADE_BENCH_METRICS_TOKEN"])

        def probe_job():
            try:
                probes(args.url, token, recipient_token, stop, records)
            except Exception as exc:
                records.append({"kind": "probe-error", "status": None, "error": type(exc).__name__})

        probe = threading.Thread(target=probe_job)
        probe.start()
        args.processes = 4
        args.warmup = 5
        args.connection = "close"
        args.allotment = "reference"
        event_summary = drive(args.url, None, dict(os.environ), args.output, "rust-native", args, 64)

    finally:
        stop.set()
        if probe:
            probe.join(timeout=30)
        for worker in burners:
            worker.terminate()
        for worker in burners:
            worker.wait()
        last_metrics = metrics(args.url, os.environ["BRIGADE_BENCH_METRICS_TOKEN"])
        (args.output / "probes.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
        summary = {
            "events": event_summary,
            "claim_ms": load.percentiles([r["latency_ms"] for r in records if r["kind"] == "claim"]),
            "wake_ms": load.percentiles([r["latency_ms"] for r in records if r["kind"] == "wake"]),
            "errors": [r for r in records if r["status"] != 200 or r.get("delivered") is False],
            "end_load": os.getloadavg(),
            "commit_p99_bucket_upper_ms": commit_p99(first_metrics, last_metrics),
            "qualification": "COMMIT p99 is a histogram bucket upper bound, failures included. Window includes warmup and concurrent probes.",
        }
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1:] == ["--burn"]:
        burn()
    else:
        main()
