"""Per-machine Brigade identity stored at ``.brigade/node.toml``.

The file is workspace-local and gitignored. ``node_id`` is a stable uuid4
generated once and never rewritten if the file already exists. Hostname and
platform are snapshots from the first init; they are not a security identity.
"""

from __future__ import annotations

import json
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from . import localio, toml_compat as tomllib

NODE_REL_PATH = Path(".brigade") / "node.toml"
SHORT_ID_LEN = 8


class NodeIdentityError(ValueError):
    """Raised when an existing node file is present but unreadable."""


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    hostname: str
    roles: tuple[str, ...]
    platform: str

    @property
    def short_id(self) -> str:
        return short_node_id(self.node_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "hostname": self.hostname,
            "roles": list(self.roles),
            "platform": self.platform,
            "short_id": self.short_id,
        }


def node_path(target: Path) -> Path:
    return target.expanduser() / NODE_REL_PATH


def short_node_id(node_id: str) -> str:
    compact = node_id.replace("-", "").lower()
    return compact[:SHORT_ID_LEN]


def _format_node_toml(identity: NodeIdentity) -> str:
    roles = ", ".join(json.dumps(role) for role in identity.roles)
    return (
        "# Per-machine Brigade identity. Do not commit.\n"
        f"node_id = {json.dumps(identity.node_id)}\n"
        f"hostname = {json.dumps(identity.hostname)}\n"
        f"roles = [{roles}]\n"
        f"platform = {json.dumps(identity.platform)}\n"
    )


def _parse_roles(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if not isinstance(value, list):
        raise NodeIdentityError("roles must be a list of strings")
    roles: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise NodeIdentityError("roles must be a list of strings")
        roles.append(item.strip())
    return tuple(roles)


def _parse_node_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NodeIdentityError("node_id must be a uuid4 string")
    raw = value.strip()
    try:
        parsed = UUID(raw)
    except ValueError as exc:
        raise NodeIdentityError("node_id must be a uuid4 string") from exc
    if parsed.version != 4:
        raise NodeIdentityError("node_id must be a uuid4 string")
    return str(parsed)


def _parse_identity(payload: dict[str, Any]) -> NodeIdentity:
    hostname = payload.get("hostname")
    platform = payload.get("platform")
    if not isinstance(hostname, str) or not hostname.strip():
        raise NodeIdentityError("hostname must be a non-empty string")
    if not isinstance(platform, str) or not platform.strip():
        raise NodeIdentityError("platform must be a non-empty string")
    return NodeIdentity(
        node_id=_parse_node_id(payload.get("node_id")),
        hostname=hostname.strip(),
        roles=_parse_roles(payload.get("roles")),
        platform=platform.strip(),
    )


def load_identity(target: Path) -> NodeIdentity | None:
    """Return the persisted identity, or None when the file is absent.

    Raises ``NodeIdentityError`` when the file exists but cannot be parsed.
    Never rewrites the file.
    """
    path = node_path(target)
    try:
        if not path.is_file():
            return None
        text = path.read_text()
    except OSError as exc:
        raise NodeIdentityError(f"could not read {path}: {exc}") from exc
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise NodeIdentityError(f"invalid node identity file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise NodeIdentityError(f"invalid node identity file {path}")
    return _parse_identity(payload)


def _new_identity(*, hostname: str | None = None, platform: str | None = None) -> NodeIdentity:
    raw_host = hostname if hostname is not None else socket.gethostname()
    host = (raw_host.split(".")[0] or "local").strip() or "local"
    return NodeIdentity(
        node_id=str(uuid4()),
        hostname=host,
        roles=(),
        platform=(platform if platform is not None else sys.platform),
    )


def ensure_identity(target: Path) -> NodeIdentity:
    """Load ``.brigade/node.toml``, creating it once if missing.

    If the file already exists, the stored ``node_id`` is returned unchanged
    even when hostname or platform have drifted. A corrupt existing file is
    never overwritten.
    """
    existing = load_identity(target)
    if existing is not None:
        return existing
    path = node_path(target)
    identity = _new_identity()
    try:
        localio.write_text_exclusive(path, _format_node_toml(identity))
    except FileExistsError:
        loaded = load_identity(target)
        if loaded is None:
            raise NodeIdentityError(f"node identity file appeared then vanished: {path}") from None
        return loaded
    except OSError as exc:
        raise NodeIdentityError(f"could not write {path}: {exc}") from exc
    return identity


def infer_workspace_from_runs_dir(runs_dir: Path) -> Path:
    """Best-effort workspace for a runs directory.

    ``<workspace>/.brigade/runs`` maps to ``<workspace>``. Any other layout
    falls back to ``runs_dir`` itself so identity still has a place to live.
    """
    try:
        resolved = runs_dir.expanduser().resolve()
    except OSError:
        resolved = runs_dir.expanduser()
    if resolved.name == "runs" and resolved.parent.name == ".brigade":
        return resolved.parent.parent
    return resolved


def run(*, target: Path, json_output: bool = False) -> int:
    """Init the node file if needed and print the identity."""
    try:
        identity = ensure_identity(target)
    except NodeIdentityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(identity.to_dict(), indent=2, sort_keys=True))
        return 0
    print(f"node_id: {identity.node_id}")
    print(f"short_id: {identity.short_id}")
    print(f"hostname: {identity.hostname}")
    print(f"platform: {identity.platform}")
    print(f"roles: {', '.join(identity.roles) if identity.roles else '(none)'}")
    return 0
