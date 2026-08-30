"""n8n-operator runtime URL rules and secure file reads."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade.grokbot_n8n.contracts import N8nError
from brigade.grokbot_n8n.runtime_config import (
    MAX_RUNTIME_BYTES,
    assert_disjoint_paths,
    parse_n8n_private_runtime,
    read_secure_api_key,
    read_secure_runtime_text,
    validate_base_url,
)


def _write_mode(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_base_url_allows_https_root_and_loopback_http_only():
    assert validate_base_url("https://n8n.example.invalid") == "https://n8n.example.invalid"
    assert validate_base_url("https://n8n.example.invalid/") == "https://n8n.example.invalid"
    assert validate_base_url("http://127.0.0.1:5678") == "http://127.0.0.1:5678"
    assert validate_base_url("http://localhost") == "http://localhost"
    for value in (
        "http://n8n.example.invalid",
        "http://192.0.2.10",
        "https://user:pass@n8n.example.invalid",
        "https://n8n.example.invalid/api",
        "https://n8n.example.invalid?x=1",
        "https://n8n.example.invalid#frag",
        "ftp://n8n.example.invalid",
        "https://",
        "",
    ):
        with pytest.raises(N8nError) as caught:
            validate_base_url(value)
        assert caught.value.code == "invalid_request"
        assert "user" not in str(caught.value)
        assert "pass" not in str(caught.value)


def test_runtime_schema_is_exact_and_stores_api_key_file_reference(tmp_path: Path):
    key = tmp_path / "n8n.key"
    _write_mode(key, "n8n-placeholder-key-not-real\n")
    runtime = parse_n8n_private_runtime(
        {
            "version": 1,
            "base_url": "https://n8n.example.invalid",
            "api_key_file": str(key),
        }
    )
    assert runtime["version"] == 1
    assert runtime["base_url"] == "https://n8n.example.invalid"
    assert runtime["api_key_file"] == str(key)
    with pytest.raises(N8nError):
        parse_n8n_private_runtime(
            {
                "version": 1,
                "base_url": "https://n8n.example.invalid",
                "api_key_file": str(key),
                "api_key": "n8n-placeholder-key-not-real",
            }
        )
    with pytest.raises(N8nError):
        parse_n8n_private_runtime(
            {"version": 1, "base_url": "https://n8n.example.invalid", "api_key_env": "N8N_API_KEY"}
        )


def test_secure_runtime_and_key_reads_refuse_mode_owner_symlink_and_toctou(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime.json"
    key = tmp_path / "n8n.key"
    payload = {"version": 1, "base_url": "https://n8n.example.invalid", "api_key_file": str(key)}
    _write_mode(runtime, json.dumps(payload) + "\n")
    _write_mode(key, "n8n-placeholder-key-not-real\n")
    runtime_text = read_secure_runtime_text(str(runtime))
    assert str(key) in runtime_text
    assert "n8n-placeholder-key-not-real" not in runtime_text
    assert read_secure_api_key(str(key)) == "n8n-placeholder-key-not-real"

    world = tmp_path / "world.json"
    _write_mode(world, json.dumps(payload) + "\n", 0o644)
    with pytest.raises(N8nError):
        read_secure_runtime_text(str(world))

    linked = tmp_path / "linked.key"
    linked.symlink_to(key)
    with pytest.raises(N8nError):
        read_secure_api_key(str(linked))

    original = Path.read_text

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        if self.name in {"runtime.json", "n8n.key"}:
            raise AssertionError("TOCTOU Path.read_text")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    assert json.loads(read_secure_runtime_text(str(runtime)))["version"] == 1
    assert read_secure_api_key(str(key)) == "n8n-placeholder-key-not-real"


def test_disjoint_paths_reject_overlap_and_relative():
    with pytest.raises(N8nError) as caught:
        assert_disjoint_paths(["/var/lib/n8n/runtime.json", "/var/lib/n8n/runtime.json.d", "/var/lib/n8n"])
    assert caught.value.code == "invalid_request"
    assert "/var/lib" not in str(caught.value)
    with pytest.raises(N8nError):
        assert_disjoint_paths(["var/lib/a", "/var/lib/b", "/var/lib/c"])


def test_base_url_rejects_malformed_ports_and_encoded_host_syntax():
    assert validate_base_url("https://N8N.Example.INVALID:443") == "https://n8n.example.invalid:443"
    assert validate_base_url("http://[::1]:5678") == "http://[::1]:5678"
    assert validate_base_url("http://127.0.0.1") == "http://127.0.0.1"
    for value in (
        "https://n8n.example.invalid:99999",
        "https://n8n.example.invalid:65536",
        "https://n8n.example.invalid:0",
        "https://n8n.example.invalid:-1",
        "https://n8n.example.invalid:abc",
        "https://n8n.example.invalid:",
        "https://n8n.example%2einvalid",
        "https://n8n.example.invalid%00.evil",
        "https://n8n.example.invalid\n.evil",
        "https://n8n.example.invalid\t.evil",
        "http://127.0.0.1:99999",
        "http://localhost%2eexample.invalid",
        "https://127.0.0.1:80@n8n.example.invalid",
    ):
        with pytest.raises(N8nError) as caught:
            validate_base_url(value)
        assert caught.value.code == "invalid_request"
        assert "99999" not in str(caught.value)
        assert "%2e" not in str(caught.value)
        assert "example.invalid" not in str(caught.value)


def test_base_url_rejects_malformed_bracket_hosts_without_leaking_urlsplit():
    for value in (
        "http://[",
        "http://[::1",
        "https://[::1",
        "http://[]",
        "https://[::g]",
        "http://[127.0.0.1]",
        "https://[",
        "https://n8n.example.invalid]",
    ):
        with pytest.raises(N8nError) as caught:
            validate_base_url(value)
        assert caught.value.code == "invalid_request"
        assert "n8n runtime configuration is invalid" in str(caught.value)
        assert "ValueError" not in str(caught.value)
        assert "IPv6" not in str(caught.value)
        assert "IPv4" not in str(caught.value)
        assert "[" not in str(caught.value)
        assert "]" not in str(caught.value)
        assert "example.invalid" not in str(caught.value)


def test_runtime_read_rejects_growth_after_fstat(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime.json"
    key = tmp_path / "n8n.key"
    payload = json.dumps({"version": 1, "base_url": "https://n8n.example.invalid", "api_key_file": str(key)})
    _write_mode(runtime, payload + "\n")
    grown = False
    real_fstat = os.fstat

    def grow_after_fstat(descriptor: int) -> os.stat_result:
        nonlocal grown
        info = real_fstat(descriptor)
        if not grown:
            grown = True
            runtime.write_text(payload + ("x" * (MAX_RUNTIME_BYTES + 8)), encoding="utf-8")
            os.chmod(runtime, 0o600)
        return info

    monkeypatch.setattr(os, "fstat", grow_after_fstat)
    with pytest.raises(N8nError) as caught:
        read_secure_runtime_text(str(runtime))
    assert grown
    assert caught.value.code == "invalid_request"
    assert "n8n environment is invalid" in str(caught.value)
    assert str(runtime) not in str(caught.value)
    assert "xxxx" not in str(caught.value)


def test_hardlinked_runtime_and_key_alias_are_rejected(tmp_path: Path):
    runtime = tmp_path / "runtime.json"
    key = tmp_path / "n8n.key"
    alias = tmp_path / "alias.key"
    _write_mode(
        runtime, json.dumps({"version": 1, "base_url": "https://n8n.example.invalid", "api_key_file": str(key)})
    )
    _write_mode(key, "n8n-placeholder-key-not-real\n")
    os.link(key, alias)
    with pytest.raises(N8nError) as multi:
        read_secure_api_key(str(key))
    assert multi.value.code == "invalid_request"
    assert str(key) not in str(multi.value)
    assert str(alias) not in str(multi.value)
    assert "n8n-placeholder-key-not-real" not in str(multi.value)
    alias.unlink()
    key.unlink()
    os.link(runtime, key)
    with pytest.raises(N8nError) as linked:
        read_secure_api_key(str(key))
    assert linked.value.code == "invalid_request"
    assert str(runtime) not in str(linked.value)
    assert str(key) not in str(linked.value)


def test_windows_secure_read_fails_closed(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime.json"
    key = tmp_path / "n8n.key"
    _write_mode(
        runtime, json.dumps({"version": 1, "base_url": "https://n8n.example.invalid", "api_key_file": str(key)})
    )
    _write_mode(key, "n8n-placeholder-key-not-real\n")
    monkeypatch.setattr("brigade.grokbot_n8n.runtime_config.permission_policy", lambda: "unsupported")
    with pytest.raises(N8nError) as caught:
        read_secure_runtime_text(str(runtime))
    assert caught.value.code == "invalid_request"
    assert "n8n environment is invalid" in str(caught.value)
    assert str(runtime) not in str(caught.value)
    with pytest.raises(N8nError):
        read_secure_api_key(str(key))
