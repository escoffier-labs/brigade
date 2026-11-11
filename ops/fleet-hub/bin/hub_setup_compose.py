"""Setup-owned Compose mount and file selection, independent of operator overrides."""

from pathlib import Path

HEADER = b"# Written by bin/hub-setup.py"
MANAGED = "compose.hybrid.yaml"
OPERATOR = "compose.override.yaml"


def compose_override(
    project: Path, retrieval: str, backend: str | None, tokens: dict[str, str], env: dict[str, str]
) -> None:
    """Change only setup-owned files and its entry in the Compose file list.

    COMPOSE_FILE disables Compose's implicit override discovery. Materialize
    those defaults when adding our mount, and keep any explicit operator list.
    """
    managed = project / MANAGED
    legacy = project / OPERATOR
    old = legacy.read_bytes() if legacy.exists() else b""
    owned_legacy = old.startswith(HEADER)
    if managed.exists() and not managed.read_bytes().startswith(HEADER):
        raise ValueError("compose.hybrid.yaml is not setup-owned; move it aside and rerun")
    separator = env.get("COMPOSE_PATH_SEPARATOR", ":")
    files = env.get("COMPOSE_FILE", "").strip("\"'").split(separator)
    files = [name for name in files if name and name not in (MANAGED, "./" + MANAGED)]
    if owned_legacy:
        files = [name for name in files if name not in (OPERATOR, "./" + OPERATOR)]
    if retrieval == "hybrid":
        body = (
            f"# Written by bin/hub-setup.py for the {backend} embedder backend.\n"
            "services:\n  hub:\n    secrets:\n      - admin_token\n      - findings_key\n      - embed_token\n"
            f"secrets:\n  embed_token:\n    file: ./secrets/{tokens[str(backend)]}\n"
        ).encode()
        # Move all legacy content, updating only the mount if the backend changed.
        if owned_legacy:
            body = old
            for token in tokens.values():
                body = body.replace(
                    f"file: ./secrets/{token}\n".encode(), f"file: ./secrets/{tokens[str(backend)]}\n".encode()
                )
        managed.write_bytes(body)
        if owned_legacy:
            legacy.unlink()
        if not files:
            files = ["compose.yaml"]
        if legacy.exists() and not any((project / name).resolve() == legacy.resolve() for name in files):
            files.append(OPERATOR)
        files.append(MANAGED)
    else:
        if managed.exists():
            managed.unlink()
        if owned_legacy:
            legacy.unlink()
    defaults = ["compose.yaml"] + ([OPERATOR] if legacy.exists() else [])
    if files and (retrieval == "hybrid" or files != defaults):
        env["COMPOSE_FILE"] = separator.join(files)
    else:
        env.pop("COMPOSE_FILE", None)
