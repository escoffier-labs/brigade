"""Exclusive MCP stdio server for live paired skill trials.

The server exposes exactly the five paired tools backed by the existing
``Boundary``. Every call, including rejected and non-allowlisted ones, is written
to a server-side JSONL trace that the live adapter treats as authoritative.
Run it as ``python -m brigade model trial paired-mcp``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .mcp_server import serve_stdio
from .model_trial_paired import TOOLS, Boundary, collect_package

SERVER_NAME = "brigade-paired"
TRACE_FILE = "trace.jsonl"
RECEIPT_FILE = "receipt.json"
DESCRIPTIONS = {
    "brief": "Read the evaluator's work brief for the fresh target. Takes no arguments.",
    "verify": "Run the evaluator's fixed verification command once. Takes no arguments.",
    "receipt": "Read the verification receipt. Takes no arguments.",
    "capture": "Capture the outcome for the verification run. Takes no arguments.",
    "scoreability": "Read the scoreability notice for the receipt. Takes no arguments.",
}
# The adapter blanks the token in the server's MCP config env, so the server starts without it.
# This scrub is a second line: the Boundary's fixture subprocesses must never inherit a token.
_SCRUB_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_OAUTH")


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": tool,
            "description": DESCRIPTIONS[tool],
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        }
        for tool in TOOLS
    ]


class PairedServer:
    """Boundary calls plus an append-only authoritative trace in ``state_dir``."""

    def __init__(self, boundary: Boundary, state_dir: Path) -> None:
        self.boundary = boundary
        self.state_dir = state_dir
        self.seq = 0
        self._receipt_written = False

    def _append(self, row: dict[str, Any]) -> None:
        with (self.state_dir / TRACE_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[object, bool]:
        self.seq += 1
        row: dict[str, Any] = {"seq": self.seq, "tool": name, "arguments": arguments, "allowlisted": name in TOOLS}
        try:
            response = self.boundary.call(name, arguments)
        except Exception as exc:  # Fixture failures are evidence; the adapter fails the cell.
            response = {"error": f"apparatus_error:{type(exc).__name__}"}
            row["apparatus_error"] = True
        row["response"] = response
        self._append(row)
        if self.boundary.receipt_bytes is not None and not self._receipt_written:
            (self.state_dir / RECEIPT_FILE).write_bytes(self.boundary.receipt_bytes)
            self._receipt_written = True
        return json.dumps(response, sort_keys=True), "error" in response


def scrub_environment() -> None:
    for key in [k for k in os.environ if k.startswith(_SCRUB_PREFIXES)]:
        del os.environ[key]


def serve(case: str, target: Path, package_dir: Path, state_dir: Path) -> int:
    scrub_environment()
    files = collect_package(package_dir)
    server = PairedServer(Boundary(target, case, files), state_dir)
    return serve_stdio(
        server_name=SERVER_NAME,
        list_resources=lambda: [],
        read_resource=lambda uri: (None, None),
        list_tools=tool_definitions,
        call_tool=server.call_tool,
    )
