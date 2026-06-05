from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List
try:
    import tomllib                       # py3.11+
except ModuleNotFoundError:              # py3.10
    import tomli as tomllib              # type: ignore

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

def load(target: Path) -> ResearchConfig:
    p = target / ".brigade" / "research.toml"
    if not p.exists():
        return ResearchConfig({})
    return ResearchConfig(tomllib.loads(p.read_text()))
