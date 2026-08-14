# src/brigade/research/types.py
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Protocol

Trust = Literal["local", "web", "cli", "browser", "browser-ai"]
Origin = Literal["local", "repository", "indexed-cli", "web", "browser-ai"]
Status = Literal["running", "done", "cancelled", "error"]
ResearchPhase = Literal[
    "planning", "discovery", "extraction", "synthesis", "review", "repair", "publishing"
]


@dataclass(frozen=True)
class SourceEnvelope:
    source_id: str
    origin: Origin
    provider: str
    uri: str
    content: str
    content_digest: str
    acquired_at: str
    trust: Trust
    producing_lane: str | None = None
    requested_model: str | None = None
    observed_model: str | None = None
    parent_source_ids: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        origin: Origin,
        provider: str,
        uri: str,
        content: str,
        trust: Trust,
        acquired_at: str,
        producing_lane: str | None = None,
        requested_model: str | None = None,
        observed_model: str | None = None,
        parent_source_ids: tuple[str, ...] = (),
    ) -> SourceEnvelope:
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        identity = "\0".join((origin, provider, uri, content_digest))
        source_id = "src-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return cls(
            source_id=source_id,
            origin=origin,
            provider=provider,
            uri=uri,
            content=content,
            content_digest=content_digest,
            acquired_at=acquired_at,
            trust=trust,
            producing_lane=producing_lane,
            requested_model=requested_model,
            observed_model=observed_model,
            parent_source_ids=parent_source_ids,
        )


@dataclass(frozen=True)
class Finding:
    source_ids: tuple[str, ...]
    title: str
    summary: str
    evidence: str
    trust: Trust
    extraction_lane: str
    extracted_at: str
    parent_source_ids: tuple[str, ...] = ()

    @property
    def source(self) -> str:
        return self.source_ids[0]


@dataclass(frozen=True)
class CitationRecord:
    token: str
    source_ids: tuple[str, ...]
    status: Literal["accepted", "rejected", "unresolved", "repaired"]


@dataclass(frozen=True)
class CitationAudit:
    accepted: bool
    citations: tuple[CitationRecord, ...]
    unresolved: tuple[str, ...]


@dataclass
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
    def build(cls, **overrides: Any) -> Caps:
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
