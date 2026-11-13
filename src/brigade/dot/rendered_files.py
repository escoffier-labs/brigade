"""Safe rendered unit writes shared by connector profiles."""

from pathlib import Path
from brigade import grokbot_ops


def _write_rendered(path: Path, rendered: str, *, force: bool) -> Path:
    try:
        existing = grokbot_ops._read_regular_text(path)
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        if not force or not grokbot_ops._path_is_symlink(path):
            raise grokbot_ops.ServiceRenderError(f"refusing unsafe unit path: {path.name}") from exc
        existing = None
    if existing == rendered:
        return path
    if existing is not None and not force:
        raise grokbot_ops.ServiceRenderError(f"refusing to overwrite existing unit: {path.name}")
    try:
        grokbot_ops._write_text_nofollow_atomic(
            path, rendered, mode=0o644, replace_symlink=force, replace=force or existing is not None
        )
    except OSError as exc:
        raise grokbot_ops.ServiceRenderError(f"refusing unsafe unit path: {path.name}") from exc
    return path
