"""Error messages and settled-read policy for scanner import reads."""

from pathlib import Path
from typing import Any, Callable, TypeVar

_ReadResult = TypeVar("_ReadResult")


class ImportChangedDuringRead(OSError):
    """The import identity changed between the start and end of one read."""

    def __init__(self) -> None:
        super().__init__("scanner import file changed while reading")


def import_read_error(scanner_id: object, import_path: Path, error: OSError | UnicodeDecodeError) -> str:
    """Distinguish missing imports from read and decode failures."""
    if isinstance(error, FileNotFoundError):
        return f"{scanner_id}: import file not found: {import_path}"
    if isinstance(error, UnicodeDecodeError):
        return f"{scanner_id}: import file is not valid UTF-8: {import_path}: {error}"
    return f"{scanner_id}: cannot read import file: {import_path}: {error}"


def read_import_settled(
    read: Callable[..., _ReadResult], *args: Any
) -> tuple[_ReadResult | None, OSError | UnicodeDecodeError | None]:
    """Read once, then once more only when the identity check trips mid-read.

    The scanner process group is reaped before import validation, so a late
    write can settle by the second read. Every check runs again on the retry.
    A missing file, a decode failure and any validation refusal are reported
    on the first read without a retry.
    """
    try:
        return read(*args), None
    except ImportChangedDuringRead:
        pass
    except (OSError, UnicodeDecodeError) as exc:
        return None, exc
    try:
        return read(*args), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, exc
