"""The hardened git commit resolver (verify slice 9a, D3 "Commit resolver contract").

Every test runs real git against scratch repositories. The "remote" is a bare
repository served over HTTPS by a small local server with a self-signed CA,
because the resolver accepts only https remotes. The CA reaches git through the
resolver's test hook, never through the environment variables the resolver
removes. Planted hooks, helpers and commands write a marker file when they run,
and every test that plants one asserts the marker never appears.
"""

from __future__ import annotations

import datetime
import gc
import hashlib
import http.server
import logging
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from brigade import memory_verify_config as config
from brigade import memory_verify_contract as vc
from brigade import memory_verify_resolver as resolver
from brigade import proc

posix = pytest.mark.skipif(os.name != "posix", reason="scratch HTTPS remotes and marker scripts need POSIX")
held = pytest.mark.skipif(
    os.name != "posix" or not proc.held_exec_supported(), reason="the resolver runs git only with held execution"
)
pytestmark = [pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")]
# Variables the test's own setup git calls must not inherit from the developer's shell.
SETUP_DROPPED = frozenset(
    {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "curl_ca_bundle", "ssl_cert_file", "ssl_cert_dir"}
)
REAL_GIT_ENVIRONMENT = resolver.git_environment

SCOPE = "repo:example"
IDENTITY = {
    "GIT_AUTHOR_NAME": "Example",
    "GIT_AUTHOR_EMAIL": "example@example.com",
    "GIT_COMMITTER_NAME": "Example",
    "GIT_COMMITTER_EMAIL": "example@example.com",
}


# --- Scratch HTTPS remote ----------------------------------------------------------------


def make_ca(directory: Path) -> tuple[Path, Path, Path]:
    """A CA bundle plus a server certificate and key for `localhost`."""
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "verify resolver test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_file, cert_file, key_file = directory / "ca.pem", directory / "server.pem", directory / "server.key"
    ca_file.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return ca_file, cert_file, key_file


class HttpsServer:
    """Serves a directory over HTTPS on localhost. git falls back to its dumb HTTP protocol against it."""

    def __init__(self, root: Path, cert: Path, key: Path) -> None:
        handler = _quiet_handler(root)
        self.httpd = http.server.ThreadingHTTPServer(("localhost", 0), handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, name: str) -> str:
        return f"https://localhost:{self.port}/{name}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _quiet_handler(root: Path) -> type[http.server.SimpleHTTPRequestHandler]:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, format, *args):  # noqa: A002 - signature fixed by the base class
            pass

    return Handler


class Listener:
    """A TCP listener that records every connection and never answers. Stands in for a proxy or a stalled host."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("localhost", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self.held: list[socket.socket] = []
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            self.held.append(conn)

    def stop(self) -> None:
        self.sock.close()
        for conn in self.held:
            conn.close()


# --- Scratch repositories ----------------------------------------------------------------


def setup_env(ca: Path | None = None) -> dict[str, str]:
    """Environment for the test's own setup git calls. Never used by the resolver."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k.lower() not in SETUP_DROPPED}
    env.update(IDENTITY, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    if ca is not None:
        env["GIT_SSL_CAINFO"] = str(ca)
    return env


def git(cwd: Path, *args: str, ca: Path | None = None, input: bytes | None = None) -> str:  # noqa: A002
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=setup_env(ca), input=input, capture_output=True, check=False, timeout=60
    )
    assert result.returncode == 0, (args, result.stderr.decode(errors="replace"))
    return result.stdout.decode().strip()


def marker_script(path: Path, marker: Path) -> Path:
    path.write_text(f"#!/bin/sh\necho ran >> '{marker}'\nexit 0\n")
    path.chmod(0o755)
    return path


class World:
    """A bare remote served over HTTPS, an authoring clone that pushes to it, and the bound workspace clone."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.ca, cert, key = make_ca(root)
        self.served = root / "served"
        self.served.mkdir()
        self.bare = self.served / "remote.git"
        git(root, "init", "--quiet", "--bare", "--initial-branch=main", str(self.bare))
        self.server = HttpsServer(self.served, cert, key)
        self.url = self.server.url("remote.git")
        self.author = root / "author"
        git(root, "init", "--quiet", "--initial-branch=main", str(self.author))
        git(self.author, "remote", "add", "origin", str(self.bare))
        self.base = self.commit("base")
        self.workspace = root / "workspace"
        git(root, "clone", "--quiet", self.url, str(self.workspace), ca=self.ca)
        git(self.workspace, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.markers = root / "markers"
        self.markers.mkdir()

    def commit(self, message: str, branch: str = "main", push: bool = True) -> str:
        if git(self.author, "branch", "--show-current") != branch:
            existing = git(self.author, "branch", "--list", branch)
            git(self.author, "checkout", "--quiet", *(() if existing else ("-b",)), branch)
        (self.author / "file.txt").write_text(message + "\n")
        git(self.author, "add", "file.txt")
        git(self.author, "commit", "--quiet", "-m", message)
        full = git(self.author, "rev-parse", "HEAD")
        if push:
            self.push(branch)
        return full

    def push(self, *refspecs: str) -> None:
        git(self.author, "push", "--quiet", "--force", "origin", *refspecs)
        git(self.bare, "update-server-info")

    def marker(self, name: str) -> Path:
        return self.markers / name

    def ran(self) -> list[str]:
        return sorted(path.name for path in self.markers.iterdir())

    def bound(self, path: Path | None = None, **extra) -> config.Workspace:
        entry = {"scope": SCOPE, "path": str(path or self.workspace), "archive_projects": [], **extra}
        return config.parse({"verify": {"workspace": [entry]}}).workspaces[0]

    def resolve(self, abbreviation: str, *, run: resolver.Run | None = None, **extra) -> str | None:
        return resolver.commit_resolver(self.bound(**extra), run)(abbreviation)


def trust_scratch_ca(monkeypatch, ca: Path) -> None:
    """Test-only: give the resolver's git the scratch CA. Production code has no way to add one."""

    def with_ca(*args, **kwargs):
        return {**REAL_GIT_ENVIRONMENT(*args, **kwargs), "GIT_SSL_CAINFO": str(ca)}

    monkeypatch.setattr(resolver, "git_environment", with_ca)


def confine_temp(monkeypatch, tmp_path: Path) -> Path:
    """Point the resolver's private directories, and any tempfile use, under this test's own tmp_path."""
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setattr(resolver, "SYSTEM_TEMP", str(system_temp), raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
    return system_temp


@pytest.fixture
def world(tmp_path, monkeypatch):
    if not resolver.platform_supported():
        pytest.skip("the resolver runs git only with held execution (Linux)")
    confine_temp(monkeypatch, tmp_path)
    for name in list(os.environ):
        if name.startswith("GIT_") or name.lower() in SETUP_DROPPED:
            monkeypatch.delenv(name)
    built = World(tmp_path)
    trust_scratch_ca(monkeypatch, built.ca)
    yield built
    built.server.stop()


def short(full: str, length: int = vc.MIN_ABBREVIATION) -> str:
    return full[:length]


# --- Ground objects that share a prefix -------------------------------------------------


def _object_id(kind: str, body: bytes) -> str:
    return hashlib.sha1(b"%s %d\0" % (kind.encode(), len(body)) + body).hexdigest()


def grind_twins(tree: str, parent: str, *, second_kind: str = "commit", length: int = 7) -> tuple[bytes, bytes, str]:
    """A commit body and a second object body (commit or blob) whose ids share a `length`-hex prefix.

    A birthday search over 28 bits needs about twenty thousand candidates, which hashes in well under a second.
    """
    seen: dict[str, bytes] = {}
    others: dict[str, bytes] = {}
    for index in range(2_000_000):
        body = (
            f"tree {tree}\nparent {parent}\nauthor Example <example@example.com> 1700000000 +0000\n"
            f"committer Example <example@example.com> 1700000000 +0000\n\ntwin {index}\n"
        ).encode()
        prefix = _object_id("commit", body)[:length]
        if second_kind == "commit":
            if prefix in seen and seen[prefix] != body:
                return seen[prefix], body, prefix
            seen[prefix] = body
            continue
        blob = f"blob twin {index}\n".encode()
        blob_prefix = _object_id("blob", blob)[:length]
        seen[prefix] = body
        others[blob_prefix] = blob
        if prefix in others:
            return body, others[prefix], prefix
        if blob_prefix in seen:
            return seen[blob_prefix], blob, blob_prefix
    raise AssertionError("no twin found")


def write_object(repo: Path, kind: str, body: bytes) -> str:
    return git(repo, "hash-object", "-t", kind, "-w", "--stdin", input=body)


# --- The positive case -------------------------------------------------------------------


def test_a_unique_fresh_reachable_commit_resolves_to_its_full_id(world):
    target = world.commit("target")  # pushed after the workspace cloned, so only a fetch in this run finds it
    assert world.resolve(short(target)) == target
    assert git(world.workspace, "rev-parse", "refs/remotes/origin/main") == target


def test_resolution_accepts_every_length_from_the_minimum_up(world):
    target = world.commit("target")
    run = resolver.Run()
    for length in (vc.MIN_ABBREVIATION, 12, 39):
        assert world.resolve(short(target, length), run=run) == target


def test_explicit_commit_refs_allow_a_second_branch(world):
    release = world.commit("release only", branch="release")
    refs = ["refs/remotes/origin/main", "refs/remotes/origin/release"]
    assert world.resolve(short(release), commit_refs=refs) == release
    assert world.resolve(short(release)) is None  # the default allows origin/HEAD's target only


# --- None cases from D3 ------------------------------------------------------------------


def test_an_abbreviation_under_the_minimum_never_reaches_git(world, monkeypatch):
    target = world.commit("target")
    calls = []
    monkeypatch.setattr(resolver, "_git", lambda *args: calls.append(args))
    for value in (short(target, vc.MIN_ABBREVIATION - 1), short(target, 4), "", target.upper()[:12], "zz" * 6):
        assert world.resolve(value) is None
    assert calls == []


def test_six_characters_are_refused_and_the_minimum_is_seven(world):
    assert vc.MIN_ABBREVIATION == 7
    target = world.commit("target")
    assert world.resolve(short(target, 6)) is None
    assert world.resolve(short(target, 7)) == target


def test_a_repository_outside_the_bound_workspace_is_refused(world):
    target = world.commit("target")
    inner = world.workspace / "nested"
    inner.mkdir()
    assert resolver.commit_resolver(world.bound(path=inner))(short(target)) is None
    plain = world.root / "plain"
    plain.mkdir()
    assert resolver.commit_resolver(world.bound(path=plain))(short(target)) is None


def test_a_shallow_clone_is_refused(world):
    world.commit("second")
    target = world.commit("target")
    shallow = world.root / "shallow"
    git(world.root, "clone", "--quiet", "--depth", "1", f"file://{world.bare}", str(shallow))
    git(shallow, "remote", "set-url", "origin", world.url)
    git(shallow, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    assert resolver.commit_resolver(world.bound(path=shallow))(short(target)) is None


def test_a_partial_clone_is_refused(world):
    target = world.commit("target")
    git(world.workspace, "config", "core.repositoryformatversion", "1")
    git(world.workspace, "config", "extensions.partialClone", "origin")
    assert world.resolve(short(target)) is None


def test_an_unset_default_ref_is_refused(world):
    target = world.commit("target")
    git(world.workspace, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    assert world.resolve(short(target)) is None


def test_a_default_ref_that_names_a_local_branch_is_refused(world):
    target = world.commit("target")
    git(world.workspace, "fetch", "--quiet", "origin", ca=world.ca)
    git(world.workspace, "update-ref", "refs/heads/main", target)
    git(world.workspace, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/heads/main")
    assert world.resolve(short(target)) is None


def test_a_default_ref_under_another_remote_is_refused(world):
    target = world.commit("target")
    git(world.workspace, "remote", "add", "upstream", world.url)
    git(world.workspace, "fetch", "--quiet", "upstream", ca=world.ca)
    git(world.workspace, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/upstream/main")
    assert world.resolve(short(target)) is None
    assert world.resolve(short(target), commit_refs=["refs/remotes/upstream/main"]) == target


def test_a_missing_allowed_ref_is_refused(world):
    target = world.commit("target")
    assert world.resolve(short(target), commit_refs=["refs/remotes/origin/absent"]) is None
    assert world.resolve(short(target), commit_refs=["refs/remotes/origin/main", "refs/remotes/origin/absent"]) is None


def test_a_failed_fetch_is_refused(world):
    target = world.commit("target")
    git(world.workspace, "fetch", "--quiet", "origin", ca=world.ca)
    git(world.workspace, "remote", "set-url", "origin", world.server.url("missing.git"))
    assert world.resolve(short(target)) is None


def test_a_fetch_that_times_out_is_refused(world, monkeypatch):
    target = world.commit("target")
    git(world.workspace, "fetch", "--quiet", "origin", ca=world.ca)
    stalled = Listener()
    try:
        git(world.workspace, "remote", "set-url", "origin", f"https://localhost:{stalled.port}/remote.git")
        monkeypatch.setattr(resolver, "TIMEOUT_SECONDS", 2.0)
        codes = []
        real_run = resolver.proc.run_held

        def spy(*args, **kwargs):
            result = real_run(*args, **kwargs)
            codes.append(result.code)
            return result

        monkeypatch.setattr(resolver.proc, "run_held", spy)
        assert world.resolve(short(target)) is None
        assert stalled.connections >= 1
        # The resolver's own timeout ended the fetch, not curl's much longer connect timeout.
        assert codes[-1] == resolver.PROC_TIMEOUT_CODE
    finally:
        stalled.stop()


def test_a_fetch_proof_from_an_earlier_run_does_not_count(world):
    target = world.commit("target")
    first = resolver.Run()
    assert world.resolve(short(target), run=first) == target
    later = world.commit("later")
    # The same run reuses its proof and does not fetch again, so the later commit is not known yet.
    assert world.resolve(short(later), run=first) is None
    world.server.stop()
    # A new run must fetch again. With the remote down, the first run's proof does not carry over.
    assert world.resolve(short(target), run=resolver.Run()) is None


def test_a_new_run_fetches_again(world):
    first = resolver.Run()
    assert world.resolve(short(world.base), run=first) == world.base
    later = world.commit("later")
    assert world.resolve(short(later), run=resolver.Run()) == later


def test_no_matching_object_is_refused(world):
    world.commit("target")
    assert world.resolve("0123456789abc") is None


def test_ambiguity_from_an_object_on_another_ref_is_refused(world):
    tree = git(world.author, "rev-parse", "HEAD^{tree}")
    head = git(world.author, "rev-parse", "HEAD")
    first, second, prefix = grind_twins(tree, head)
    on_main = write_object(world.author, "commit", first)
    on_other = write_object(world.author, "commit", second)
    assert on_main[:7] == on_other[:7] == prefix and on_main != on_other
    git(world.author, "update-ref", "refs/heads/main", on_main)
    git(world.author, "update-ref", "refs/heads/other", on_other)
    world.push("main", "other")
    git(world.workspace, "fetch", "--quiet", "origin", ca=world.ca)  # origin/other lands in the object database
    assert world.resolve(prefix) is None
    # One more character separates them, and the commit on the allowed ref then resolves.
    longer = on_main[: next(i for i in range(8, 40) if on_main[:i] != on_other[:i])]
    assert world.resolve(longer) == on_main


def test_ambiguity_between_two_reachable_commits_is_refused(world):
    """Both twins sit on allowed refs, so whichever one a resolver picked would pass every later check."""
    tree = git(world.author, "rev-parse", "HEAD^{tree}")
    head = git(world.author, "rev-parse", "HEAD")
    first, second, prefix = grind_twins(tree, head)
    on_main = write_object(world.author, "commit", first)
    on_release = write_object(world.author, "commit", second)
    git(world.author, "update-ref", "refs/heads/main", on_main)
    git(world.author, "update-ref", "refs/heads/release", on_release)
    world.push("main", "release")
    refs = ["refs/remotes/origin/main", "refs/remotes/origin/release"]
    assert world.resolve(prefix, commit_refs=refs) is None
    longer = on_release[: next(i for i in range(8, 40) if on_main[:i] != on_release[:i])]
    assert world.resolve(longer, commit_refs=refs) == on_release


def test_ambiguity_with_a_blob_is_refused(world):
    tree = git(world.author, "rev-parse", "HEAD^{tree}")
    head = git(world.author, "rev-parse", "HEAD")
    commit_body, blob_body, prefix = grind_twins(tree, head, second_kind="blob")
    target = write_object(world.author, "commit", commit_body)
    git(world.author, "update-ref", "refs/heads/main", target)
    world.push("main")
    write_object(world.workspace, "blob", blob_body)  # unreachable, loose, never on any ref
    assert world.resolve(prefix) is None


def test_a_match_that_is_not_a_commit_is_refused(world):
    world.commit("target")
    blob = write_object(world.workspace, "blob", b"just a blob\n")
    tree = git(world.workspace, "rev-parse", "refs/remotes/origin/main^{tree}")
    assert world.resolve(short(blob)) is None
    assert world.resolve(short(tree)) is None


def test_an_annotated_tag_is_not_peeled_to_its_commit(world):
    target = world.commit("target")
    git(world.author, "tag", "-a", "-m", "annotated", "v1", target)
    tag = git(world.author, "rev-parse", "v1")
    world.push("refs/tags/v1")
    git(world.workspace, "fetch", "--quiet", "origin", "+refs/tags/v1:refs/tags/v1", ca=world.ca)
    assert world.resolve(short(tag)) is None


def test_a_commit_not_reachable_from_an_allowed_ref_is_refused(world):
    other = world.commit("other branch", branch="other")
    git(world.workspace, "fetch", "--quiet", "origin", ca=world.ca)
    assert world.resolve(short(other)) is None
    (world.workspace / "local.txt").write_text("local\n")
    git(world.workspace, "add", "local.txt")
    git(world.workspace, "commit", "--quiet", "-m", "local only")
    local = git(world.workspace, "rev-parse", "HEAD")
    assert world.resolve(short(local)) is None


def test_a_branch_or_tag_named_like_the_abbreviation_never_answers(world):
    target = world.commit("target")
    decoy = world.commit("decoy", branch="decoy")
    git(world.workspace, "fetch", "--quiet", "origin", ca=world.ca)
    for name in (short(target), "0123456789abc"):
        git(world.workspace, "branch", "--force", name, decoy)
        git(world.workspace, "tag", "--force", name, decoy)
    assert world.resolve(short(target)) == target
    assert world.resolve("0123456789abc") is None


def test_no_git_in_the_system_directories_is_refused(world, monkeypatch):
    target = world.commit("target")
    empty = world.root / "empty-bin"
    empty.mkdir()
    monkeypatch.setattr(resolver, "GIT_CANDIDATES", (str(empty / "git"),))
    assert world.resolve(short(target)) is None


def test_ls_remote_proves_freshness_before_git_2_41(world, monkeypatch):
    monkeypatch.setattr(resolver, "PORCELAIN_VERSION", (99, 0))
    calls = []
    real = resolver._git

    def spy(tool, cwd, *args):
        calls.append(args[0])
        return real(tool, cwd, *args)

    monkeypatch.setattr(resolver, "_git", spy)
    target = world.commit("target")
    assert world.resolve(short(target)) == target
    assert "ls-remote" in calls


def test_ls_remote_mismatch_is_refused_before_git_2_41(world, monkeypatch):
    monkeypatch.setattr(resolver, "PORCELAIN_VERSION", (99, 0))
    target = world.commit("target")
    real = resolver._git

    def moved(tool, cwd, *args):
        result = real(tool, cwd, *args)
        if args[0] == "ls-remote" and result is not None:
            return resolver._Result(result.code, result.out.replace(target, world.base))
        return result

    monkeypatch.setattr(resolver, "_git", moved)
    assert world.resolve(short(target)) is None


def test_a_fetch_whose_porcelain_does_not_prove_the_ref_is_refused(world, monkeypatch):
    target = world.commit("target")
    real = resolver._git

    def silent(tool, cwd, *args):
        result = real(tool, cwd, *args)
        if args[0] == "fetch" and result is not None:
            return resolver._Result(result.code, "")
        return result

    monkeypatch.setattr(resolver, "_git", silent)
    assert world.resolve(short(target)) is None


@pytest.mark.parametrize(
    "output",
    [
        "",
        "! {old} {new} {ref}\n",
        "- {old} {new} {ref}\n",
        "t {old} {new} {ref}\n",
        "* {old} {new} refs/remotes/origin/other\n",
        "* {old} {other} {ref}\n",
        "* {old} {new} {ref}\n* {old} {new} refs/remotes/origin/other\n",
        "* {old} {new} {ref} extra\n",
        "*{old} {new} {ref}\n",
    ],
)
def test_porcelain_without_proof_is_refused(output):
    ref, old, new, other = "refs/remotes/origin/main", "0" * 40, "a" * 40, "b" * 40
    assert not resolver._porcelain_proves(output.format(old=old, new=new, other=other, ref=ref), ref, new)


@pytest.mark.parametrize("flag", [" ", "+", "*", "="])
def test_porcelain_with_proof_is_accepted(flag):
    ref, new = "refs/remotes/origin/main", "a" * 40
    assert resolver._porcelain_proves(f"{flag} {'0' * 40} {new} {ref}\n", ref, new)


# --- Hostile settings planted in the repository ------------------------------------------


def plant(world: World, *pairs: tuple[str, str]) -> None:
    for key, value in pairs:
        git(world.workspace, "config", "--local", "--add", key, value)


def test_an_include_is_refused_unread(world):
    target = world.commit("target")
    hook = marker_script(world.root / "fsmonitor.sh", world.marker("include"))
    included = world.root / "included.cfg"
    included.write_text(f"[core]\n\tfsmonitor = {hook}\n[alias]\n\tx = !{hook}\n")
    plant(world, ("include.path", str(included)))
    assert world.resolve(short(target)) is None
    plant_conditional = world.root / "conditional.cfg"
    plant_conditional.write_text("[core]\n\tbare = false\n")
    git(world.workspace, "config", "--local", "--unset-all", "include.path")
    plant(world, ("includeIf.gitdir:/.path", str(plant_conditional)))
    assert world.resolve(short(target)) is None
    assert world.ran() == []


def test_an_fsmonitor_command_is_refused(world):
    target = world.commit("target")
    hook = marker_script(world.root / "fsmonitor.sh", world.marker("fsmonitor"))
    plant(world, ("core.fsmonitor", str(hook)))
    assert world.resolve(short(target)) is None
    assert world.ran() == []


def test_a_hooks_path_is_refused(world):
    target = world.commit("target")
    hooks = world.root / "hooks"
    hooks.mkdir()
    marker_script(hooks / "reference-transaction", world.marker("hooks-path"))
    plant(world, ("core.hooksPath", str(hooks)))
    assert world.resolve(short(target)) is None
    assert world.ran() == []


def test_a_reference_transaction_hook_in_the_default_location_never_runs(world):
    target = world.commit("target")
    marker_script(world.workspace / ".git" / "hooks" / "reference-transaction", world.marker("default-hook"))
    assert world.resolve(short(target)) == target  # the fetch updated a ref, so the hook would have fired
    assert world.ran() == []


def test_a_credential_helper_is_refused(world):
    target = world.commit("target")
    helper = marker_script(world.root / "helper.sh", world.marker("credential"))
    plant(world, ("credential.helper", f"!{helper}"))
    assert world.resolve(short(target)) is None
    git(world.workspace, "config", "--local", "--unset-all", "credential.helper")
    plant(world, (f"credential.{world.url}.username", "someone"))
    assert world.resolve(short(target)) is None
    assert world.ran() == []


def test_an_alias_is_refused(world):
    target = world.commit("target")
    script = marker_script(world.root / "alias.sh", world.marker("alias"))
    plant(world, ("alias.fetch", f"!{script}"), ("alias.rev-parse", f"!{script}"))
    assert world.resolve(short(target)) is None
    assert world.ran() == []


@pytest.mark.parametrize("variable", ["insteadOf", "pushInsteadOf"])
def test_a_url_rewrite_is_refused(world, variable):
    target = world.commit("target")
    plant(world, (f"url.https://localhost:1/elsewhere.git.{variable}", world.url))
    assert world.resolve(short(target)) is None


@pytest.mark.parametrize(
    "url",
    [
        "ext::sh -c echo% ran% >>% {marker}",
        "file://{bare}",
        "{bare}",
        "fd::7",
        "file::{bare}",
        "brigadetest::https://localhost/remote.git",
        "ssh://localhost/remote.git",
        "git://localhost/remote.git",
        "http://localhost/remote.git",
        "HTTPS://localhost/remote.git",
        "https:///remote.git",
    ],
)
def test_a_remote_url_that_is_not_https_is_refused(world, monkeypatch, url):
    target = world.commit("target")
    helper_dir = world.root / "helper-bin"
    helper_dir.mkdir()
    marker_script(helper_dir / "git-remote-brigadetest", world.marker("remote-helper"))
    monkeypatch.setenv("PATH", f"{helper_dir}{os.pathsep}{os.environ['PATH']}")
    git(
        world.workspace,
        "remote",
        "set-url",
        "origin",
        url.format(marker=world.marker("ext"), bare=world.bare),
    )
    assert world.resolve(short(target)) is None
    assert world.ran() == []


def test_two_urls_on_the_allowed_remote_are_refused(world):
    target = world.commit("target")
    plant(world, ("remote.origin.url", world.url))
    assert world.resolve(short(target)) is None


def test_an_alternate_refs_command_is_refused(world):
    target = world.commit("target")
    script = marker_script(world.root / "alternate.sh", world.marker("alternate-refs"))
    plant(world, ("core.alternateRefsCommand", str(script)))
    assert world.resolve(short(target)) is None
    assert world.ran() == []


@pytest.mark.parametrize(
    "key",
    ["remote.origin.vcs", "remote.origin.uploadpack", "remote.origin.proxy", "remote.origin.proxyAuthMethod"],
)
def test_remote_command_and_proxy_settings_are_refused(world, key):
    target = world.commit("target")
    script = marker_script(world.root / "remote.sh", world.marker("remote-setting"))
    plant(world, (key, str(script) if key.endswith(("vcs", "uploadpack")) else "http://localhost:9"))
    assert world.resolve(short(target)) is None
    assert world.ran() == []


@pytest.mark.parametrize(
    "pair",
    [
        ("http.sslVerify", "false"),
        ("http.https://localhost/.proxy", "http://localhost:9"),
        ("http.proxy", "http://localhost:9"),
        ("http.extraHeader", "X-Planted: 1"),
        ("http.followRedirects", "true"),
        ("http.sslCAInfo", "/nonexistent/ca.pem"),
        ("http.cookieFile", "/nonexistent/cookies"),
        ("protocol.file.allow", "always"),
        ("core.sshCommand", "/bin/false"),
        ("core.gitProxy", "/bin/false"),
        ("core.askPass", "/bin/false"),
        ("fetch.bundleURI", "https://localhost:9/bundle"),
        ("bundle.version", "1"),
        ("extensions.worktreeConfig", "true"),
    ],
)
def test_transport_settings_in_local_config_are_refused(world, pair):
    target = world.commit("target")
    plant(world, pair)
    assert world.resolve(short(target)) is None


def test_a_grafts_file_is_refused(world):
    target = world.commit("target")
    (world.workspace / ".git" / "info").mkdir(exist_ok=True)
    (world.workspace / ".git" / "info" / "grafts").write_text(f"{target}\n")
    assert world.resolve(short(target)) is None


def test_a_planted_replacement_ref_changes_nothing(world):
    target = world.commit("target")
    blob = write_object(world.workspace, "blob", b"replacement\n")
    git(world.workspace, "fetch", "--quiet", "origin", ca=world.ca)
    git(world.workspace, "replace", "--force", target, blob)
    git(world.workspace, "replace", "--force", git(world.workspace, "rev-parse", "refs/remotes/origin/main"), blob)
    assert git(world.workspace, "cat-file", "-t", target) == "blob"  # what git without the guard would see
    assert world.resolve(short(target)) == target


def test_a_tag_refspec_in_config_fetches_no_tag(world):
    target = world.commit("target")
    git(world.author, "tag", "planted", target)
    git(world.author, "push", "--quiet", "origin", "refs/tags/planted")
    git(world.bare, "update-server-info")
    git(world.workspace, "tag", "local-only", world.base)
    plant(
        world,
        ("remote.origin.fetch", "+refs/tags/*:refs/tags/*"),
        ("remote.origin.tagOpt", "--tags"),
        ("remote.origin.pruneTags", "true"),
        ("fetch.pruneTags", "true"),
    )
    assert world.resolve(short(target)) == target
    # No tag arrives, and pruneTags never deletes the tag that exists only here.
    assert git(world.workspace, "tag", "--list") == "local-only"


def test_an_inherited_git_dir_is_ignored(world, monkeypatch):
    target = world.commit("target")
    other = world.root / "other"
    git(world.root, "init", "--quiet", str(other))
    marker_script(other / ".git" / "hooks" / "reference-transaction", world.marker("git-dir"))
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'url.https://localhost:1/x.git.insteadof'='{world.url}'")
    monkeypatch.setenv("GIT_GRAFT_FILE", str(world.root / "grafts"))
    (world.root / "grafts").write_text(f"{target}\n")
    assert world.resolve(short(target)) == target
    assert world.ran() == []


def test_an_inherited_proxy_is_never_contacted(world, monkeypatch):
    target = world.commit("target")
    proxy = Listener()
    try:
        for name in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "http_proxy"):
            monkeypatch.setenv(name, f"http://localhost:{proxy.port}")
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.setattr(resolver, "TIMEOUT_SECONDS", 10.0)
        assert world.resolve(short(target)) == target
        assert proxy.connections == 0
    finally:
        proxy.stop()


def test_inherited_tls_variables_are_removed(world, monkeypatch):
    target = world.commit("target")
    monkeypatch.setattr(resolver, "git_environment", REAL_GIT_ENVIRONMENT)
    for name in ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"):
        monkeypatch.setenv(name, str(world.ca))
    # Without the test-only CA the scratch server is not trusted, so a variable that leaked through would make this pass.
    assert world.resolve(short(target)) is None


# --- The wrapper itself ------------------------------------------------------------------


HOSTILE_INHERITED = {
    "PATH": "/attacker/bin",
    "HOME": "/attacker/home",
    "XDG_CONFIG_HOME": "/attacker/xdg",
    "GIT_DIR": "x",
    "GIT_CONFIG_PARAMETERS": "x",
    "GIT_CONFIG_COUNT": "1",
    "git_trace": "1",
    "LD_PRELOAD": "/attacker/lib.so",
    "LD_LIBRARY_PATH": "/attacker/lib",
    "DYLD_INSERT_LIBRARIES": "/attacker/lib.dylib",
    "OPENSSL_CONF": "/attacker/openssl.cnf",
    "OPENSSL_MODULES": "/attacker/modules",
    "SSLKEYLOGFILE": "/attacker/keys",
    "HTTPS_PROXY": "x",
    "http_proxy": "x",
    "All_Proxy": "x",
    "NO_PROXY": "x",
    "CURL_CA_BUNDLE": "x",
    "CURL_HOME": "x",
    "SSL_CERT_FILE": "x",
    "SSL_CERT_DIR": "x",
    "TMPDIR": "/attacker/tmp",
}


def test_the_environment_is_built_from_scratch(monkeypatch):
    for name, value in HOSTILE_INHERITED.items():
        monkeypatch.setenv(name, value)
    env = resolver.git_environment("/private/home", "/usr/lib/git-core")
    assert env == {
        "PATH": os.pathsep.join(("/usr/lib/git-core", *resolver.trusted_dirs(resolver.SYSTEM_DIRS))),
        "HOME": "/private/home",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    assert set(env) == resolver.ENV_ALLOWLIST


@posix
def test_untrusted_system_dirs_are_dropped_from_path(tmp_path, monkeypatch):
    user_bin = tmp_path / "user-bin"
    user_bin.mkdir()
    monkeypatch.setattr(resolver, "SYSTEM_DIRS", (str(user_bin), "/usr/bin", "/bin", str(tmp_path / "absent")))
    env = resolver.git_environment("/private/home", "/usr/lib/git-core")
    path = env["PATH"].split(os.pathsep)
    assert str(user_bin) not in path and str(tmp_path / "absent") not in path
    assert path[0] == "/usr/lib/git-core" and len(path) == len(set(path))  # /bin collapses into /usr/bin


def test_every_call_carries_the_overrides():
    tool = resolver.GitTool("/usr/bin/git", 3, {})
    argv = tool.argv(["status"])
    assert argv[:3] == ["/usr/bin/git", "--no-pager", "--no-replace-objects"]
    assert argv[-1] == "status"
    overrides = [argv[index + 1] for index, value in enumerate(argv) if value == "-c"]
    assert overrides == list(resolver.OVERRIDES)
    assert not any("sslcainfo" in value.lower() for value in argv)  # production never adds a CA
    hooks = next(value for value in overrides if value.startswith("core.hooksPath="))
    assert not os.path.exists(hooks.partition("=")[2])
    for required in (
        "core.fsmonitor=false",
        "credential.helper=",
        "protocol.allow=never",
        "protocol.https.allow=always",
        "http.sslVerify=true",
        "http.proxy=",
        "http.followRedirects=false",
        "http.extraHeader=",
        "http.cookieFile=",
    ):
        assert required in overrides


def test_the_module_has_no_ca_hook():
    assert not hasattr(resolver, "_TEST_CA_FILE")
    source = Path(resolver.__file__).read_text()
    assert "sslCAInfo" not in source and "GIT_SSL_CAINFO" not in source


def test_https_url_accepts_only_plain_https():
    assert resolver.https_url("https://example.com/repo.git")
    for value in (
        "http://example.com/repo.git",
        "ssh://example.com/repo.git",
        "ext::sh",
        "file:///repo.git",
        "/srv/repo.git",
        "https::https://example.com/repo.git",
        "https://",
        "https://example.com/repo .git",
        "https://example.com/\nrepo.git",
    ):
        assert not resolver.https_url(value)


@pytest.mark.parametrize(
    "key",
    [
        "include.path",
        "includeIf.gitdir:/x/.path",
        "credential.helper",
        "credential.https://example.com.username",
        "alias.co",
        "http.proxy",
        "http.https://example.com/.sslVerify",
        "protocol.allow",
        "core.fsmonitor",
        "core.hooksPath",
        "core.sshCommand",
        "core.gitProxy",
        "core.askPass",
        "core.alternateRefsCommand",
        "url.https://example.com/.insteadOf",
        "url.https://example.com/.pushInsteadOf",
        "remote.origin.vcs",
        "remote.origin.uploadpack",
        "remote.origin.proxy",
        "remote.origin.proxyAuthMethod",
        "extensions.partialClone",
        "extensions.worktreeConfig",
        "fetch.bundleURI",
        "bundle.heuristic",
    ],
)
def test_refused_keys(key):
    assert resolver.refused_key(key)


@pytest.mark.parametrize(
    "key", ["core.bare", "remote.origin.url", "remote.origin.fetch", "branch.main.remote", "user.name"]
)
def test_ordinary_keys_are_allowed(key):
    assert not resolver.refused_key(key)


def test_a_resolver_error_returns_none(world, monkeypatch):
    def boom(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(resolver, "_git", boom)
    assert world.resolve(short(world.base)) is None


# --- Fix round 1: trusted git, from-scratch environment, process tree -------------------


def real_git() -> str:
    found = shutil.which("git")
    assert found is not None
    return os.path.realpath(found)


def collect(run_holder: list) -> None:
    """Drop the only reference to a Run and collect it, so its cleanup finalizers run."""
    run_holder.clear()
    gc.collect()


def held_python(tmp_path: Path) -> resolver.GitTool:
    """A GitTool whose held image is this Python, for process-tree tests that need a script."""
    real = os.path.realpath(sys.executable)
    fd = proc.high_fd(os.open(real, os.O_RDONLY | os.O_CLOEXEC))
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    return resolver.GitTool(real, fd, REAL_GIT_ENVIRONMENT(str(home)))


def test_a_git_earlier_on_the_callers_path_never_runs(world, monkeypatch):
    target = world.commit("target")
    fake = world.root / "fake-bin"
    fake.mkdir()
    script = fake / "git"
    script.write_text(f"#!/bin/sh\necho ran >> '{world.marker('path-git')}'\nexec '{real_git()}' \"$@\"\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    assert world.resolve(short(target)) == target
    assert world.ran() == []


def test_ld_preload_never_reaches_git(world, monkeypatch):
    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        pytest.skip("needs a C compiler")
    target = world.commit("target")
    source = world.root / "mark.c"
    marker = world.marker("ld-preload")
    source.write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void mark(void) {\n"
        f'  FILE *f = fopen("{marker}", "a"); if (f) {{ fputs("ran\\n", f); fclose(f); }}\n'
        "}\n"
    )
    library = world.root / "libmark.so"
    built = subprocess.run([compiler, "-shared", "-fPIC", "-o", str(library), str(source)], capture_output=True)
    if built.returncode != 0:
        pytest.skip("the marker library did not compile")
    monkeypatch.setenv("LD_PRELOAD", str(library))
    assert world.resolve(short(target)) == target
    assert world.ran() == []


def test_git_receives_exactly_the_allowlisted_environment(world, monkeypatch):
    target = world.commit("target")
    monkeypatch.setattr(resolver, "git_environment", REAL_GIT_ENVIRONMENT)
    for name, value in {**HOSTILE_INHERITED, "PATH": f"{world.root}{os.pathsep}{os.environ['PATH']}"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OPENSSL_CONF", str(world.root / "openssl.cnf"))
    seen = []
    real_run = resolver.proc.run_held

    def spy(argv, fd, *args, env=None, **kwargs):
        home = env["HOME"]
        seen.append((argv, os.fstat(fd), dict(env), sorted(os.listdir(home)), stat.S_IMODE(os.stat(home).st_mode)))
        return real_run(argv, fd, *args, env=env, **kwargs)

    monkeypatch.setattr(resolver.proc, "run_held", spy)
    world.resolve(short(target))  # the scratch CA is untrusted here, so only the calls matter
    commands = [argv[argv.index("--no-replace-objects") + 1 + 2 * len(resolver.OVERRIDES)] for argv, *_ in seen]
    assert "fetch" in commands
    for argv, held_image, env, listing, mode in seen:
        assert set(env) == resolver.ENV_ALLOWLIST
        assert argv[0] == real_git() and os.path.samestat(held_image, os.stat(real_git()))
        assert str(world.root) not in env["PATH"] and not env["HOME"].startswith("/attacker")
        assert listing == [] and mode == 0o700
        assert Path(env["HOME"]).parent.parent == Path(resolver.SYSTEM_TEMP)


def test_an_untrusted_exec_directory_is_refused(world, monkeypatch):
    """The helpers link to the real root-owned ones, so only the exec directory's own chain can refuse it."""
    target = world.commit("target")
    loose = world.root / "loose-exec"
    loose.mkdir()
    helper = os.path.realpath(os.path.join(git(world.root, "--exec-path"), "git-remote-https"))
    os.symlink(helper, loose / "git-remote-https")
    os.symlink(helper, loose / "git-remote-http")
    loose.chmod(0o777)
    real = resolver._git

    def reported(tool, cwd, *args):
        if args == ("--exec-path",):
            return resolver._Result(0, f"{loose}\n")
        return real(tool, cwd, *args)

    monkeypatch.setattr(resolver, "_git", reported)
    try:
        assert world.resolve(short(target)) is None
    finally:
        loose.chmod(0o700)


HEARTBEAT = """
import sys, time
while True:
    with open(sys.argv[1], "a") as beat:
        beat.write(".")
    time.sleep(0.05)
"""
PARENT = """
import subprocess, sys, time
subprocess.Popen(
    [sys.executable, "-c", sys.argv[2], sys.argv[1]],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
time.sleep(120)
"""
LINGERING_PARENT = """
import os, subprocess, sys, time
subprocess.Popen(
    [sys.executable, "-c", sys.argv[2], sys.argv[1]],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
for _ in range(200):
    if os.path.exists(sys.argv[1]):
        break
    time.sleep(0.05)
"""


def assert_heartbeat_stopped(beat: Path) -> None:
    assert beat.exists()
    before = beat.stat().st_size
    time.sleep(1.0)
    assert beat.stat().st_size == before


@held
@pytest.mark.parametrize("script, timeout", [(PARENT, 3.0), (LINGERING_PARENT, 60.0)])
def test_no_child_of_git_outlives_the_call(tmp_path, monkeypatch, script, timeout):
    """A grandchild left by a timed-out call, or by a call that exited normally, is gone afterwards."""
    beat = tmp_path / "beat.txt"
    parent = tmp_path / "parent.py"
    parent.write_text(script)
    monkeypatch.setattr(resolver, "TIMEOUT_SECONDS", timeout)
    monkeypatch.setattr(resolver.GitTool, "argv", lambda self, args: [self.path, str(parent), str(beat), HEARTBEAT])
    tool = held_python(tmp_path)
    try:
        result = resolver._git(tool, str(tmp_path), "fetch")
    finally:
        os.close(tool.fd)
    assert (result is None) if script is PARENT else (result is not None and result.code == 0)
    assert_heartbeat_stopped(beat)


def test_a_failed_fetch_with_proving_porcelain_is_refused(world, monkeypatch):
    target = world.commit("target")
    real = resolver._git

    def failed(tool, cwd, *args):
        result = real(tool, cwd, *args)
        if args[0] == "fetch" and result is not None:
            assert result.code == 0 and result.out  # the porcelain line would prove the ref
            return resolver._Result(1, result.out)
        return result

    monkeypatch.setattr(resolver, "_git", failed)
    assert world.resolve(short(target)) is None


@pytest.mark.parametrize("porcelain", [True, False])
def test_the_fetch_argv_pins_tags_prune_and_the_refspec(world, monkeypatch, porcelain):
    if not porcelain:
        monkeypatch.setattr(resolver, "PORCELAIN_VERSION", (99, 0))
    target = world.commit("target")
    fetches = []
    real = resolver._git

    def spy(tool, cwd, *args):
        if args[0] == "fetch":
            fetches.append(args)
        return real(tool, cwd, *args)

    monkeypatch.setattr(resolver, "_git", spy)
    assert world.resolve(short(target)) == target
    (args,) = fetches
    assert args[-2:] == ("origin", "+refs/heads/main:refs/remotes/origin/main")
    for flag in ("--no-tags", "--no-prune-tags", "--prune", "--no-recurse-submodules", "--no-write-fetch-head"):
        assert flag in args
    assert ("--porcelain" in args and "--verbose" in args) is porcelain


# --- Fix round 2: private HOME cleanup ---------------------------------------------------


@pytest.fixture
def made_home(tmp_path, monkeypatch, caplog):
    """A Run with its private HOME made under this test's tmp_path, plus the sentinel directory to protect."""
    if os.name != "posix":
        pytest.skip("the private HOME exists only where the resolver runs")
    system_temp = confine_temp(monkeypatch, tmp_path)
    caplog.set_level(logging.WARNING, logger=resolver.__name__)
    holder = [resolver.Run()]
    home = Path(resolver._private_home(holder[0]))
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "keep.txt").write_text("keep\n")
    return holder, home, system_temp, sentinel


def left_in_place(caplog) -> bool:
    return any("left its private HOME in place" in record.getMessage() for record in caplog.records)


def test_the_private_home_and_its_parent_are_removed_after_the_run(made_home, caplog):
    holder, home, system_temp, _ = made_home
    assert home.is_dir() and home.parent.parent == system_temp
    assert stat.S_IMODE(home.stat().st_mode) == stat.S_IMODE(home.parent.stat().st_mode) == 0o700
    (home / "scratch").write_text("x")
    collect(holder)
    assert list(system_temp.iterdir()) == []
    assert not left_in_place(caplog)


def test_a_home_replaced_by_another_directory_is_left_in_place(made_home, caplog):
    holder, home, _, _ = made_home
    os.rename(home, home.with_name(home.name + "-original"))
    home.mkdir()
    (home / "keep.txt").write_text("keep\n")
    collect(holder)
    assert (home / "keep.txt").read_text() == "keep\n"
    assert left_in_place(caplog)


def test_a_home_replaced_by_a_symlink_is_left_in_place(made_home, caplog):
    holder, home, _, sentinel = made_home
    os.rename(home, home.with_name(home.name + "-original"))
    os.symlink(sentinel, home)
    collect(holder)
    assert (sentinel / "keep.txt").read_text() == "keep\n"
    assert home.is_symlink()
    assert left_in_place(caplog)


def test_a_home_moved_outside_its_parent_is_left_in_place(made_home, caplog):
    """The parent becomes a link to another directory that holds a same-named child."""
    holder, home, _, sentinel = made_home
    parent = home.parent
    lookalike = sentinel / home.name
    lookalike.mkdir()
    (lookalike / "keep.txt").write_text("keep\n")
    os.rename(parent, parent.with_name(parent.name + "-moved"))
    os.symlink(sentinel, parent)
    collect(holder)
    assert (lookalike / "keep.txt").read_text() == "keep\n"
    assert (sentinel / "keep.txt").read_text() == "keep\n"
    assert left_in_place(caplog)


def test_a_home_moved_into_a_replacement_parent_is_left_in_place(made_home, caplog):
    """The HOME keeps its inode but now sits in a different directory at the parent's path."""
    holder, home, _, _ = made_home
    parent = home.parent
    moved = parent.with_name(parent.name + "-moved")
    os.rename(parent, moved)
    parent.mkdir(mode=0o700)
    os.rename(moved / home.name, home)
    (home / "keep.txt").write_text("keep\n")
    collect(holder)
    assert (home / "keep.txt").read_text() == "keep\n"
    assert left_in_place(caplog)


def test_a_parent_reached_through_a_link_is_left_in_place(made_home, caplog):
    """The parent path becomes a link to the original parent: same identities, but reached through a link."""
    holder, home, _, _ = made_home
    parent = home.parent
    real_parent = parent.with_name(parent.name + "-real")
    os.rename(parent, real_parent)
    os.symlink(real_parent, parent)
    (real_parent / home.name / "keep.txt").write_text("keep\n")
    collect(holder)
    assert (real_parent / home.name / "keep.txt").read_text() == "keep\n"
    assert left_in_place(caplog)


def after_home_check(monkeypatch, home: Path, action) -> list:
    """Run `action` once, right after the cleanup's identity check on the HOME passes."""
    metadata = os.lstat(home)
    home_id = (metadata.st_dev, metadata.st_ino)
    real_matches = resolver._matches
    done: list = []

    def matches(found, identity):
        result = real_matches(found, identity)
        if result and identity == home_id and not done:
            action()
            done.append(True)
        return result

    monkeypatch.setattr(resolver, "_matches", matches)
    return done


def test_a_home_swapped_right_after_its_identity_check_is_never_emptied(made_home, monkeypatch, caplog):
    """Codex P2 and Daybreak P1: an unrelated directory swapped in after the check keeps its contents."""
    holder, home, _, _ = made_home
    (home / "own.txt").write_text("resolver scratch\n")
    original = home.with_name(home.name + "-original")

    def swap_home():
        os.rename(home, original)
        home.mkdir()
        (home / "keep.txt").write_text("keep\n")

    swapped = after_home_check(monkeypatch, home, swap_home)
    collect(holder)
    assert swapped
    assert (home / "keep.txt").read_text() == "keep\n"
    assert not (original / "own.txt").exists()  # the held original was emptied
    assert left_in_place(caplog)


def test_an_empty_directory_swapped_in_after_the_contents_are_removed_survives(made_home, monkeypatch, caplog):
    """The final rmdir is by name, so the name is checked again first. An unrelated empty directory survives."""
    holder, home, _, _ = made_home
    original = home.with_name(home.name + "-original")
    real_clear = resolver._clear_held

    def clear_then_swap(fd):
        real_clear(fd)
        os.rename(home, original)
        home.mkdir()

    monkeypatch.setattr(resolver, "_clear_held", clear_then_swap)
    collect(holder)
    assert home.is_dir() and list(home.iterdir()) == []
    assert left_in_place(caplog)


def test_a_subdirectory_swapped_during_removal_is_never_emptied(made_home, monkeypatch, caplog):
    """Inside the HOME, a directory replaced between its lstat and its open is refused, not emptied."""
    holder, home, _, _ = made_home
    (home / "sub").mkdir()
    (home / "sub" / "own.txt").write_text("resolver scratch\n")
    real_open = resolver._open_child_dir
    swapped = []

    def swap_then_open(parent_fd, name):
        if name == "sub" and not swapped:
            os.rename(home / "sub", home / "sub-original")
            (home / "sub").mkdir()
            (home / "sub" / "keep.txt").write_text("keep\n")
            swapped.append(True)
        return real_open(parent_fd, name)

    monkeypatch.setattr(resolver, "_open_child_dir", swap_then_open)
    collect(holder)
    assert swapped
    assert (home / "sub" / "keep.txt").read_text() == "keep\n"
    assert left_in_place(caplog)


def test_removal_after_the_checks_never_follows_a_swapped_parent(made_home, monkeypatch):
    """The parent is swapped for a link right after the identity checks pass. Removal stays on the held parent."""
    holder, home, _, sentinel = made_home
    parent = home.parent
    lookalike = sentinel / home.name
    lookalike.mkdir()
    (lookalike / "keep.txt").write_text("keep\n")

    def swap_parent():
        os.rename(parent, parent.with_name(parent.name + "-moved"))
        os.symlink(sentinel, parent)

    swapped = after_home_check(monkeypatch, home, swap_parent)
    collect(holder)
    assert swapped
    assert (lookalike / "keep.txt").read_text() == "keep\n"
    assert not (parent.with_name(parent.name + "-moved") / home.name).exists()  # the real HOME was removed


# --- Fix round 2: only a root-owned git, run from a held descriptor ----------------------


def fake_root(monkeypatch, *trusted: Path) -> None:
    """Fake root ownership for everything under `trusted`. The mode rules (write, setuid, setgid) still apply there.

    The real directories above a fake root (the test's tmp_path, /tmp and so on) are accepted as they are, since
    a test cannot make them root-owned.
    """
    real_node = resolver._trusted_node
    roots = [str(path) for path in trusted]

    def node(path, metadata):
        if any(path == root or path.startswith(root + os.sep) for root in roots):
            # Only the owner is faked. The module's own mode rules decide the rest.
            as_root = os.stat_result(
                (metadata.st_mode, metadata.st_ino, metadata.st_dev, metadata.st_nlink, 0, 0, 0, 0, 0, 0)
            )
            return real_node(path, as_root)
        if any(root.startswith(path.rstrip(os.sep) + os.sep) for root in roots):
            return True
        return real_node(path, metadata)

    monkeypatch.setattr(resolver, "_trusted_node", node)


# The mode bits a trusted node must not carry. The test below pins the module's value.
UNSAFE_MODE = 0o022 | stat.S_ISUID | stat.S_ISGID


def test_the_unsafe_mode_mask_is_write_setuid_and_setgid():
    assert resolver.UNSAFE_MODE == UNSAFE_MODE


def tree(root: Path, *parts: str) -> Path:
    """Make root/parts with every created directory at 0755, so a fake root passes the mode rules."""
    current = root
    current.mkdir(mode=0o755, exist_ok=True)
    current.chmod(0o755)
    for part in parts:
        current = current / part
        current.mkdir(mode=0o755, exist_ok=True)
        current.chmod(0o755)
    return current


@posix
def test_a_user_owned_git_is_refused(tmp_path, monkeypatch):
    if os.getuid() == 0:
        pytest.skip("root owns every file it creates")
    directory = tmp_path / "bin"
    directory.mkdir()
    shutil.copy2(real_git(), directory / "git")
    monkeypatch.setattr(resolver, "GIT_CANDIDATES", (str(directory / "git"),))
    assert resolver.find_git() is None
    metadata = os.stat(directory / "git")
    assert not resolver._trusted_node(str(directory / "git"), metadata)


@posix
def test_a_symlinked_git_whose_target_is_not_root_owned_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "root"
    tree(root, "usr", "local", "bin")
    tree(root, "usr", "lib")
    user = tmp_path / "home" / "user"
    user.mkdir(parents=True)
    shutil.copy2(real_git(), user / "git")
    shutil.copy2(real_git(), root / "usr" / "lib" / "git")
    link = root / "usr" / "local" / "bin" / "git"
    os.symlink(user / "git", link)
    fake_root(monkeypatch, root)
    monkeypatch.setattr(resolver, "GIT_CANDIDATES", (str(link),))
    assert resolver.find_git() is None
    link.unlink()
    os.symlink(root / "usr" / "lib" / "git", link)  # the control: a target inside the trusted root passes
    assert resolver.find_git() == os.path.realpath(root / "usr" / "lib" / "git")


@pytest.mark.parametrize(
    "uid, mode, trusted",
    [
        (0, 0o100755, True),
        (0, 0o040755, True),
        (0, 0o100775, False),
        (0, 0o100757, False),
        (0, 0o040777, False),
        (0, 0o104755, False),
        (0, 0o102755, False),
        (0, 0o042755, False),
        (1000, 0o100755, False),
    ],
)
def test_only_root_owned_nodes_without_group_or_world_write_are_trusted(uid, mode, trusted):
    metadata = os.stat_result((mode, 1, 1, 1, uid, 0, 0, 0, 0, 0))
    assert resolver._trusted_node("/usr/bin/git", metadata) is trusted


@posix
def test_a_root_owned_git_in_an_untrusted_directory_is_refused(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    git_path = directory / "git"
    shutil.copy2(real_git(), git_path)
    real_node = resolver._trusted_node
    monkeypatch.setattr(resolver, "_trusted_node", lambda path, md: path == str(git_path) or real_node(path, md))
    monkeypatch.setattr(resolver, "GIT_CANDIDATES", (str(git_path),))
    assert resolver.find_git() is None  # the file passes, but the directories above it do not


def test_a_git_swapped_after_the_check_never_runs(world, monkeypatch):
    target = world.commit("target")
    root = world.root / "root"
    tree(root, "usr", "bin")
    git_path = root / "usr" / "bin" / "git"
    shutil.copy2(real_git(), git_path)
    fake_root(monkeypatch, root)
    monkeypatch.setattr(resolver, "GIT_CANDIDATES", (str(git_path),))
    swap = root / "usr" / "bin" / "swap"
    swap.write_text(f"#!/bin/sh\necho ran >> '{world.marker('swapped-git')}'\nexec '{real_git()}' \"$@\"\n")
    swap.chmod(0o755)
    real_run = resolver.proc.run_held
    swapped = []

    def swap_then_run(argv, fd, *args, **kwargs):
        if not swapped:
            os.replace(swap, git_path)  # after the check and the hold, before the first exec
            swapped.append(True)
        return real_run(argv, fd, *args, **kwargs)

    monkeypatch.setattr(resolver.proc, "run_held", swap_then_run)
    assert world.resolve(short(target)) == target
    assert world.ran() == []
    # A new run finds the swapped script, which is not a native file, so it is refused before it is held.
    assert world.resolve(short(target), run=resolver.Run()) is None
    assert world.ran() == []


# --- Fix round 2: unsupported platforms fail closed --------------------------------------


def spawn_spies(monkeypatch) -> list:
    spawned = []

    def refuse(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("the resolver must not start a process here")

    monkeypatch.setattr(resolver.proc, "run_held", refuse)
    monkeypatch.setattr(resolver.proc, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    return spawned


def bound_workspace(tmp_path: Path) -> config.Workspace:
    """A workspace bound to a directory under tmp_path, built directly.

    The `[[verify.workspace]]` parser accepts only POSIX roots, so it would refuse a Windows tmp_path before the
    resolver is reached. The fail-closed path never reads the workspace, so a directly built entry is enough here.
    """
    path = tmp_path / "workspace"
    path.mkdir()
    metadata = os.stat(path)
    return config.Workspace(
        scope=SCOPE,
        path=str(path),
        archive_projects=(),
        resolved=os.path.normcase(os.path.realpath(path)),
        identity=(metadata.st_dev, metadata.st_ino),
    )


def test_an_unsupported_platform_fails_closed_without_spawning(tmp_path, monkeypatch):
    workspace = bound_workspace(tmp_path)
    monkeypatch.setattr(resolver, "platform_supported", lambda: False)
    spawned = spawn_spies(monkeypatch)
    assert resolver.commit_resolver(workspace)("0123456789abc") is None
    assert resolver.unavailable_reason() == resolver.UNSUPPORTED_PLATFORM == "verify_resolver_unsupported_platform"
    assert spawned == []


@pytest.mark.skipif(sys.platform != "win32", reason="checks the real Windows fail-closed path")
def test_windows_fails_closed_without_spawning(tmp_path, monkeypatch):
    assert not resolver.platform_supported()
    workspace = bound_workspace(tmp_path)
    spawned = spawn_spies(monkeypatch)
    assert resolver.commit_resolver(workspace)("0123456789abc") is None
    assert resolver.unavailable_reason() == resolver.UNSUPPORTED_PLATFORM
    assert spawned == []


@posix
def test_a_git_replaced_between_open_and_check_is_not_held(tmp_path, monkeypatch):
    root = tmp_path / "root"
    tree(root, "usr", "bin")
    git_path = root / "usr" / "bin" / "git"
    shutil.copy2(real_git(), git_path)
    replacement = root / "usr" / "bin" / "replacement"
    shutil.copy2(real_git(), replacement)
    fake_root(monkeypatch, root)
    real_lstat = os.lstat
    replaced = []

    def lstat(path, *args, **kwargs):
        if str(path) == str(git_path) and not replaced:
            os.replace(replacement, git_path)  # the descriptor is already open on the original
            replaced.append(True)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(resolver.os, "lstat", lstat)
    assert resolver._hold(str(git_path)) is None
    assert replaced


# --- Fix round 3: reachability binds to the object ids a fetch proved -------------------


def attacker_commit(world: World, message: str) -> str:
    """An unpushed commit on top of the workspace's origin/main, made only in the workspace."""
    tree = git(world.workspace, "rev-parse", "refs/remotes/origin/main^{tree}")
    parent = git(world.workspace, "rev-parse", "refs/remotes/origin/main")
    return git(world.workspace, "commit-tree", tree, "-p", parent, "-m", message)


def test_a_ref_moved_after_the_proof_never_vouches_for_a_later_card(world):
    """Codex P1: card 2 in the same run must not resolve against a remote-tracking ref moved locally."""
    target = world.commit("target")
    run = resolver.Run()
    assert world.resolve(short(target), run=run) == target
    attacker = attacker_commit(world, "attacker")
    git(world.workspace, "update-ref", "refs/remotes/origin/main", attacker)
    assert world.resolve(short(attacker), run=run) is None
    # The run stopped trusting the repository, so even a genuine commit abstains for the rest of it.
    assert world.resolve(short(target), run=run) is None
    # A new run fetches and proves again: the remote resets the ref, and only the genuine commit resolves.
    fresh = resolver.Run()
    assert world.resolve(short(attacker), run=fresh) is None
    assert world.resolve(short(target), run=fresh) == target


def test_a_ref_moved_between_the_fetch_and_the_ancestry_check_is_ignored(world, monkeypatch):
    """Within one card, reachability uses the proven id even if the ref moves right after the fetch."""
    world.commit("target")
    attacker = attacker_commit(world, "attacker")
    real = resolver._git
    moved = []

    def move_after_fetch(tool, cwd, *args):
        result = real(tool, cwd, *args)
        if args[0] == "rev-parse" and any(arg.startswith("--disambiguate=") for arg in args) and not moved:
            git(world.workspace, "update-ref", "refs/remotes/origin/main", attacker)
            moved.append(True)
        return result

    monkeypatch.setattr(resolver, "_git", move_after_fetch)
    assert world.resolve(short(attacker)) is None
    assert moved


def test_the_run_caches_the_ids_each_fetch_proved(world):
    target = world.commit("target")
    run = resolver.Run()
    assert world.resolve(short(target), run=run) == target
    ((key, proven),) = run.proofs.items()
    assert key[1] == ("refs/remotes/origin/main",)
    assert proven == {"refs/remotes/origin/main": target}


# --- Fix round 3: setuid and setgid, and git's helpers ----------------------------------


@posix
@pytest.mark.parametrize("mode", [0o4755, 0o2755])
def test_a_setuid_or_setgid_git_is_refused(tmp_path, monkeypatch, mode):
    root = tmp_path / "root"
    git_path = tree(root, "usr", "bin") / "git"
    shutil.copy2(real_git(), git_path)
    git_path.chmod(0o755)
    fake_root(monkeypatch, root)
    monkeypatch.setattr(resolver, "GIT_CANDIDATES", (str(git_path),))
    assert resolver.find_git() == os.path.realpath(git_path)  # the control
    git_path.chmod(mode)
    if not os.stat(git_path).st_mode & mode & 0o6000:
        pytest.skip("this filesystem does not keep setuid or setgid bits for the test user")
    assert resolver.find_git() is None


def real_helper() -> str:
    found = os.path.realpath(os.path.join(git(Path.cwd(), "--exec-path"), "git-remote-https"))
    assert os.path.isfile(found)
    return found


def fake_exec_dir(world: World, monkeypatch) -> Path:
    """A fake root exec directory with trusted copies of git's https helpers, reported by `git --exec-path`."""
    root = world.root / "root"
    exec_dir = tree(root, "usr", "lib", "git-core")
    shutil.copy2(real_helper(), exec_dir / "git-remote-http")
    (exec_dir / "git-remote-http").chmod(0o755)
    os.symlink("git-remote-http", exec_dir / "git-remote-https")
    fake_root(monkeypatch, root)
    real = resolver._git

    def reported(tool, cwd, *args):
        if args == ("--exec-path",):
            return resolver._Result(0, f"{exec_dir}\n")
        world.calls.append(args[0])
        return real(tool, cwd, *args)

    world.calls = []
    monkeypatch.setattr(resolver, "_git", reported)
    return exec_dir


def test_trusted_helpers_in_the_exec_dir_pass(world, monkeypatch):
    target = world.commit("target")
    fake_exec_dir(world, monkeypatch)
    assert world.resolve(short(target)) == target
    assert "fetch" in world.calls


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o4755])
def test_a_writable_or_setuid_helper_is_refused_before_any_fetch(world, monkeypatch, mode):
    target = world.commit("target")
    helper = fake_exec_dir(world, monkeypatch) / "git-remote-http"
    helper.chmod(mode)
    assert world.resolve(short(target)) is None
    assert "fetch" not in world.calls


def test_a_helper_linked_to_a_file_root_does_not_own_is_refused_before_any_fetch(world, monkeypatch):
    target = world.commit("target")
    exec_dir = fake_exec_dir(world, monkeypatch)
    user = world.root / "home" / "user"
    user.mkdir(parents=True)
    shutil.copy2(real_helper(), user / "git-remote-https")
    (exec_dir / "git-remote-https").unlink()
    os.symlink(user / "git-remote-https", exec_dir / "git-remote-https")
    assert world.resolve(short(target)) is None
    assert "fetch" not in world.calls


def test_a_missing_https_helper_is_refused_before_any_fetch(world, monkeypatch):
    target = world.commit("target")
    exec_dir = fake_exec_dir(world, monkeypatch)
    (exec_dir / "git-remote-https").unlink()
    assert world.resolve(short(target)) is None
    assert "fetch" not in world.calls


def test_a_helper_that_is_not_a_native_file_is_refused_before_any_fetch(world, monkeypatch):
    target = world.commit("target")
    exec_dir = fake_exec_dir(world, monkeypatch)
    script = exec_dir / "git-remote-http"
    script.unlink()
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    assert world.resolve(short(target)) is None
    assert "fetch" not in world.calls
