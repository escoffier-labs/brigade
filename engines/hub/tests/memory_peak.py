"""Record the front and managed upstream peak under a delegated cgroup v2."""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from transport import HOST, front, request


class MemoryPeak(unittest.TestCase):
    def test_credential_body_share_peak(self):
        parent = Path(os.environ.get("BRIGADE_TEST_CGROUP_PARENT", "/sys/fs/cgroup"))
        if not (parent / "cgroup.controllers").exists():
            self.skipTest("S3c cgroup peak: cgroup v2 is unavailable")
        try:
            group = Path(tempfile.mkdtemp(prefix="brigade-hub-test-", dir=parent))
        except OSError as exc:
            self.skipTest(f"S3c cgroup peak: no writable delegated cgroup v2 parent: {exc.strerror}")
        if not (group / "memory.peak").exists():
            group.rmdir()
            self.skipTest("S3c cgroup peak: memory controller is not delegated")
        try:
            # The test launcher moves only its own child before exec. Descendants
            # inherit this group, including the managed upstream.
            original = __import__("subprocess").Popen

            def launch(*args, **kwargs):
                def join():
                    fd = os.open(group / "cgroup.procs", os.O_WRONLY)
                    os.write(fd, str(os.getpid()).encode())
                    os.close(fd)

                kwargs["preexec_fn"] = join
                return original(*args, **kwargs)

            with patch("transport.subprocess.Popen", launch), front() as (child, port), contextlib.ExitStack() as stack:
                db = child.args[child.args.index("--db") + 1]
                tokens = [f"fake-node-{i}" for i in range(4)]
                with sqlite3.connect(db) as conn:
                    for i, token in enumerate(tokens):
                        conn.execute(
                            "INSERT INTO nodes(node_id,token_sha256,created_at) VALUES (?,?,?)",
                            (f"fake-node-{i}", hashlib.sha256(token.encode()).hexdigest(), "2026-01-01T00:00:00Z"),
                        )
                for i in range(8):
                    length = (8 if i < 7 else 5) * 1024 * 1024
                    sock = stack.enter_context(socket.create_connection((HOST, port), timeout=5))
                    sock.sendall(
                        (
                            f"POST /events HTTP/1.1\r\nHost: example.invalid\r\nAuthorization: Bearer {tokens[i // 2]}\r\nContent-Length: {length}\r\n\r\n"
                        ).encode()
                    )
                    sock.sendall(b"x" * (length - 1))
                time.sleep(0.2)
                self.assertEqual(request(port, "/health")[0], 200)
                peak = int((group / "memory.peak").read_text())
                print(
                    json.dumps(
                        {
                            "acceptance": "S3c credential body share full",
                            "reserved_body_bytes": 61 * 1024 * 1024,
                            "cgroup_memory_peak_bytes": peak,
                            "includes": "Rust front and Python upstream",
                        }
                    )
                )
                self.assertGreater(peak, 61 * 1024 * 1024)
        finally:
            for _ in range(100):
                try:
                    group.rmdir()
                    break
                except OSError:
                    time.sleep(0.02)


if __name__ == "__main__":
    unittest.main(verbosity=2)
