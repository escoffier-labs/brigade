"""Platform boundary for scanner descriptor-relative operations."""

from __future__ import annotations

import os

SCANNER_DESCRIPTOR_OPERATIONS_UNAVAILABLE = "scanner descriptor-relative operations are unavailable on Windows"
_platform_name = os.name


class ScannerDescriptorOperationsUnavailable(OSError):
    """Raised before scanners touch descriptor-protected state on Windows."""


def require_scanner_descriptor_operations() -> None:
    """Reject Windows before scanner operations can mutate protected state."""
    if _platform_name == "nt":
        raise ScannerDescriptorOperationsUnavailable(SCANNER_DESCRIPTOR_OPERATIONS_UNAVAILABLE)


def scanner_import_read_primitives_available() -> bool:
    """Return whether scanner imports can be opened without pathname races."""
    return (
        _platform_name == "posix"
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NONBLOCK", 0))
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
    )


def scanner_inbox_open_primitives_available() -> bool:
    """Return whether every inbox path component can remain no-follow."""
    return (
        _platform_name == "posix"
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and bool(getattr(os, "O_DIRECTORY", 0))
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
    )


def scanner_inbox_has_single_link(metadata: os.stat_result) -> bool:
    """Return whether metadata proves the inbox has exactly one directory entry."""
    try:
        link_count = metadata.st_nlink
    except AttributeError:
        return False
    return type(link_count) is int and link_count == 1
