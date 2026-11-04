#!/usr/bin/env python3
"""Run the writer overload test with its front and upstream in a 512 MiB scope."""
import json
import os
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[3]
build = subprocess.run(
    ["cargo", "test", "--locked", "--manifest-path", str(root / "engines/hub/Cargo.toml"),
     "--test", "writer_front", "--no-run", "--message-format=json"],
    text=True, stdout=subprocess.PIPE, check=True,
)
executables = [
    event["executable"] for line in build.stdout.splitlines()
    if (event := json.loads(line)).get("reason") == "compiler-artifact"
    and event.get("executable") and event["target"]["name"] == "writer_front"
]
assert len(executables) == 1, executables
# Compile outside the scope, then meter the test and both managed processes.
environment = os.environ.copy()
environment["PYTHONPATH"] = str(root / "src")
raise SystemExit(subprocess.run(
    ["systemd-run", "--user", "--scope", "--quiet", "-p", "MemoryMax=512M",
     "-p", "MemorySwapMax=0", "env", f"PYTHONPATH={root / 'src'}", executables[0],
     "--ignored", "--exact", "overload_includes_managed_upstream_cgroup", "--nocapture"],
    env=environment,
).returncode)
