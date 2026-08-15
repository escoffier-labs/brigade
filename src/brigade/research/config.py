from __future__ import annotations
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List

from .. import toml_compat as tomllib
from .types import BUILTIN_PROFILES, ResearchProfile

_LANE_FIELDS = ("discovery", "planner", "extractor", "synthesizer", "reviewer")
_ALLOWED_PROFILE_KEYS = frozenset(_LANE_FIELDS) | frozenset({"allow_synthesis_fallback", "browser_ai_research"})


class ResearchConfig:
    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def corpus_paths(self, name: str) -> List[str]:
        for c in self._data.get("corpus", []):
            if c.get("name") == name:
                return list(c.get("paths", []))
        return []

    def caps_overrides(self) -> Dict[str, Any]:
        return dict(self._data.get("caps", {}))

    def search_settings(self) -> Dict[str, Any]:
        return dict(self._data.get("search", {}))

    def source_adapters(self) -> List[Dict[str, Any]]:
        adapters = self._data.get("source", [])
        if not isinstance(adapters, list):
            return []
        return [dict(item) for item in adapters if isinstance(item, dict)]

    def profile(self, name: str | None) -> ResearchProfile:
        research = self._data.get("research", {})
        if research is None:
            research = {}
        if not isinstance(research, dict):
            raise ValueError("[research] must be a TOML table")

        if name is None:
            default = research.get("default_profile", "grounded")
            if not isinstance(default, str) or not default.strip():
                raise ValueError("research.default_profile must be a non-empty string")
            name = default.strip()

        base = BUILTIN_PROFILES.get(name)
        if base is None:
            raise ValueError(f"unknown research profile: {name!r}")

        profiles = self._data.get("profiles", {})
        if profiles is None:
            profiles = {}
        if not isinstance(profiles, dict):
            raise ValueError("[profiles] must be a TOML table")

        raw = profiles.get(name)
        if raw is None:
            return base
        if not isinstance(raw, dict):
            raise ValueError(f"[profiles.{name}] must be a TOML table")

        unknown_keys = sorted(set(raw) - _ALLOWED_PROFILE_KEYS)
        if unknown_keys:
            joined = ", ".join(unknown_keys)
            raise ValueError(f"profiles.{name} has unknown key(s): {joined}")

        overlays: Dict[str, Any] = {}
        for field in _LANE_FIELDS:
            if field not in raw:
                continue
            value = raw[field]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"profiles.{name}.{field} must be a list of strings")
            if len(value) == 0:
                raise ValueError(f"profiles.{name}.{field} must not be empty when configured")
            stripped = tuple(item.strip() for item in value)
            if any(not item for item in stripped):
                raise ValueError(f"profiles.{name}.{field} must not contain blank entries")
            overlays[field] = stripped

        if "allow_synthesis_fallback" in raw:
            fallback = raw["allow_synthesis_fallback"]
            if not isinstance(fallback, bool):
                raise ValueError(f"profiles.{name}.allow_synthesis_fallback must be true or false")
            overlays["allow_synthesis_fallback"] = fallback

        # Browser-AI activation is reserved for the built-in browser-ai profile
        # and the explicit CLI flag. Repo overlays must not flip it on for other
        # profiles; the browser-ai profile may opt out explicitly.
        if "browser_ai_research" in raw:
            browser = raw["browser_ai_research"]
            if browser is not True and browser is not False:
                raise ValueError(f"profiles.{name}.browser_ai_research must be true or false")
            if name == "browser-ai":
                overlays["browser_ai_research"] = browser
            # else: ignore overlay; keep built-in False for grounded/local-only/luna-only

        return replace(base, **overlays)


def load(target: Path) -> ResearchConfig:
    p = target / ".brigade" / "research.toml"
    if not p.exists():
        return ResearchConfig({})
    return ResearchConfig(tomllib.loads(p.read_text()))
