"""Repeatable snapshot continuation measurement, run as an explicit pytest selector."""

from __future__ import annotations

import cProfile
import io
import json
import platform
import pstats
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from brigade import fleet_hub, fleet_hub_paging as paging


def test_measure_snapshot_walks():
    count = 2000
    data = {"claims": [{"owner_node": "reader", "target": f"item-{i:05d}", "note": "x" * 4096} for i in range(count)]}
    with tempfile.TemporaryDirectory() as root:
        conn = fleet_hub.init_db(Path(root) / "fleet.db")
        fleet_hub.add_node(conn, "reader")
        raw = {"action": "claims", "limit": 1}
        first = paging.page(conn, raw, data, caller="reader", limit=1, visible=lambda *_: True, snapshot_write=True)
        conn.commit()
        decoded = []
        loads = json.loads

        def measured_loads(value, *args, **kwargs):
            decoded.append(len(value.encode() if isinstance(value, str) else value))
            return loads(value, *args, **kwargs)

        def continuation(cursor, limit):
            request = {**raw, "cursor": cursor}
            snapshot, offsets, sid = paging.load(conn, request, caller="reader")
            return paging.snapshot_page(conn, snapshot, offsets, sid, limit=limit, snapshot_write=True)

        for limit in (1, 200):
            samples = []
            for _ in range(5):
                conn.execute("DELETE FROM fleet_read_snapshots")
                first = paging.page(
                    conn, raw, data, caller="reader", limit=1, visible=lambda *_: True, snapshot_write=True
                )
                cursor = first["next_cursor"]
                started = time.perf_counter()
                pages = 0
                while cursor:
                    result = continuation(cursor, limit)
                    cursor = result["next_cursor"]
                    pages += 1
                samples.append(time.perf_counter() - started)
            first = paging.page(conn, raw, data, caller="reader", limit=1, visible=lambda *_: True, snapshot_write=True)
            decoded.clear()
            with patch.object(json, "loads", measured_loads):
                result = continuation(first["next_cursor"], limit)
            print(
                json.dumps(
                    {
                        "limit": limit,
                        "rows": count,
                        "payload_bytes": len(json.dumps(data, separators=(",", ":")).encode()),
                        "walk_pages": pages,
                        "seconds": samples,
                        "median": statistics.median(samples),
                        "decoded_bytes_one_continuation": sum(decoded),
                        "decoded_documents": len(decoded),
                        "returned_rows": len(result["claims"]),
                        "python": platform.python_version(),
                    }
                )
            )
        profiler = cProfile.Profile()
        profiler.runcall(continuation, first["next_cursor"], 1)
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(12)
        print(stream.getvalue())
        conn.close()
