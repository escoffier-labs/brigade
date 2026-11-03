#!/usr/bin/env python3
"""Reproducible public synthetic CLI benchmark, two runs per size/backend."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from brigade.guard.native import native_spans
from brigade.guard.rules import DEFAULT_RULES

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    os.environ["BRIGADE_GUARD_BIN"] = str(ROOT / "engines/brigade-guard/target/release/brigade-guard")
    os.environ["BRIGADE_GUARD_BACKEND"] = "native"
    # Only public repository fixtures are used. Add deterministic fictional
    # examples for rule families the fixtures do not contain.
    samples = "\n".join(p.read_text() for p in (ROOT / "tests/guard/fixtures/real_world").glob("*.md"))
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
    words = re.sub("[^A-Za-z \\n]", " ", (ROOT / "README.md").read_text())
    size = 21 * 1024 * 1024
    large = (words * (size // len(words) + 1))[:size] + "\n" + samples
    with tempfile.TemporaryDirectory() as directory:
        for name, text in [("small", "A public example.\n"), ("64KiB", "a " * 32768), ("large", large)]:
            path = Path(directory) / f"{name}.txt"
            path.write_text(text)
            outputs = []
            for mode in ("python", "native", "auto"):
                for run in (1, 2):
                    env = dict(os.environ, BRIGADE_GUARD_BACKEND=mode)
                    start = time.perf_counter()
                    result = subprocess.run(
                        [sys.executable, "-m", "brigade", "guard", "scan", str(path), "--json"],
                        env=env,
                        capture_output=True,
                        check=False,
                    )
                    elapsed = time.perf_counter() - start
                    assert result.returncode in (0, 1), result.stderr.decode()
                    assert not result.stderr, result.stderr.decode()
                    outputs.append(result.stdout)
                    print(
                        json.dumps(
                            {
                                "size": name,
                                "bytes": path.stat().st_size,
                                "backend": mode,
                                "run": run,
                                "seconds": round(elapsed, 6),
                                "MiB_per_second": round(path.stat().st_size / 1048576 / elapsed, 4),
                            }
                        ),
                        flush=True,
                    )
            assert all(output == outputs[0] for output in outputs), "CLI output drift"


if __name__ == "__main__":
    main()
