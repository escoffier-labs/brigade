from tests.dot_support import requires_local_state

import json
import os
from pathlib import Path

import pytest

from brigade.dot.contracts import DotError
from brigade.dot.runtime_config import (
    RUNTIME_SCHEMA,
    AuthConfig,
    BinPaths,
    HubConfig,
    OwnerAuthorization,
    PathsConfig,
    Runtime,
    load_runtime,
    parse_runtime,
    validate_runtime_reference,
)


def _document() -> dict[str, object]:
    return {
        "schema": RUNTIME_SCHEMA,
        "auth": {
            "issuer": "https://team.example.test",
            "team_domain": "team.example.test",
            "audience": "a" * 64,
            "approved_email": "dot@example.test",
            "resource": "https://dot.example.test/mcp",
        },
        "paths": {
            "state_dir": str(Path("/srv/dot/state").absolute()),
            "workspace": str(Path("/srv/dot/workspace").absolute()),
            "fleet_config": str(Path("/srv/dot/config.toml").absolute()),
            "bin": {
                "t3_fleet": str(Path("/usr/bin/t3-fleet").absolute()),
                "brigade": str(Path("/usr/bin/brigade").absolute()),
                "git": str(Path("/usr/bin/git").absolute()),
                "gh": str(Path("/usr/bin/gh").absolute()),
            },
        },
        "hub": {
            "principal": "agent:dot",
            "node_id": "00000000-0000-4000-8000-000000000001",
            "message_token_file": str(Path("/srv/dot/secrets/message-token").absolute()),
            "poll_seconds": 30,
            "recipients": {
                "review": "00000000-0000-4000-8000-000000000002",
                "ops": "agent:ops-bot",
            },
        },
        "dispatch_enabled": True,
        "events_enabled": False,
        "digest_pr_repo": "owner/repo",
        "callback_hosts": ["connectors.api.openai.com"],
        "notify_command": [str(Path("/usr/bin/notifier").absolute()), "--hook", "custom"],
        "owner_authorization": {"version": "1", "text": "Owner approved Dot automation."},
    }


def _write_private(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)


def test_parse_runtime_builds_frozen_config_dataclasses() -> None:
    runtime = parse_runtime(_document())

    assert isinstance(runtime, Runtime)
    assert runtime.auth == AuthConfig(
        issuer="https://team.example.test",
        team_domain="team.example.test",
        audience="a" * 64,
        approved_email="dot@example.test",
        resource="https://dot.example.test/mcp",
    )
    assert runtime.paths == PathsConfig(
        state_dir=Path("/srv/dot/state").absolute(),
        workspace=Path("/srv/dot/workspace").absolute(),
        fleet_config=Path("/srv/dot/config.toml").absolute(),
        bin=BinPaths(
            t3_fleet=str(Path("/usr/bin/t3-fleet").absolute()),
            brigade=str(Path("/usr/bin/brigade").absolute()),
            git=str(Path("/usr/bin/git").absolute()),
            gh=str(Path("/usr/bin/gh").absolute()),
            octopool=None,
        ),
    )
    assert runtime.hub == HubConfig(
        principal="agent:dot",
        node_id="00000000-0000-4000-8000-000000000001",
        message_token_file=Path("/srv/dot/secrets/message-token").absolute(),
        poll_seconds=30,
        recipients={
            "review": "00000000-0000-4000-8000-000000000002",
            "ops": "agent:ops-bot",
        },
    )
    assert runtime.dispatch_enabled is True
    assert runtime.events_enabled is False
    assert runtime.digest_pr_repo == "owner/repo"
    assert runtime.callback_hosts == ("connectors.api.openai.com",)
    assert runtime.notify_command == (str(Path("/usr/bin/notifier").absolute()), "--hook", "custom")
    assert runtime.owner_authorization == OwnerAuthorization("1", "Owner approved Dot automation.")
    assert runtime.paths.bin.octopool is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(extra=True),
        lambda data: data["auth"].update(extra=True),
        lambda data: data["auth"].pop("approved_email"),
        lambda data: data["paths"].update(state_dir="relative/state"),
        lambda data: data["paths"]["bin"].update(git="relative/git"),
        lambda data: data["auth"].update(audience="A" * 64),
        lambda data: data["hub"].update(poll_seconds=9),
        lambda data: data["hub"].update(poll_seconds=301),
        lambda data: data["hub"].update(poll_seconds=True),
        lambda data: data["hub"].update(recipients={str(i): "agent:ops" for i in range(17)}),
        lambda data: data["hub"].update(recipients={"bad": "not-a-recipient"}),
        lambda data: data["hub"].update(principal="dot"),
        lambda data: data.update(callback_hosts=[]),
    ],
    ids=[
        "unknown-top-level-key",
        "unknown-nested-key",
        "missing-approved-email",
        "relative-path",
        "relative-bin-path",
        "invalid-audience",
        "poll-too-low",
        "poll-too-high",
        "bool-poll-seconds",
        "too-many-recipients",
        "bad-recipient-value",
        "invalid-principal",
        "empty-callback-hosts",
    ],
)
def test_parse_runtime_rejects_invalid_documents(mutate) -> None:
    data = _document()
    mutate(data)

    with pytest.raises(DotError) as error:
        parse_runtime(data)

    assert error.value.code == "invalid_config"


@requires_local_state
def test_load_runtime_requires_owner_only_regular_file(dot_private_dir: Path) -> None:
    insecure = dot_private_dir / "insecure.json"
    _write_private(insecure, json.dumps(_document()).encode(), mode=0o644)

    with pytest.raises(DotError) as error:
        load_runtime(insecure)

    assert error.value.code == "invalid_config"


@requires_local_state
def test_load_runtime_rejects_symlink(dot_private_dir: Path) -> None:
    target = dot_private_dir / "target.json"
    _write_private(target, json.dumps(_document()).encode())
    link = dot_private_dir / "runtime.json"
    link.symlink_to(target)

    with pytest.raises(DotError) as error:
        load_runtime(link)

    assert error.value.code == "invalid_config"


def test_validate_runtime_reference_requires_absolute_path() -> None:
    with pytest.raises(DotError) as error:
        validate_runtime_reference("relative/runtime.json")

    assert error.value.code == "invalid_config"


def test_runtime_repr_redacts_authorization_text_and_token_path() -> None:
    runtime = parse_runtime(_document())

    assert "Owner approved Dot automation." not in repr(runtime)
    assert str(Path("/srv/dot/secrets/message-token").absolute()) not in repr(runtime)


@pytest.mark.parametrize("paths", [[], ["/srv/fleet"], None, "path", ["relative"]])
def test_removed_writable_paths_key_is_refused(paths):
    data = _document()
    data["writable_paths"] = paths
    with pytest.raises(DotError, match="^invalid_config$"):
        parse_runtime(data)


@pytest.mark.parametrize(
    "issuer", ["https://other.example.test", "https://team.example.test/", "https://team.example.test:443"]
)
def test_issuer_must_equal_team_domain_origin(issuer):
    data = _document()
    data["auth"]["issuer"] = issuer
    with pytest.raises(DotError, match="^invalid_config$"):
        parse_runtime(data)


@pytest.mark.parametrize("version", ["a" * 65, "has\nnewline", "é", "\x7f"])
def test_authorization_version_is_bounded_printable_ascii(version):
    data = _document()
    data["owner_authorization"]["version"] = version
    with pytest.raises(DotError, match="^invalid_config$"):
        parse_runtime(data)


def test_authorization_text_uses_bridge_byte_limit():
    data = _document()
    data["owner_authorization"] = {"version": "v" * 64, "text": "é" * 2048}
    assert parse_runtime(data).owner_authorization.text == "é" * 2048
    data["owner_authorization"]["text"] += "é"
    with pytest.raises(DotError, match="^invalid_config$"):
        parse_runtime(data)


@requires_local_state
@pytest.mark.parametrize("unsafe", ["read-only", "executable", "hardlink", "parent", "ancestor", "symlink-parent"])
def test_runtime_requires_exact_private_file_and_directory_walk(dot_private_dir, unsafe):
    root = dot_private_dir.parent
    parent = root / "config"
    parent.mkdir(mode=0o700)
    path = parent / "runtime.json"
    _write_private(path, json.dumps(_document()).encode())
    assert load_runtime(path) == parse_runtime(_document())
    if unsafe == "read-only":
        path.chmod(0o400)
    elif unsafe == "executable":
        path.chmod(0o700)
    elif unsafe == "hardlink":
        os.link(path, parent / "alias.json")
    elif unsafe == "parent":
        parent.chmod(0o750)
    elif unsafe == "ancestor":
        root.chmod(0o770)
    else:
        link = root / "alias"
        link.symlink_to(parent, target_is_directory=True)
        path = link / path.name
    try:
        with pytest.raises(DotError, match="^invalid_config$"):
            load_runtime(path)
    finally:
        root.chmod(0o700)
        parent.chmod(0o700)
