"""Shared Fleet Hub message fixtures.

This module is never collected as a test module, so registering it through
``pytest_plugins`` works regardless of the order in which test files are named.
"""

import pytest

from tests.test_fleet_messages import message_hub  # noqa: F401  (registered as a fixture of this plugin)


class _RedactedHub(tuple):
    def __repr__(self):
        return "<fixture hub credentials redacted>"


@pytest.fixture
def hub(message_hub):  # noqa: F811
    return _RedactedHub(message_hub)
