"""Guard against Windows-unsafe direct process and directory primitives.

``os.O_DIRECTORY`` and ``os.getpgid`` are absent on Windows, so direct use
raises ``AttributeError`` instead of a typed fail-closed error. All uses must
go through ``brigade.dirfd`` (``directory_flags``/``root_directory_flags``)
and ``brigade.proc.process_group_id``; only ``dirfd.py``, ``nt_dirfd.py``,
and ``proc.py`` may reference the raw attributes.
"""

from __future__ import annotations

from pathlib import Path

ALLOWED = frozenset({"dirfd.py", "nt_dirfd.py", "proc.py"})


def test_posix_primitives_go_through_facades() -> None:
    root = Path(__file__).resolve().parent.parent / "src" / "brigade"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        if "os.O_DIRECTORY" in text or "os.getpgid" in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, "Windows-unsafe primitives must go through brigade.dirfd/proc facades: " + ", ".join(
        offenders
    )
