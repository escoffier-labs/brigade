"""Additive v1 ops drive cases. Run against the Rust front, never the v1-only hub."""

from __future__ import annotations

import json
from pathlib import Path

import hub_contract as hc

RULES = json.loads(Path(__file__).with_name("hub-shadow-rules.json").read_text(encoding="utf-8"))


def drive(hub: hc.Hub) -> None:
    """Authenticate with real issued credentials, then refuse every non-admin kind."""
    assert RULES["default"] == "exact"
    assert hc.request(hub.url, "GET", "/health").json == RULES["families"]["health"]["shape"]
    admin = hub.admin_token
    node = hc.request(hub.url, "POST", "/nodes", token=admin, body={"action": "add", "node_id": "ops-contract-node"})
    assert node.status == 200, node
    principal = hc.request(
        hub.url, "POST", "/message-principals", token=admin, body={"action": "add", "name": "ops-contract-agent"}
    )
    assert principal.status == 201, principal
    for token in (None, "invalid-ops-token", node.json["token"], principal.json["principal"]["token"]):
        response = hc.request(hub.url, "GET", "/metrics", token=token)
        assert response.status == 401, response
    for path, headers in (("/metrics?token=refused", {}), ("/metrics", {"Cookie": "token=refused"})):
        assert hc.request(hub.url, "GET", path, headers=headers).status == 401
    response = hc.request(hub.url, "GET", "/metrics", token=admin)
    assert response.status == 200, response
    assert response.content_type.startswith("text/plain; version=0.0.4"), response
    text = response.body.decode("utf-8")
    assert "brigade_hub_writer_queue_depth NaN" in text
    assert "brigade_hub_writer_batch_size NaN" in text
    assert "brigade_hub_checkpoint_stall_frames " in text
    assert admin not in text
