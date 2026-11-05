#!/usr/bin/env python3
"""S6 acceptance on disposable S6a targets, with every client attempt retained."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import load
import matrix


def background(url, stop, output, claim_token, admin_token):
    """Exercise a cold Python write beside the native durable claims lane."""
    records = []
    client = load.Client(url, admin_token, "close", 10)
    claim_client = load.Client(url, claim_token, "close", 10)
    try:
        while not stop.is_set():
            for method, path, body in (
                ("PUT", "/preference", {"impl": "test-worker"}),
                (
                    "POST",
                    "/claims",
                    {
                        "action": "acquire",
                        "target": "bench-target",
                        "node_id": "bench-claims",
                        "holder": "bench-holder",
                        "ttl_seconds": 900,
                    },
                ),
            ):
                started = time.monotonic()
                try:
                    status, raw, _ = (claim_client if path == "/claims" else client).request(
                        method, path, json.dumps(body).encode()
                    )
                    records.append(
                        {
                            "path": path,
                            "status": status,
                            "latency_ms": (time.monotonic() - started) * 1000,
                            "error": raw.decode("utf-8") if status != 200 else None,
                        }
                    )
                except (OSError, load.http.client.HTTPException) as exc:
                    records.append({"path": path, "status": None, "error": type(exc).__name__})
            stop.wait(0.02)
    finally:
        client.close()
        claim_client.close()
        output.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")


def commit_p99(first, last):
    """Bucket upper bound from every COMMIT in a window, never gauge samples."""
    prefix = 'brigade_hub_writer_commit_duration_seconds_bucket{le="'
    buckets = []
    for name, count in last.items():
        if name.startswith(prefix):
            bound = float(name[len(prefix) :].split('"', 1)[0])
            buckets.append((bound, count - first.get(name, 0)))
    buckets.sort()
    if not buckets or buckets[-1][1] <= 0:
        return None
    rank = buckets[-1][1] * 0.99
    for bound, count in buckets:
        if count >= rank:
            return bound * 1000 if bound != float("inf") else "above 30000 ms"
    return None


def metrics(url, token):
    client = load.Client(url, token, "close", 10)
    try:
        status, raw, _ = client.request("GET", "/metrics", None)
        return load.parse_metrics(raw) if status == 200 else {}
    finally:
        client.close()


def drive(url, db, env, output, target, args, clients):
    """Several S6a clients avoid a single Python GIL limiting the load source."""
    first_metrics = metrics(url, (env.get("BRIGADE_BENCH_METRICS_TOKEN") or env.get("BRIGADE_FLEET_TOKEN", "")))
    initial = load.db_info(str(db))
    processes = min(args.processes, clients)
    children = []
    for number in range(processes):
        path = output / f"client-{number}.json"
        command = [
            sys.executable,
            str(matrix.HERE / "load.py"),
            "--url",
            url,
            "--output",
            str(path),
            "--target",
            target,
            "--allotment",
            args.allotment,
            "--mode",
            "closed",
            "--rate",
            "1000000000",
            "--clients",
            str(clients // processes + (number < clients % processes)),
            "--seconds",
            str(args.seconds),
            "--warmup",
            str(args.warmup),
            "--timeout",
            "10",
            "--connection",
            args.connection,
            "--sample-interval",
            ".1",
            "--database",
            str(db),
            "--flags=--allow-admin-writes"
            + (
                " --route-mode registration=native --route-mode heartbeat=native --route-mode claims=native --route-mode messages=native"
                if target == "rust-native"
                else ""
            ),
        ]
        if getattr(args, "client_cpus", None):
            command = ["taskset", "-c", args.client_cpus, *command]
        children.append((subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL), path))
    reports = []
    for child, path in children:
        if child.wait() != 0:
            raise RuntimeError("load child failed")
        reports.append(json.loads(path.read_text(encoding="utf-8")))
    attempts = [r for report in reports for r in report["attempts"] if r["phase"] == "measure"]
    window = args.warmup + args.seconds
    ok = [r for r in attempts if r["status"] == 200 and r["error"] is None and r["completed_s"] <= window]
    samples = [s for report in reports for s in report["telemetry"]["samples"]]
    sampled_metrics = {}
    for name in ("writer_lock_wait_seconds", "writer_commit_seconds", "writer_batch_size", "writer_queue_depth"):
        values = [s["value"].get("brigade_hub_" + name) for s in samples if isinstance(s.get("value"), dict)]
        sampled_metrics[name] = load.percentiles(
            [v * 1000 if name.endswith("seconds") else v for v in values if isinstance(v, (int, float))]
        )
    result = {
        "target": target,
        "clients": clients,
        "server_cpus": getattr(args, "server_cpus", None),
        "client_cpus": getattr(args, "client_cpus", None),
        "allotment": args.allotment,
        "processes": processes,
        "seconds": args.seconds,
        "warmup": args.warmup,
        "connection": args.connection,
        "initial_database": initial,
        "cgroup_limits": json.loads((Path(db).parent / "limits.json").read_text(encoding="utf-8"))
        if db is not None and (Path(db).parent / "limits.json").exists()
        else None,
        "ok_per_second": len(ok) / args.seconds,
        "errors": sum(r["error"] is not None for r in attempts),
        "transport_errors": sum(r["status"] is None for r in attempts),
        "refused_connections": sum(r["error"] == "ConnectionRefusedError" for r in attempts),
        "latency_ms": load.percentiles([r["latency_ms"] for r in attempts if r["latency_ms"] is not None]),
        "sampled_writer_metrics": sampled_metrics,
        "commit_p99_bucket_upper_ms": commit_p99(
            first_metrics, metrics(url, (env.get("BRIGADE_BENCH_METRICS_TOKEN") or env.get("BRIGADE_FLEET_TOKEN", "")))
        ),
        "host_load": os.getloadavg(),
        "client_reports": [path.name for _, path in children],
    }
    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--seconds", type=float, default=10)
    p.add_argument("--warmup", type=float, default=2)
    p.add_argument("--clients", type=int, nargs="+", default=[16, 64, 256])
    p.add_argument("--processes", type=int, default=4)
    p.add_argument("--repetitions", type=int, default=3)
    p.add_argument("--connection", choices=("close", "keep-alive"), default="close")
    p.add_argument("--allotment", choices=("reference", "1cpu-512m"), default="reference")
    p.add_argument("--server-cpus", help="Server CPU affinity, inherited by the managed upstream")
    p.add_argument("--client-cpus", help="Client CPU affinity outside the server allocation")
    p.add_argument("--mixed", action="store_true")
    args = p.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for clients in args.clients:
        for repetition in range(1, args.repetitions + 1):
            for target in ("python", "rust-native"):
                output = args.output / f"{target}-{clients}-{repetition}"
                output.mkdir()
                with tempfile.TemporaryDirectory(prefix="s6-bench-") as work:
                    with matrix.server(
                        target, args.binary.resolve(), args.allotment == "1cpu-512m", Path(work), args.server_cpus
                    ) as (
                        url,
                        db,
                        env,
                    ):
                        admin_token = env["BRIGADE_FLEET_TOKEN"]
                        tokens = {}
                        admin_client = load.Client(url, admin_token, "close", 10)
                        try:
                            for node in ("bench-node", "bench-claims"):
                                status, raw, _ = admin_client.request(
                                    "POST", "/nodes", json.dumps({"action": "add", "node_id": node}).encode()
                                )
                                if status != 200:
                                    raise RuntimeError("benchmark enrollment failed")
                                tokens[node] = json.loads(raw)["token"]
                        finally:
                            admin_client.close()
                        env["BRIGADE_BENCH_TOKEN"] = tokens["bench-node"]
                        env["BRIGADE_BENCH_METRICS_TOKEN"] = admin_token
                        stop = threading.Event()
                        mixed = None
                        if args.mixed:
                            mixed = threading.Thread(
                                target=background,
                                args=(url, stop, output / "mixed.json", tokens["bench-claims"], admin_token),
                            )
                            mixed.start()
                        try:
                            results.append(drive(url, db, env, output, target, args, clients))
                        finally:
                            stop.set()
                            if mixed:
                                mixed.join()
                print(json.dumps(results[-1]), flush=True)
    (args.output / "summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
