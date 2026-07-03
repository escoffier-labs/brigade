"""Opt-in canary against the real `codex app-server` binary.

Run with: BRIGADE_CODEX_INTEGRATION=1 python -m pytest tests/test_codex_appserver_integration.py
Detects protocol drift in the experimental app-server API without spending tokens.
"""

from __future__ import annotations

import os
import shutil

import pytest

from brigade import codex_appserver

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None or os.environ.get("BRIGADE_CODEX_INTEGRATION") != "1",
    reason="needs codex binary and BRIGADE_CODEX_INTEGRATION=1",
)


def test_real_handshake_and_thread_start(tmp_path):
    with codex_appserver.AppServer() as server:
        thread = server.start_thread(cwd=tmp_path, sandbox="read-only")
        assert thread.thread_id
