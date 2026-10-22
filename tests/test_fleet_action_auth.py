"""Operator capability is separate from dashboard viewing."""

import pytest

from brigade import fleet_hub


def test_bootstrap_explicit_activation_expiry_and_revocation(tmp_path, monkeypatch):
    from brigade import fleet_action_auth as auth

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    grant = auth.grant_operator(
        conn, "operator@example.test", [{"target_id": "repair", "subject": {"kind": "telemetry", "node_id": "fixture"}}]
    )
    monkeypatch.setattr(auth.time, "time", lambda: 1000)
    nonce, proof = auth.bootstrap("operator@example.test", "fixture-signing-key")
    with pytest.raises(auth.ActionError):
        auth.activate(conn, "viewer@example.test", nonce, proof, "fixture-signing-key")
    credential, csrf = auth.activate(conn, "operator@example.test", nonce, proof, "fixture-signing-key")
    assert auth.session(conn, credential, csrf)["grant_id"] == grant
    stored = " ".join(str(row) for row in conn.execute("SELECT * FROM action_sessions"))
    assert credential not in stored and csrf not in stored
    monkeypatch.setattr(auth.time, "time", lambda: 1901)
    with pytest.raises(auth.ActionError):
        auth.session(conn, credential, csrf)
    monkeypatch.setattr(auth.time, "time", lambda: 1001)
    auth.revoke_operator(conn, grant)
    with pytest.raises(auth.ActionError):
        auth.session(conn, credential, csrf)
    conn.close()
