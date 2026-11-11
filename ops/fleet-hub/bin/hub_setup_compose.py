"""Setup-owned Compose mount and file selection, independent of operator overrides."""

import os
import stat
import sys
from pathlib import Path

HEADER = b"# Written by bin/hub-setup.py"
MANAGED = "compose.hybrid.yaml"
OPERATOR = "compose.override.yaml"
COMPOSE_FILE = "compose.yaml:compose.override.yaml:compose.hybrid.yaml"
STUB = b"# Operator-owned. Setup never edits this file.\nservices: {}\n"
RUNBOOK = "docs/runbooks/fleet-hub-dogfood.md#switch-retrieval-modes"


def unquote(value: str) -> str:
    """Unwrap matching dotenv quotes without interpreting a Compose file list."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def document(backend: str, token: str) -> bytes:
    """Exact legacy output, including its final newline (also the new mount)."""
    return (
        f"# Written by bin/hub-setup.py for the {backend} embedder backend.\n"
        "services:\n  hub:\n    secrets:\n      - admin_token\n      - findings_key\n      - embed_token\n"
        f"secrets:\n  embed_token:\n    file: ./secrets/{token}\n"
    ).encode()


def legacy_owned(data: bytes, tokens: dict[str, str]) -> bool:
    return any(
        data in (generated, generated.replace(b"\n", b"\r\n"))
        for backend, token in tokens.items()
        for generated in [document(backend, token)]
    )


def preflight(project: Path, retrieval: str, tokens: dict[str, str], env: dict[str, str]) -> None:
    """Refuse ambiguous ownership before the transaction makes its first write."""
    override = read_file(project / OPERATOR, OPERATOR)
    managed = read_file(project / MANAGED, MANAGED)
    if override is not None:
        old = override[0]
        if old.startswith(HEADER) and not legacy_owned(old, tokens):
            raise ValueError(
                "mixed legacy compose.override.yaml: remove the embed_token lines and the header, "
                f"or move the file aside, then rerun. See {RUNBOOK}"
            )
    if managed is not None and not managed[0].startswith(HEADER):
        raise ValueError("compose.hybrid.yaml is not setup-owned; move it aside and rerun")
    if retrieval == "hybrid" and (
        ("COMPOSE_FILE" in env and unquote(env["COMPOSE_FILE"]) != COMPOSE_FILE)
        or ("COMPOSE_PATH_SEPARATOR" in env and not setup_selection(env))
    ):
        raise ValueError(
            "operator Compose selection: setup cannot change it. Add compose.hybrid.yaml to your selection "
            "using your separator, or use the setup default:\n"
            f"COMPOSE_FILE={COMPOSE_FILE}\nCOMPOSE_PATH_SEPARATOR=:\nSee {RUNBOOK}"
        )


def compose_override(
    project: Path, retrieval: str, backend: str | None, tokens: dict[str, str], env: dict[str, str]
) -> None:
    """Never parse operator lists or edit an existing operator override."""
    preflight(project, retrieval, tokens, env)
    managed = project / MANAGED
    override = project / OPERATOR
    saved = read_file(override, OPERATOR)
    old = saved[0] if saved is not None else b""
    if retrieval == "hybrid":
        managed.write_bytes(document(str(backend), tokens[str(backend)]))
        if legacy_owned(old, tokens):
            override.unlink()
        if not override.exists():
            override.write_bytes(STUB)
        env["COMPOSE_FILE"] = COMPOSE_FILE
        env["COMPOSE_PATH_SEPARATOR"] = ":"
    else:
        if managed.exists():
            managed.unlink()
        if legacy_owned(old, tokens):
            override.unlink()
        if setup_selection(env):
            env.pop("COMPOSE_FILE")
            env.pop("COMPOSE_PATH_SEPARATOR", None)
        elif "COMPOSE_FILE" in env or "COMPOSE_PATH_SEPARATOR" in env:
            print(
                "warning: operator COMPOSE_FILE and COMPOSE_PATH_SEPARATOR left unchanged. Remove any compose.hybrid.yaml entry yourself.",
                file=sys.stderr,
            )


def setup_selection(env: dict[str, str]) -> bool:
    """Recognize only the setup list with its explicit or legacy unset separator."""
    return (
        unquote(env.get("COMPOSE_FILE", "")) == COMPOSE_FILE and unquote(env.get("COMPOSE_PATH_SEPARATOR", ":")) == ":"
    )


def read_file(path: Path, label: str) -> tuple[bytes, int] | None:
    """Read regular files only, without following links or blocking on FIFOs."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"{label} is a symlink; move it aside and rerun")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} is not a regular file; move it aside and rerun")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{label} is not a regular file; move it aside and rerun")
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks), stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)
