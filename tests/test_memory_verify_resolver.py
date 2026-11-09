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
import hashlib
import http.server
import os
import shutil
import socket
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

from brigade import memory_verify_config as config
from brigade import memory_verify_contract as vc
from brigade import memory_verify_resolver as resolver

posix = pytest.mark.skipif(os.name != "posix", reason="scratch HTTPS remotes and marker scripts need POSIX")
pytestmark = [
    posix,
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
]

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
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k.lower() not in resolver.CURL_VARIABLES}
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


@pytest.fixture
def world(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("GIT_") or name.lower() in resolver.CURL_VARIABLES:
            monkeypatch.delenv(name)
    built = World(tmp_path)
    monkeypatch.setattr(resolver, "_TEST_CA_FILE", str(built.ca))
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
        killed = []
        real_kill = resolver._kill
        monkeypatch.setattr(resolver, "_kill", lambda process: (killed.append(process.pid), real_kill(process)))
        assert world.resolve(short(target)) is None
        assert stalled.connections >= 1
        assert killed  # the resolver's own timeout ended the fetch, not curl's much longer connect timeout
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


def test_git_missing_from_path_is_refused(world, monkeypatch):
    target = world.commit("target")
    empty = world.root / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert world.resolve(short(target)) is None


def test_ls_remote_proves_freshness_before_git_2_41(world, monkeypatch):
    monkeypatch.setattr(resolver, "PORCELAIN_VERSION", (99, 0))
    calls = []
    real = resolver._git

    def spy(cwd, *args):
        calls.append(args[0])
        return real(cwd, *args)

    monkeypatch.setattr(resolver, "_git", spy)
    target = world.commit("target")
    assert world.resolve(short(target)) == target
    assert "ls-remote" in calls


def test_ls_remote_mismatch_is_refused_before_git_2_41(world, monkeypatch):
    monkeypatch.setattr(resolver, "PORCELAIN_VERSION", (99, 0))
    target = world.commit("target")
    real = resolver._git

    def moved(cwd, *args):
        result = real(cwd, *args)
        if args[0] == "ls-remote" and result is not None:
            return resolver._Result(result.code, result.out.replace(target, world.base))
        return result

    monkeypatch.setattr(resolver, "_git", moved)
    assert world.resolve(short(target)) is None


def test_a_fetch_whose_porcelain_does_not_prove_the_ref_is_refused(world, monkeypatch):
    target = world.commit("target")
    real = resolver._git

    def silent(cwd, *args):
        result = real(cwd, *args)
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
    monkeypatch.setattr(resolver, "_TEST_CA_FILE", None)
    for name in ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"):
        monkeypatch.setenv(name, str(world.ca))
    # Without the test hook the scratch CA is not trusted, so a variable that leaked through would make this pass.
    assert world.resolve(short(target)) is None


# --- The wrapper itself ------------------------------------------------------------------


def test_the_environment_is_rebuilt_without_git_or_curl_variables():
    inherited = {
        "PATH": "/usr/bin",
        "GIT_DIR": "x",
        "GIT_CONFIG_PARAMETERS": "x",
        "GIT_CONFIG_COUNT": "1",
        "git_trace": "1",
        "HTTPS_PROXY": "x",
        "http_proxy": "x",
        "All_Proxy": "x",
        "NO_PROXY": "x",
        "CURL_CA_BUNDLE": "x",
        "SSL_CERT_FILE": "x",
        "SSL_CERT_DIR": "x",
    }
    env = resolver.git_environment(inherited)
    assert env == {
        "PATH": "/usr/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def test_every_call_carries_the_overrides(monkeypatch):
    monkeypatch.setattr(resolver, "_TEST_CA_FILE", None)
    argv = resolver.git_argv(["status"])
    assert argv[:3] == ["git", "--no-pager", "--no-replace-objects"]
    assert argv[-1] == "status"
    overrides = [argv[index + 1] for index, value in enumerate(argv) if value == "-c"]
    assert overrides == list(resolver.OVERRIDES)
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
