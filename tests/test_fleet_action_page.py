"""Selectable repair prompts retain evidence without changing operator submission."""

import html
import re

import pytest

from brigade import fleet_action_page as page
from tests.test_fleet_action_http import action_hub, request_http
from urllib.parse import urlencode
import json

HOSTILE = '</textarea><script>bad("&")</script>'


def snapshot(kind="run", *, optional=False):
    source = {"kind": kind, "node_id": "fixture-node"}
    if kind == "run":
        source["run_id"] = "run-1"
    record = {"state": HOSTILE, "evidence_at": "2000-01-01T00:00:00Z", "ttl_seconds": 60}
    if optional:
        record.update(version=3, repo_identity=HOSTILE)
    return {"source": source, "revision": "fixture-revision", "evidence": record}


@pytest.mark.parametrize("kind, optional", [("run", False), ("run", True), ("telemetry", True)])
def test_read_only_draft_contains_visible_evidence_rows_before_instruction(kind, optional):
    subject = "run: fixture-node/run-1" if kind == "run" else "telemetry: fixture-node"
    expected = [f"Subject: {subject}", f"Observed state: {HOSTILE}", "Evidence time: 2000-01-01T00:00:00Z"]
    if kind == "telemetry":
        expected.append("Freshness: Stale telemetry")
    if optional:
        expected += ["Work version: 3", f"Repository: {HOSTILE}"]
    rendered = page.compose(snapshot(kind, optional=optional), [], nonce="copy-nonce", csrf=None)
    match = re.search(r'<textarea id="draft-prompt" readonly>(.*?)</textarea>', rendered, re.S)
    assert match
    assert html.unescape(match.group(1)) == "\n".join(expected) + "\n\n" + page.DEFAULT_PROMPT
    assert page.evidence_text(snapshot(kind, optional=optional)) == "\n".join(expected)
    assert 'type="button" class="copy-prompt" data-copy-target="draft-prompt">Copy prompt</button>' in rendered
    assert HOSTILE not in rendered and rendered.count("<textarea") == 1
    assert "data-copy-prefix=" not in rendered
    rows = re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>", rendered, re.S)
    assert [html.unescape(key + ": " + value) for key, value in rows] == expected


def test_operator_copy_adds_escaped_evidence_prefix_and_keeps_editable_prompt_unchanged():
    target = {"family": "openclaw", "target_id": "repair", "label": "Fixture", "models": []}
    rendered = page.compose(
        snapshot(optional=True), [target], nonce="copy-nonce", csrf="fixture", values={"prompt": HOSTILE}
    )
    match = re.search(r'data-copy-target="repair-prompt" data-copy-prefix="([^"]*)"', rendered)
    assert match and html.unescape(match.group(1)) == page.evidence_text(snapshot(optional=True))
    textarea = re.search(r'<textarea id="repair-prompt" name="prompt"[^>]*>(.*?)</textarea>', rendered, re.S)
    assert textarea and html.unescape(textarea.group(1)) == HOSTILE
    assert HOSTILE not in rendered and rendered.count("<textarea") == 1
    script = re.search(r'<script nonce="copy-nonce">(.*?)</script>', rendered, re.S)
    assert script
    for marker in (
        "navigator.clipboard.writeText",
        ".select()",
        "Copied",
        "2000",
        "addEventListener",
        "textarea.value",
        "execCommand('copy')",
        "copy-fallback",
    ):
        assert marker in script.group(1)
    assert not re.search(r"\son\w+=", rendered)
    assert ".action-page .copy-prompt{" in rendered and "min-height:0" in rendered
    assert "fetch(" not in script.group(1)


def test_copy_script_carries_the_http_response_nonce(tmp_path):
    with action_hub(tmp_path) as (address, _, _, request):
        status, headers, rendered = request_http(
            address, "/deck/actions/compose?" + urlencode({"source": json.dumps(request["source"])})
        )
        assert status == 200
        nonce = re.search(r'<script nonce="([^"]+)">', rendered).group(1)
        assert "'nonce-" + nonce + "'" in headers["Content-Security-Policy"]
        assert "navigator.clipboard.writeText" in rendered
        assert "script-src-attr 'none'" in headers["Content-Security-Policy"]


def test_status_page_returns_to_the_alerts_page():
    action = {
        "action_id": "action-1",
        "status": "finished",
        "verification": "pending",
        "delivery": "delivered",
        "destination_ref": None,
        "request": {"target": {"capability": "inbox"}, "prompt": "fixture", "evidence": {}},
        "events": [],
    }
    rendered = page.status(action, "status-nonce")
    assert '<a href="/deck/alerts">Back to alerts</a>' in rendered
