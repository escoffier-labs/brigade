"""The station contract: a registered, health-checkable unit of Brigade."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# (status, name, detail), reusing the doctor vocabulary.
CheckResult = Tuple[str, str, str]


@dataclass
class DoctorContext:
    """Resolved workspace facts shared across station doctors."""

    target: Path
    selection: object | None  # brigade.selection.Selection | None (avoids import cycle)
    harnesses: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class Station:
    """A unit of Brigade.

    v1 stations are all built-in: their behavior lives inside the brigade
    package and only `doctor` is exercised. Managed stations (install/wire/
    verbs) arrive in Plan 2 and will extend this contract; do not add those
    fields until they are needed (YAGNI).
    """

    name: str
    summary: str
    doctor: Optional[Callable[[DoctorContext], List[CheckResult]]]
    aliases: Tuple[str, ...] = ()
    kind: str = "builtin"
    tools: Tuple[str, ...] = ()

    def matches(self, name_or_alias: str) -> bool:
        return name_or_alias == self.name or name_or_alias in self.aliases
