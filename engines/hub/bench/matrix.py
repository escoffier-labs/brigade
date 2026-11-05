#!/usr/bin/env python3
"""Launch disposable S6a targets; never use an operator database."""

from __future__ import annotations

import argparse
import http.client
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

from load import Client, HOST, db_info

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def check_cgroup():
    try:
        relative = next(
            line.split("::", 1)[1]
            for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
            if "::" in line
        )
        group = Path("/sys/fs/cgroup") / relative.lstrip("/")
        quota, period = (group / "cpu.max").read_text(encoding="utf-8").split()
        memory = (group / "memory.max").read_text(encoding="utf-8").strip()
        if quota == "max" or int(quota) / int(period) > 1 or memory == "max" or int(memory) > 512 * 1024**2:
            raise ValueError("limits were not applied")
    except (OSError, ValueError, StopIteration) as exc:
        raise RuntimeError("cgroup delegation unavailable: require CPUQuota=100% and MemoryMax=512M") from exc
    return {"cpu_max": [quota, period], "memory_max": memory}


def scoped(command, constrained):
    if not constrained:
        return command
    if not shutil.which("systemd-run"):
        raise RuntimeError("cgroup delegation unavailable: systemd-run not found")
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "-p",
        "CPUQuota=100%",
        "-p",
        "MemoryMax=512M",
        sys.executable,
        str(HERE / "matrix.py"),
        "--guard",
        *command,
    ]


@contextlib.contextmanager
def server(target, binary, constrained, directory, server_cpus=None):
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        port = sock.getsockname()[1]
    db = directory / "hub.db"
    command = [sys.executable, "-m", "brigade", "fleet", "serve"] if target == "python" else [str(binary)]
    command += [
        "--host",
        HOST,
        "--port",
        str(port),
        "--db",
        str(db),
        "--allow-admin-writes",
    ]
    if target == "rust-native":
        for family in ("registration", "heartbeat", "claims", "messages"):
            command += ["--route-mode", family + "=native"]
    if server_cpus:
        command = ["taskset", "-c", server_cpus, *command]
    env = dict(
        os.environ,
        BRIGADE_FLEET_TOKEN="bench-disposable-token",
        BRIGADE_BENCH_TOKEN="bench-disposable-token",
        BRIGADE_HUB_PYTHON=sys.executable,
        PYTHONPATH=str(ROOT / "src"),
        BRIGADE_BENCH_LIMIT_REPORT=str(directory / "limits.json"),
    )
    # Do not inherit live hub settings or upstream credentials.
    for key in list(env):
        if key.startswith("BRIGADE_HUB_") and key != "BRIGADE_HUB_PYTHON":
            del env[key]
        if key.startswith("BRIGADE_FLEET_") and key != "BRIGADE_FLEET_TOKEN":
            del env[key]
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(
            scoped(command, constrained),
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        client = Client(f"http://{HOST}:{port}", "", "close", 1)
        try:
            for _ in range(150):
                if proc.poll() is not None:
                    raise RuntimeError(
                        "target startup failed; cgroup delegation may be unavailable"
                        if constrained
                        else "target startup failed"
                    )
                try:
                    if client.request("GET", "/health", None)[0] == 200:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("target startup deadline exceeded")
            yield f"http://{HOST}:{port}", db, env
        finally:
            client.close()
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--guard":
        limits = check_cgroup()
        Path(os.environ["BRIGADE_BENCH_LIMIT_REPORT"]).write_text(json.dumps(limits), encoding="utf-8")
        os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--binary", type=Path, default=HERE.parent / "target/debug/brigade-hub")
    p.add_argument("--seconds", type=float, default=2)
    p.add_argument("--warmup", type=float, default=1)
    p.add_argument("--rate", type=float, default=2)
    p.add_argument("--clients", type=int, default=1)
    p.add_argument("--allotment", choices=("reference", "1cpu-512m", "both"), default="both")
    p.add_argument("--connection", choices=("close", "keep-alive"), default="close")
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    reports = []
    for allotment in ("reference", "1cpu-512m") if args.allotment == "both" else (args.allotment,):
        for target in ("python", "rust-proxy"):
            for mode in ("closed", "open"):
                for repetition in range(1, 4):
                    name = f"{allotment}-{target}-{mode}-{repetition}.json"
                    with tempfile.TemporaryDirectory(prefix="hub-bench-") as work:
                        with server(
                            target,
                            args.binary.resolve(),
                            allotment == "1cpu-512m",
                            Path(work),
                        ) as (url, db, env):
                            initial_database = db_info(str(db))
                            subprocess.run(
                                [
                                    sys.executable,
                                    str(HERE / "load.py"),
                                    "--url",
                                    url,
                                    "--output",
                                    str(args.output.resolve() / name),
                                    "--target",
                                    target,
                                    "--allotment",
                                    allotment,
                                    "--mode",
                                    mode,
                                    "--seconds",
                                    str(args.seconds),
                                    "--warmup",
                                    str(args.warmup),
                                    "--rate",
                                    str(args.rate),
                                    "--clients",
                                    str(args.clients),
                                    "--connection",
                                    args.connection,
                                    "--database",
                                    str(db),
                                    # One token: argparse rejects a separate value that starts with "--".
                                    "--flags=--allow-admin-writes"
                                    + ("; managed upstream" if target == "rust-proxy" else ""),
                                ],
                                env=env,
                                check=True,
                            )
                            report_path = args.output.resolve() / name
                            report = json.loads(report_path.read_text(encoding="utf-8"))
                            report["conditions"]["initial_database"] = initial_database
                            report["conditions"]["source_revision"] = subprocess.check_output(
                                ["git", "rev-parse", "HEAD"],
                                cwd=ROOT,
                                text=True,
                                encoding="utf-8",
                            ).strip()
                            limits_file = Path(work) / "limits.json"
                            report["conditions"]["server_limits"] = (
                                json.loads(limits_file.read_text(encoding="utf-8"))
                                if limits_file.exists()
                                else {"status": "unconstrained reference"}
                            )
                            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                    reports.append(name)
                    print(name, flush=True)
    (args.output / "matrix.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provisional": True,
                "reports": reports,
                "tls_run": {
                    "status": "not run: operator endpoint and credential required",
                    "gated": False,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
