from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Literal, Protocol

Trust = Literal["local", "web", "cli", "browser"]
Status = Literal["running", "done", "cancelled", "error"]
ResearchPhase = Literal[
    "planning", "discovery", "extraction", "synthesis", "review", "repair", "publishing"
]


@dataclass(frozen=True)
class ResearchProfile:
    name: str
    discovery: tuple[str, ...]
    planner: tuple[str, ...]
    extractor: tuple[str, ...]
    synthesizer: tuple[str, ...]
    reviewer: tuple[str, ...]
    allow_synthesis_fallback: bool
    browser_ai_research: bool


BUILTIN_PROFILES: Dict[str, ResearchProfile] = {
    "grounded": ResearchProfile(
        name="grounded",
        discovery=("brigade",),
        planner=(),
        extractor=(),
        synthesizer=(),
        reviewer=(),
        allow_synthesis_fallback=True,
        browser_ai_research=False,
    ),
    "browser-ai": ResearchProfile(
        name="browser-ai",
        discovery=("brigade", "browser-ai"),
        planner=(),
        extractor=(),
        synthesizer=(),
        reviewer=(),
        allow_synthesis_fallback=True,
        browser_ai_research=True,
    ),
    "local-only": ResearchProfile(
        name="local-only",
        discovery=("local", "repository"),
        planner=(),
        extractor=(),
        synthesizer=(),
        reviewer=(),
        allow_synthesis_fallback=False,
        browser_ai_research=False,
    ),
    "luna-only": ResearchProfile(
        name="luna-only",
        discovery=("brigade",),
        planner=(),
        extractor=(),
        synthesizer=(),
        reviewer=(),
        allow_synthesis_fallback=False,
        browser_ai_research=False,
    ),
}


@dataclass
class Finding:
    source: str
    title: str
    summary: str
    evidence: str
    trust: Trust

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Caps:
    max_rounds: int = 6
    min_rounds: int = 2
    max_time: int = 300  # wall-clock seconds
    max_urls_per_round: int = 3
    max_local_docs_per_round: int = 5
    max_content_chars: int = 15000
    max_report_tokens: int = 8192
    max_empty_rounds: int = 2
    synthesis_window: int = 10

    @classmethod
    def build(cls, **overrides: Any) -> "Caps":
        base = cls()
        for k, v in overrides.items():
            if v is not None and hasattr(base, k):
                setattr(base, k, v)
        return base


@dataclass
class ProgressEvent:
    phase: str
    detail: Dict[str, Any] = field(default_factory=dict)


class LlmBackend(Protocol):
    def complete(
        self, messages: List[Dict[str, str]], *, max_tokens: int = 2048, temperature: float = 0.3, timeout: int = 60
    ) -> str: ...


class SearchProvider(Protocol):
    def search(self, query: str, limit: int) -> List[Dict[str, str]]: ...  # [{url,title}]
    def fetch(self, url: str) -> Dict[str, Any]: ...  # {success,content,title}
