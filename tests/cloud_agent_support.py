"""Shared Access runtime document for connector tests."""

from pathlib import Path
from brigade.dot.runtime_config import CLOUD_AGENT_RUNTIME_SCHEMA
from tests.test_dot_runtime import _document


def _memory_document() -> dict[str, object]:
    return {
        "schema": CLOUD_AGENT_RUNTIME_SCHEMA,
        "auth": {
            "issuer": "https://team.example.test",
            "team_domain": "team.example.test",
            "audience": "c" * 64,
            "approved_email": _document()["auth"]["approved_email"],
            "resource": "https://cloud-agent.example.test/mcp",
        },
        "paths": {
            "state_dir": Path("/srv/cloud-agent/state").absolute().as_posix(),
            "workspace": Path("/srv/cloud-agent/workspace").absolute().as_posix(),
            "bin": {
                "brigade": Path("/usr/bin/brigade").absolute().as_posix(),
                "git": Path("/usr/bin/git").absolute().as_posix(),
                "gh": Path("/usr/bin/gh").absolute().as_posix(),
            },
        },
        "hub": {
            "principal": "agent:cloud-agent",
            "node_id": "00000000-0000-4000-8000-000000000004",
            "message_token_file": Path("/srv/cloud-agent/secrets/message-token").absolute().as_posix(),
            "poll_seconds": 30,
            "recipients": {},
        },
    }
