"""Runtime JSON, environment, and pinned TLS for Obsidian Operator."""

from __future__ import annotations

import json
import os
import ssl
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from brigade.grokbot_obsidian.contracts import ObsidianError
from brigade.grokbot_obsidian.runtime_config import (
    ADAPTER_ENVIRONMENT_KEYS,
    MAX_CA_BYTES,
    adapter_environment,
    load_runtime_env,
    parse_private_runtime,
    read_secure_runtime_text,
)
from brigade.grokbot_obsidian.tls import extract_spki_pin, pin_of_pem, pinned_fetch

WRONG_PIN = "sha256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
UPSTREAM_KEY = "u" * 16
BEARER = "a" * 32


class _RecordedHTTPSServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler, *, recorded):
        super().__init__(server_address, handler)
        self.recorded = recorded


def _issue_cert(work: Path, label: str, san: str) -> dict[str, str]:
    cnf = work / f"{label}.cnf"
    cnf.write_text(f"[v3]\nsubjectAltName={san}\n", encoding="utf-8")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(work / f"{label}-ca.key"),
            "-out",
            str(work / f"{label}-ca.pem"),
            "-days",
            "1",
            "-subj",
            f"/CN={label}-ca",
        ],
        check=True,
        capture_output=True,
        cwd=work,
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(work / f"{label}-server.key"),
            "-out",
            str(work / f"{label}-server.csr"),
            "-subj",
            "/CN=127.0.0.1",
        ],
        check=True,
        capture_output=True,
        cwd=work,
    )
    subprocess.run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(work / f"{label}-server.csr"),
            "-CA",
            str(work / f"{label}-ca.pem"),
            "-CAkey",
            str(work / f"{label}-ca.key"),
            "-CAcreateserial",
            "-out",
            str(work / f"{label}-server.pem"),
            "-days",
            "1",
            "-extfile",
            str(cnf),
            "-extensions",
            "v3",
        ],
        check=True,
        capture_output=True,
        cwd=work,
    )
    cert_pem = (work / f"{label}-server.pem").read_text(encoding="utf-8")
    return {
        "ca": (work / f"{label}-ca.pem").read_text(encoding="utf-8"),
        "cert": cert_pem,
        "key": (work / f"{label}-server.key").read_text(encoding="utf-8"),
        "pin": pin_of_pem(cert_pem),
    }


def _serve_https(tmp_path: Path, cert: dict[str, str], handler_cls, host: str = "127.0.0.1"):
    recorded: list[dict[str, str]] = []

    class Handler(handler_cls):
        pass

    server = _RecordedHTTPSServer((host, 0), Handler, recorded=recorded)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert_path = tmp_path / "live-server.pem"
    key_path = tmp_path / "live-server.key"
    cert_path.write_text(cert["cert"], encoding="utf-8")
    key_path.write_text(cert["key"], encoding="utf-8")
    context.load_cert_chain(str(cert_path), str(key_path))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    display = f"[{host}]" if ":" in host else host
    return server, recorded, f"https://{display}:{port}/"


def _write_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def test_runtime_env_defaults_to_loopback_8773_and_requires_https_upstream(tmp_path: Path):
    helper = _write_executable(tmp_path / "excalidraw")
    env = {
        "GROKBOT_OBSIDIAN_TOKEN": "a" * 32,
        "GROKBOT_OBSIDIAN_HOST": "127.0.0.1",
        "GROKBOT_OBSIDIAN_UPSTREAM_URL": "https://127.0.0.1:27124/",
        "GROKBOT_OBSIDIAN_UPSTREAM_KEY": "u" * 16,
        "GROKBOT_OBSIDIAN_RUNTIME_PATH": str(tmp_path / "runtime.json"),
        "GROKBOT_OBSIDIAN_ACTION_STATE_PATH": str(tmp_path / "actions"),
        "GROKBOT_OBSIDIAN_APPROVAL_DIR": str(tmp_path / "approvals"),
        "GROKBOT_OBSIDIAN_STAGING_DIR": str(tmp_path / "staging"),
        "GROKBOT_OBSIDIAN_EXCALIDRAW_BIN": str(helper),
    }
    loaded = load_runtime_env(env)
    assert loaded["port"] == 8773
    assert loaded["host"] == "127.0.0.1"
    with pytest.raises(ObsidianError):
        load_runtime_env({**env, "GROKBOT_OBSIDIAN_UPSTREAM_URL": "http://127.0.0.1:27124/"})
    with pytest.raises(ObsidianError):
        load_runtime_env({**env, "GROKBOT_OBSIDIAN_UPSTREAM_URL": "https://example.test/"})
    with pytest.raises(ObsidianError):
        load_runtime_env({**env, "NODE_TLS_REJECT_UNAUTHORIZED": "0"})


def test_runtime_env_requires_disjoint_state_paths(tmp_path: Path):
    helper = _write_executable(tmp_path / "excalidraw")
    env = {
        "GROKBOT_OBSIDIAN_TOKEN": "a" * 32,
        "GROKBOT_OBSIDIAN_HOST": "127.0.0.1",
        "GROKBOT_OBSIDIAN_UPSTREAM_URL": "https://127.0.0.1:27124/",
        "GROKBOT_OBSIDIAN_UPSTREAM_KEY": "u" * 16,
        "GROKBOT_OBSIDIAN_RUNTIME_PATH": str(tmp_path / "state" / "runtime.json"),
        "GROKBOT_OBSIDIAN_ACTION_STATE_PATH": str(tmp_path / "state"),
        "GROKBOT_OBSIDIAN_APPROVAL_DIR": str(tmp_path / "approvals"),
        "GROKBOT_OBSIDIAN_STAGING_DIR": str(tmp_path / "staging"),
        "GROKBOT_OBSIDIAN_EXCALIDRAW_BIN": str(helper),
    }
    with pytest.raises(ObsidianError) as caught:
        load_runtime_env(env)
    assert caught.value.code == "invalid_request"
    assert str(tmp_path) not in str(caught.value)


def test_private_runtime_rejects_phase2_plugin_actions(tmp_path: Path):
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", encoding="utf-8")
    with pytest.raises(ObsidianError):
        parse_private_runtime(
            {
                "version": 1,
                "upstream_tls": {"ca_path": str(ca), "spki_sha256": ["sha256:" + ("A" * 43) + "="]},
                "templates": {"catalog": []},
                "home_note": "Home.md",
                "flashcard_note": "03 - Resources/Cards.md",
                "flashcard_heading": "Inbox",
                "dashboard_root": "01 - Projects/Dashboard.base",
                "daily_notes_folder": "",
                "sensitive_tags": [],
                "sensitive_path_prefixes": [],
                "command_fingerprint": "sha256:" + ("0" * 64),
                "plugin_inventory": [
                    {"id": "canvas", "version": "1", "supported_action_ids": ["patch_canvas"]},
                ],
                "excalidraw": {
                    "enabled": False,
                    "verified_suffix": ".excalidraw.md",
                    "probe_receipt_sha256": "sha256:" + ("0" * 64),
                },
            },
            filesystem_read=lambda path, maximum: b"ca",
        )


def test_spki_pin_extracts_from_generated_certificate(tmp_path: Path):
    key = tmp_path / "key.pem"
    cert = tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    pem = cert.read_text(encoding="utf-8")
    pin = pin_of_pem(pem)
    assert pin.startswith("sha256:")
    assert len(pin) == 51
    assert extract_spki_pin(__import__("ssl").PEM_cert_to_DER_cert(pem)) == pin
    fetch = pinned_fetch(pem.encode("utf-8"), (pin,))
    with pytest.raises(ObsidianError) as caught:
        fetch("http://127.0.0.1/")
    assert caught.value.code == "invalid_request"


def test_adapter_environment_is_allowlisted():
    env = adapter_environment(
        {
            **{key: f"value-{key}" for key in ADAPTER_ENVIRONMENT_KEYS},
            "GROKBOT_OBSIDIAN_TOKEN": "not-a-real-token-value-32chars!!",
            "GROKBOT_OBSIDIAN_UPSTREAM_KEY": "upstream-secret",
            "AWS_SECRET": "x",
        }
    )
    assert set(env) == set(ADAPTER_ENVIRONMENT_KEYS)
    assert "GROKBOT_OBSIDIAN_TOKEN" not in env
    assert "GROKBOT_OBSIDIAN_UPSTREAM_KEY" not in env
    assert "AWS_SECRET" not in env


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.recorded.append({"authorization": self.headers.get("Authorization", "")})
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args) -> None:
        return


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.recorded.append({"authorization": self.headers.get("Authorization", "")})
        self.send_response(302)
        self.send_header("Location", "/elsewhere")
        self.end_headers()

    def log_message(self, *_args) -> None:
        return


class _HugeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.recorded.append({"authorization": self.headers.get("Authorization", "")})
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"x" * (262_144 + 1))

    def log_message(self, *_args) -> None:
        return


def test_pinned_fetch_rejects_wrong_ca_ip_name_pin_redirect_and_oversize(tmp_path: Path):
    old = _issue_cert(tmp_path, "old", "IP:127.0.0.1")
    other = _issue_cert(tmp_path, "other", "IP:127.0.0.1")
    host = _issue_cert(tmp_path, "host", "DNS:localhost")
    server, recorded, url = _serve_https(tmp_path, old, _OkHandler)
    host_server, _, host_url = _serve_https(tmp_path, host, _OkHandler)
    try:
        good = pinned_fetch(old["ca"].encode("utf-8"), (old["pin"],))
        assert good(url)["body"] == b"ok"
        with pytest.raises(ObsidianError) as wrong_ca:
            pinned_fetch(other["ca"].encode("utf-8"), (old["pin"],))(url)
        assert wrong_ca.value.code == "unavailable"
        with pytest.raises(ObsidianError) as wrong_ip:
            pinned_fetch(host["ca"].encode("utf-8"), (host["pin"],))(host_url)
        assert wrong_ip.value.code == "unavailable"
        with pytest.raises(ObsidianError) as wrong_pin:
            pinned_fetch(old["ca"].encode("utf-8"), (WRONG_PIN,))(
                url,
                headers={"Authorization": f"Bearer {UPSTREAM_KEY}"},
            )
        assert wrong_pin.value.code == "unavailable"
        assert not any(item.get("authorization", "").startswith("Bearer ") for item in recorded[1:])
        assert all(UPSTREAM_KEY not in item.get("authorization", "") for item in recorded[1:])
    finally:
        server.shutdown()
        host_server.shutdown()


def test_pinned_fetch_rotation_pins_accept_old_and_new_then_reject_old(tmp_path: Path):
    old = _issue_cert(tmp_path, "rot-old", "IP:127.0.0.1")
    new = _issue_cert(tmp_path, "rot-new", "IP:127.0.0.1")
    old_server, _, old_url = _serve_https(tmp_path, old, _OkHandler)
    new_server, _, new_url = _serve_https(tmp_path, new, _OkHandler)
    overlap_ca = (old["ca"] + "\n" + new["ca"]).encode("utf-8")
    try:
        overlap = pinned_fetch(overlap_ca, (old["pin"], new["pin"]))
        assert overlap(old_url)["body"] == b"ok"
        assert overlap(new_url)["body"] == b"ok"
        newest = pinned_fetch(new["ca"].encode("utf-8"), (new["pin"],))
        assert newest(new_url)["body"] == b"ok"
        with pytest.raises(ObsidianError):
            newest(old_url)
    finally:
        old_server.shutdown()
        new_server.shutdown()


def test_pinned_fetch_rejects_redirects_and_oversize_response(tmp_path: Path):
    cert = _issue_cert(tmp_path, "bound", "IP:127.0.0.1")
    redirect_server, _, redirect_url = _serve_https(tmp_path, cert, _RedirectHandler)
    huge_server, _, huge_url = _serve_https(tmp_path, cert, _HugeHandler)
    fetch = pinned_fetch(cert["ca"].encode("utf-8"), (cert["pin"],))
    try:
        with pytest.raises(ObsidianError) as redirected:
            fetch(redirect_url)
        assert redirected.value.code == "unavailable"
        with pytest.raises(ObsidianError) as oversized:
            fetch(huge_url)
        assert oversized.value.code == "unavailable"
    finally:
        redirect_server.shutdown()
        huge_server.shutdown()


def _runtime_file(path: Path, text: str = "{}", *, mode: int = 0o600) -> Path:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_secure_runtime_read_rejects_world_readable_symlink_foreign_owner_and_oversize(tmp_path: Path, monkeypatch):
    runtime = _runtime_file(tmp_path / "runtime.json", '{"ok":true}', mode=0o644)
    with pytest.raises(ObsidianError) as world:
        read_secure_runtime_text(str(runtime))
    assert world.value.code == "invalid_request"
    assert str(runtime) not in str(world.value)
    assert '{"ok":true}' not in str(world.value)
    os.chmod(runtime, 0o600)
    link = tmp_path / "runtime.link"
    link.symlink_to(runtime)
    with pytest.raises(ObsidianError) as linked:
        read_secure_runtime_text(str(link))
    assert linked.value.code == "invalid_request"
    assert str(link) not in str(linked.value)
    assert str(runtime) not in str(linked.value)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    with pytest.raises(ObsidianError) as foreign:
        read_secure_runtime_text(str(runtime))
    assert foreign.value.code == "invalid_request"
    assert str(runtime) not in str(foreign.value)
    monkeypatch.undo()
    os.chmod(runtime, 0o600)
    runtime.write_text("x" * (MAX_CA_BYTES + 1), encoding="utf-8")
    os.chmod(runtime, 0o600)
    with pytest.raises(ObsidianError) as oversized:
        read_secure_runtime_text(str(runtime), max_bytes=MAX_CA_BYTES)
    assert oversized.value.code == "invalid_request"
    assert "x" * 16 not in str(oversized.value)
    assert json.loads(read_secure_runtime_text(str(_runtime_file(tmp_path / "ok.json")))) == {}


def test_secure_runtime_read_rejects_path_swap_malformed_and_never_echoes(tmp_path: Path, monkeypatch):
    runtime = _runtime_file(tmp_path / "runtime.json", '{"secret":"do-not-echo"}')
    real_fstat = os.fstat

    def mismatch_regular(fd):
        info = real_fstat(fd)
        if stat.S_ISREG(info.st_mode):
            return type(
                "Stat",
                (),
                {
                    "st_mode": info.st_mode,
                    "st_uid": info.st_uid,
                    "st_dev": info.st_dev,
                    "st_ino": info.st_ino + 99,
                },
            )()
        return info

    monkeypatch.setattr(os, "fstat", mismatch_regular)
    with pytest.raises(ObsidianError) as swapped:
        read_secure_runtime_text(str(runtime))
    assert swapped.value.code == "invalid_request"
    assert "do-not-echo" not in str(swapped.value)
    assert str(runtime) not in str(swapped.value)
    monkeypatch.setattr(os, "fstat", real_fstat)
    broken = tmp_path / "broken.json"
    broken.write_bytes(b"\xff\xfe")
    os.chmod(broken, 0o600)
    with pytest.raises(ObsidianError) as malformed:
        read_secure_runtime_text(str(broken))
    assert malformed.value.code == "invalid_request"
    assert str(broken) not in str(malformed.value)


def test_wrong_pin_never_sends_authorization_bearing_http(tmp_path: Path):
    cert = _issue_cert(tmp_path, "auth", "IP:127.0.0.1")
    server, recorded, url = _serve_https(tmp_path, cert, _OkHandler)
    fetch = pinned_fetch(cert["ca"].encode("utf-8"), (WRONG_PIN,))
    try:
        with pytest.raises(ObsidianError):
            fetch(url, headers={"Authorization": f"Bearer {UPSTREAM_KEY}"})
        assert recorded == []
    finally:
        server.shutdown()
