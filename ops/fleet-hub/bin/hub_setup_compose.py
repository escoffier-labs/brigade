"""Setup-owned Compose mount and file selection, independent of operator overrides."""

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
    return data in (document(backend, token) for backend, token in tokens.items())


def preflight(project: Path, retrieval: str, tokens: dict[str, str], env: dict[str, str]) -> None:
    """Refuse ambiguous ownership before the transaction makes its first write."""
    override = project / OPERATOR
    if override.exists():
        old = override.read_bytes()
        if old.startswith(HEADER) and not legacy_owned(old, tokens):
            raise ValueError(
                "mixed legacy compose.override.yaml: remove the embed_token lines and the header, "
                f"or move the file aside, then rerun. See {RUNBOOK}"
            )
    if retrieval == "hybrid" and (
        ("COMPOSE_FILE" in env and unquote(env["COMPOSE_FILE"]) != COMPOSE_FILE)
        or unquote(env.get("COMPOSE_PATH_SEPARATOR", ":")) != ":"
    ):
        raise ValueError(
            "operator Compose selection: setup cannot change it. Add compose.hybrid.yaml to your selection "
            "using your separator, or use the setup default with COMPOSE_PATH_SEPARATOR unset:\n"
            f"COMPOSE_FILE={COMPOSE_FILE}\nSee {RUNBOOK}"
        )


def compose_override(
    project: Path, retrieval: str, backend: str | None, tokens: dict[str, str], env: dict[str, str]
) -> None:
    """Never parse operator lists or edit an existing operator override."""
    preflight(project, retrieval, tokens, env)
    managed = project / MANAGED
    override = project / OPERATOR
    old = override.read_bytes() if override.exists() else b""
    if managed.exists() and not managed.read_bytes().startswith(HEADER):
        raise ValueError("compose.hybrid.yaml is not setup-owned; move it aside and rerun")
    if retrieval == "hybrid":
        managed.write_bytes(document(str(backend), tokens[str(backend)]))
        if legacy_owned(old, tokens):
            override.unlink()
        if not override.exists():
            override.write_bytes(STUB)
        env["COMPOSE_FILE"] = COMPOSE_FILE
    else:
        if managed.exists():
            managed.unlink()
        if legacy_owned(old, tokens):
            override.unlink()
        if unquote(env.get("COMPOSE_FILE", "")) == COMPOSE_FILE:
            env.pop("COMPOSE_FILE")
        elif "COMPOSE_FILE" in env:
            print(
                "warning: operator COMPOSE_FILE left unchanged. Remove any compose.hybrid.yaml entry yourself.",
                file=sys.stderr,
            )
