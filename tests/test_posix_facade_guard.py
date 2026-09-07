"""Guard against Windows-unsafe direct process and directory primitives.

``os.O_DIRECTORY`` and ``os.getpgid`` are absent on Windows, so direct use
raises ``AttributeError`` instead of a typed fail-closed error. All uses must
go through ``brigade.dirfd`` (``directory_flags``/``root_directory_flags``)
and ``brigade.proc.process_group_id``; only ``dirfd.py``, ``nt_dirfd.py``,
and ``proc.py`` may reference the raw attributes.
"""

from __future__ import annotations

import ast
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


POSIX_TEST_ATTRIBUTES = frozenset({"O_DIRECTORY", "O_NOFOLLOW", "O_PATH", "getpgid", "killpg"})

POSIX_TEST_MARKERS = frozenset({"requires_dirfd", "requires_process_groups", "requires_opath"})


def _direct_posix_attributes(path: Path) -> set[str]:
    """Return POSIX-only ``os.<attr>`` names read as code (not strings)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in POSIX_TEST_ATTRIBUTES
        ):
            found.add(node.attr)
    return found


def test_tests_reference_posix_primitives_only_with_shared_markers() -> None:
    """Direct ``os.O_DIRECTORY``/``os.O_NOFOLLOW``/``os.O_PATH``/``os.getpgid``/``os.killpg``
    in ``tests/`` must live in a file that imports the shared markers.

    Rule: outside ``tests/_posix.py`` (which defines the markers), a test file
    may read one of those attributes as code only when it imports
    ``tests._posix`` and references one of ``requires_dirfd``,
    ``requires_process_groups``, or ``requires_opath``. The import plus marker
    reference proves every directly-touching test carries a skip naming the
    missing primitive, so Windows collection never reaches the
    ``AttributeError``. Files that only mention the names inside strings (this
    guard's own source scan, the inbox escape-message assertions) are ignored:
    the check parses the AST, so string literals and comments never count.
    Prefer rerouting helpers through the ``brigade.dirfd`` facade opens so the
    test still runs on Windows; add a marker only when the test body genuinely
    requires the raw primitive (``dir_fd=``, ``O_PATH`` assertions, process
    group teardown, POSIX flag pins).
    """
    root = Path(__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        if path.name == "_posix.py":
            continue
        direct = _direct_posix_attributes(path)
        if not direct:
            continue
        text = path.read_text(encoding="utf-8")
        if "_posix" in text and any(marker in text for marker in POSIX_TEST_MARKERS):
            continue
        offenders.append(f"{path.name} ({', '.join(sorted(direct))})")
    assert not offenders, (
        "Windows-unsafe test primitives need a tests._posix shared marker or a dirfd facade reroute: "
        + ", ".join(offenders)
    )
