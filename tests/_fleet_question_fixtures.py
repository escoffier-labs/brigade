"""Dedicated operator authority for question tests, separate from node sessions."""

import pytest

from brigade import fleet_hub, fleet_hub_messages
from tests._fleet_message_fixtures import _RedactedHub
from tests.test_fleet_messages import NODE_A

pytest_plugins = ["tests._fleet_message_fixtures"]

OPERATOR = "agent:operator"


@pytest.fixture
def question_hub(message_hub, monkeypatch):
    base, asker_token, _node_token, db = message_hub
    with fleet_hub.open_db(db) as conn:
        principal = fleet_hub_messages.add_principal(conn, {"name": "operator", "may_send_to": [NODE_A, "agent:*"]})
    monkeypatch.setenv("BRIGADE_FLEET_OPERATOR_RECIPIENT", OPERATOR)
    return _RedactedHub((base, asker_token, principal["token"], db))
