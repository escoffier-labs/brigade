"""Parse and format node-namespaced Brigade run ids.

New run directories are ``{short_id}.{YYYYMMDD}-{HHMMSS}-{8hex}``. Legacy
un-namespaced ids (``YYYYMMDD-HHMMSS-{8hex}``) stay valid and are treated as
belonging to the local node.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import node as node_mod

_LEGACY_RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$", re.IGNORECASE)
_NAMESPACED_RUN_ID_RE = re.compile(
    rf"^([0-9a-f]{{{node_mod.SHORT_ID_LEN}}})\.(\d{{8}}-\d{{6}}-[0-9a-f]{{8}})$",
    re.IGNORECASE,
)


def format_run_id(node_short: str, local_id: str) -> str:
    """Return a namespaced run id ``{short}.{local}``."""
    return f"{node_short}.{local_id}"


def parse_run_id(run_id: str) -> tuple[str | None, str]:
    """Split a run id into ``(node_short, local_id)``.

    Un-namespaced legacy ids return ``(None, run_id)``. Treat ``None`` as
    belonging to the local node.
    """
    if not isinstance(run_id, str) or not run_id:
        return None, run_id
    match = _NAMESPACED_RUN_ID_RE.fullmatch(run_id)
    if match is not None:
        return match.group(1).lower(), match.group(2)
    return None, run_id


def is_legacy_run_id(run_id: str) -> bool:
    return bool(_LEGACY_RUN_ID_RE.fullmatch(run_id))


def is_namespaced_run_id(run_id: str) -> bool:
    return bool(_NAMESPACED_RUN_ID_RE.fullmatch(run_id))


def lookup_names(run_id: str, *, local_short: str | None) -> list[str]:
    """Directory names to try when resolving *run_id* on this node.

    An un-namespaced query also tries the locally-namespaced form. A
    namespaced query whose short id is the local node also tries the
    un-namespaced suffix so a leftover legacy directory still resolves.
    """
    names: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    _add(run_id)
    node_short, local_id = parse_run_id(run_id)
    if local_short:
        if node_short is None:
            _add(format_run_id(local_short, run_id))
            if local_id != run_id:
                _add(format_run_id(local_short, local_id))
        elif node_short == local_short.lower():
            _add(local_id)
    return names


def lookup_names_for_workspace(run_id: str, workspace: Path) -> list[str]:
    """``lookup_names`` using the persisted local short id when present.

    Read paths must not create ``node.toml``; minting and ``brigade node``
    are the writers.
    """
    try:
        identity = node_mod.load_identity(workspace)
    except node_mod.NodeIdentityError:
        return lookup_names(run_id, local_short=None)
    if identity is None:
        return lookup_names(run_id, local_short=None)
    return lookup_names(run_id, local_short=identity.short_id)


def run_id_matches(stored: str, query: str, *, local_short: str | None) -> bool:
    """True when *query* refers to the stored directory name on this node."""
    if stored == query:
        return True
    if stored.startswith(query):
        return True
    stored_node, stored_local = parse_run_id(stored)
    query_node, query_local = parse_run_id(query)
    if local_short:
        if stored_node is None:
            stored_node = local_short
        if query_node is None:
            query_node = local_short
    if stored_node is not None:
        stored_node = stored_node.lower()
    if query_node is not None:
        query_node = query_node.lower()
    if stored_node != query_node:
        return False
    return stored_local == query_local or stored_local.startswith(query_local) or stored.startswith(query)


def mint_local_run_id(now_stamp: str, suffix: str) -> str:
    return f"{now_stamp}-{suffix}"
