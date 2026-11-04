#!/usr/bin/env python3
"""Reproducible public synthetic CLI benchmark, two runs per size/backend.

``--smoke`` is the CI mode: a few KiB to a few hundred KiB, one run per
size/backend, a hard wall-clock cap, and no timing thresholds. It proves the
benchmark still builds and runs and that the three backends agree, and it
records the numbers as JSON lines for upload as an artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from brigade.guard.native import native_spans
from brigade.guard.rules import DEFAULT_RULES

ROOT = Path(__file__).resolve().parent.parent
GUARD_BIN = ROOT / "engines/brigade-guard/target/release/brigade-guard"
SMOKE_TIME_LIMIT_SECONDS = 90.0


def engine_version() -> dict[str, str]:
    """The linked PCRE2 and Unicode versions, from a one-rule UTF request."""
    request = json.dumps(
        {"protocol": 1, "mode": "utf", "text": "x", "rules": [{"id": "x", "pattern": "x", "flags": 0}]}
    )
    result = subprocess.run([str(GUARD_BIN)], input=request.encode(), capture_output=True, check=True, timeout=30)
    return json.loads(result.stdout)["engine"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--smoke", action="store_true", help="tiny inputs, one run each, hard time cap")
    parser.add_argument("--output", type=Path, help="also write every record as JSON lines to this file")
    parser.add_argument(
        "--time-limit",
        type=float,
        default=SMOKE_TIME_LIMIT_SECONDS,
        help="smoke only: total wall-clock seconds before the benchmark fails (default %(default)s)",
    )
    args = parser.parse_args()
    deadline = time.monotonic() + args.time_limit if args.smoke else None
    records: list[dict[str, object]] = []

    def emit(record: dict[str, object]) -> None:
        records.append(record)
        print(json.dumps(record), flush=True)

    os.environ["BRIGADE_GUARD_BIN"] = str(GUARD_BIN)
    os.environ["BRIGADE_GUARD_BACKEND"] = "native"
    emit(
        {
            "kind": "meta",
            "smoke": args.smoke,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "engine": engine_version(),
        }
    )
    # Only public repository fixtures are used. Add deterministic fictional
    # examples for rule families the fixtures do not contain.
    fixtures = sorted((ROOT / "tests/guard/fixtures/real_world").glob("*.md"))
    samples = "\n".join(p.read_text(encoding="utf-8") for p in fixtures)
    samples += "\n" + "\n".join(
        [
            "+1 555 123 4567",
            "person@example.com",
            "user@" + "10.0.0.1",  # content-guard: allow private-ipv4
            "127.0.0.1",
            "localhost:12345",
            "localhost",
            "port 12345",
            "Co-authored-by: Example <person@example.com>",
            '"C:/Users/example/file.txt"',  # content-guard: allow windows-home-path
            "/home/example/file.txt",  # content-guard: allow home-path
            "/root/example",  # content-guard: allow root-path
            "phone: 555 234 5678",
            "eyJabcdefgh.abcdefgh.abcdefgh",  # content-guard: allow jwt-token
            "Bearer " + "a" * 24,
            'api_key = "' + "a" * 24 + '"',
            "ghp_" + "a" * 24,
            "-----BEGIN PRIVATE KEY-----\nexample\n-----END PRIVATE KEY-----",  # content-guard: allow private-key-block
            "host: example.internal",  # content-guard: allow private-hostname
        ]
    )
    samples = samples.encode("ascii", "replace").decode()
    assert all(re.search(rule.pattern, samples, rule.flags) for rule in DEFAULT_RULES)
    assert native_spans(samples, list(DEFAULT_RULES)) is not None
    # Plain words from public repo text, stripped of syntax and identifiers,
    # keep the large corpus sparse so policy overlap processing is not quadratic.
    words = re.sub("[^A-Za-z \\n]", " ", (ROOT / "README.md").read_text(encoding="utf-8"))
    size = 256 * 1024 if args.smoke else 21 * 1024 * 1024
    large = (words * (size // len(words) + 1))[:size] + "\n" + samples
    corpora = [("small", "A public example.\n"), ("64KiB", "a " * 32768), ("medium" if args.smoke else "large", large)]
    runs = (1,) if args.smoke else (1, 2)
    try:
        with tempfile.TemporaryDirectory() as directory:
            for name, text in corpora:
                path = Path(directory) / f"{name}.txt"
                path.write_text(text, encoding="utf-8")
                outputs = []
                for mode in ("python", "native", "auto"):
                    for run in runs:
                        env = dict(os.environ, BRIGADE_GUARD_BACKEND=mode)
                        # A hang fails the smoke run; a slow run never does.
                        timeout = None if deadline is None else max(deadline - time.monotonic(), 1.0)
                        start = time.perf_counter()
                        result = subprocess.run(
                            [sys.executable, "-m", "brigade", "guard", "scan", str(path), "--json"],
                            env=env,
                            capture_output=True,
                            check=False,
                            timeout=timeout,
                        )
                        elapsed = time.perf_counter() - start
                        assert result.returncode in (0, 1), result.stderr.decode()
                        assert not result.stderr, result.stderr.decode()
                        outputs.append(result.stdout)
                        emit(
                            {
                                "size": name,
                                "bytes": path.stat().st_size,
                                "backend": mode,
                                "run": run,
                                "seconds": round(elapsed, 6),
                                "MiB_per_second": round(path.stat().st_size / 1048576 / elapsed, 4),
                            }
                        )
                assert all(output == outputs[0] for output in outputs), "CLI output drift"
    finally:
        # A failed run still records the numbers it got.
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


if __name__ == "__main__":
    main()
