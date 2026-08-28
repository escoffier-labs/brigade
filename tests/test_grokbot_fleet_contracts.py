"""Contract tests for Fleet Steward identifiers and strict tool inputs."""

from __future__ import annotations

import pytest

from brigade.grokbot_fleet.contracts import (
    FleetError,
    parse_execute_input,
    parse_host_status_input,
    parse_identifier,
    parse_incident_input,
    parse_opaque_id,
    parse_overview_input,
    parse_propose_input,
    parse_service_health_input,
)


def test_opaque_id_accepts_exactly_32_lowercase_hex():
    assert parse_opaque_id("abc123def4567890abc123def4567890") == "abc123def4567890abc123def4567890"
    assert parse_opaque_id("0" * 32) == "0" * 32
    assert parse_opaque_id("f" * 32) == "f" * 32


def test_opaque_id_rejects_uppercase_short_long_and_non_hex():
    for value in (
        "",
        "a" * 31,
        "a" * 33,
        "A" * 32,
        "ABC123DEF4567890ABC123DEF4567890",
        "g" * 32,
        "0123456789abcdef0123456789abcdeG",
        "restart-service",
        "research-bridge:unhealthy-service",
        "feedfacecafebabefeedfacecafebabe!",
    ):
        with pytest.raises(FleetError) as caught:
            parse_opaque_id(value)
        assert caught.value.code == "invalid_request"
        assert "restart-service" not in str(caught.value)
        assert "research-bridge" not in str(caught.value)


def test_identifier_accepts_boundaries_and_catalog_names():
    assert parse_identifier("a") == "a"
    assert parse_identifier("A") == "A"
    assert parse_identifier("a" * 64) == "a" * 64
    assert parse_identifier("host-a.host_b:svc-1") == "host-a.host_b:svc-1"
    assert parse_identifier("research-bridge:unhealthy-service") == "research-bridge:unhealthy-service"
    assert parse_identifier("restart-service") == "restart-service"


def test_identifier_rejects_unsafe_values():
    for value in (
        "",
        "a" * 65,
        "-leading",
        ".leading",
        "_leading",
        ":leading",
        "host/sub",
        "host sub",
        "../host-a",
        "host%61",
        "host\na",
        "host;a",
        "host@a",
    ):
        with pytest.raises(FleetError) as caught:
            parse_identifier(value)
        assert caught.value.code == "invalid_request"


def test_strict_tool_inputs_accept_and_reject_extra_keys():
    assert parse_overview_input({}) == {}
    assert parse_host_status_input({"alias": "host-a"}) == {"alias": "host-a"}
    assert parse_service_health_input({"service_id": "svc-1"}) == {"service_id": "svc-1"}
    assert parse_incident_input({"scope": "host-a"}) == {"scope": "host-a"}
    assert parse_propose_input({"finding_id": "finding-1"}) == {"finding_id": "finding-1"}
    assert parse_execute_input({"proposal_id": "a" * 32}) == {"proposal_id": "a" * 32}
    with pytest.raises(FleetError):
        parse_overview_input({"extra": True})
    with pytest.raises(FleetError):
        parse_host_status_input({"alias": "host-a", "extra": True})
    with pytest.raises(FleetError):
        parse_execute_input({"proposal_id": "restart-service"})
