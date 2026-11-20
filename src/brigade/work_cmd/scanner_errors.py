"""Error messages for scanner import reads."""

from pathlib import Path


def import_read_error(scanner_id: object, import_path: Path, error: OSError | UnicodeDecodeError) -> str:
    """Distinguish missing imports from read and decode failures."""
    if isinstance(error, FileNotFoundError):
        return f"{scanner_id}: import file not found: {import_path}"
    if isinstance(error, UnicodeDecodeError):
        return f"{scanner_id}: import file is not valid UTF-8: {import_path}: {error}"
    return f"{scanner_id}: cannot read import file: {import_path}: {error}"


def group_incomplete_error() -> str:
    """Name a scanner process group that outlived its run instead of its import file."""
    return (
        "scanner descendants still running after child exit; process group terminated, "
        "so the import file was not read (retry once no scanner descendant is running)"
    )
