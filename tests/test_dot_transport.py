"""Protected transport readers retain the old fail-closed contract."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade.dot import transport
from tests._posix import requires_dirfd


@requires_dirfd
@pytest.mark.parametrize(
    "kind", ["missing", "symlink", "directory", "mode", "owner", "oversized", "growing", "invalid"]
)
def test_protected_token_refusals(tmp_path, monkeypatch, kind):
    path = tmp_path / "token"
    path.write_text("example-token\n", encoding="utf-8")
    path.chmod(0o600)
    if kind == "missing":
        path.unlink()
    elif kind == "symlink":
        destination = tmp_path / "destination"
        path.rename(destination)
        path.symlink_to(destination)
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "mode":
        path.chmod(0o644)
    elif kind == "owner":
        actual = os.fstat

        def wrong_owner(fd):
            info = actual(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1, st_size=info.st_size)

        monkeypatch.setattr(os, "fstat", wrong_owner)
    elif kind == "oversized":
        path.write_bytes(b"x" * 4099)
    elif kind == "growing":
        monkeypatch.setattr(os, "read", lambda fd, count: b"x" * count)
    elif kind == "invalid":
        path.write_bytes(b"\xff")
    with pytest.raises(transport.ConfigurationError, match="^invalid$"):
        transport.load_hub_token_file(path)


@requires_dirfd
def test_protected_token_and_nul_path(tmp_path):
    path = tmp_path / "token"
    path.write_text("example-token\n", encoding="utf-8")
    path.chmod(0o600)
    assert transport.load_hub_token_file(path) == "example-token"
    with pytest.raises(transport.ConfigurationError, match="^invalid$"):
        transport._read_descriptor_safe_token_file(Path("/tmp/invalid\x00hub-token"))


@requires_dirfd
def test_fifo_is_refused_without_blocking(tmp_path):
    path = tmp_path / "token"
    os.mkfifo(path, mode=0o600)
    with pytest.raises(transport.ConfigurationError, match="^invalid$"):
        transport.load_hub_token_file(path)


@pytest.mark.parametrize("path_text", ["/missing/example-token", "//server/share/token", "C:/example/token"])
def test_unavailable_nofollow_refuses_before_filesystem(monkeypatch, path_text):
    monkeypatch.setattr(transport.os, "O_NOFOLLOW", None, raising=False)

    def accessed(*args, **kwargs):
        pytest.fail("token read reached the filesystem without no-follow support")

    monkeypatch.setattr(transport.os, "open", accessed)
    with pytest.raises(transport.ConfigurationError, match="^invalid$"):
        transport.load_hub_token_file(Path(path_text))


def test_http_probes_disable_proxies_and_redirects(monkeypatch):
    captured = {}

    class Opener:
        def open(self, request, *, timeout):
            captured["timeout"] = timeout
            return "bounded response"

    def build(*handlers):
        captured["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(transport.urllib.request, "build_opener", build)
    request = transport.urllib.request.Request("https://connector.example.test")
    assert transport._open_http(request, 2) == "bounded response"
    assert captured["timeout"] == 2
    proxy, redirect = captured["handlers"]
    assert proxy.proxies == {}
    assert isinstance(redirect, transport._NoRedirect)
    assert redirect.redirect_request() is None
