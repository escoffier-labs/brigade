"""Read-only overflow routing caches for a batch of held drafts."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import instruction_overflow


@dataclass
class RouteContext(instruction_overflow.RouteContext):
    """Reuse card discovery and trust checks within each file and type group."""

    _card_groups: dict[tuple[str, str], dict[int, str]] = field(default_factory=dict, init=False, repr=False)
    _trust: dict[str, str | None] = field(default_factory=dict, init=False, repr=False)

    def card_numbers(self, file_key: str, kind: str) -> dict[int, str]:
        group = (file_key, kind)
        if group not in self._card_groups:
            self._card_groups[group] = super().card_numbers(file_key, kind)
        return self._card_groups[group]

    def card_trust(self, rel: str) -> str | None:
        if rel not in self._trust:
            self._trust[rel] = super().card_trust(rel)
        return self._trust[rel]
