# src/brigade/research/engine.py
from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..untrusted import wrap_untrusted
from . import extract as _extract
from .llm import ResearchSeatError
from .provenance import audit_citations, citation_token
from .types import (
    Caps,
    CitationAudit,
    FallbackRecord,
    Finding,
    ResearchLanes,
    ResearchResult,
    ResearchRunError,
    ResumeState,
    ReviewResult,
    SourceEnvelope,
    SynthesisRecord,
    Trust,
)

PLAN_PROMPT = """You are a research strategist. Build a research plan for this question.
**Question:** {q}
Return JSON: {{"sub_questions": [...], "key_topics": [...], "success_criteria": "..."}}"""

SYNTH_PROMPT = """Write a grounded research report answering the question.
**Question:** {q}
**Findings packet:**
{packet}
Integrate the findings, remove redundancy, and keep inline [source:<id>] citations.
Write only the report."""

REVIEW_PROMPT = """You are an independent research reviewer.
Audit the report against the cited finding packet. Do not continue any prior model session.
**Question:** {q}
**Report:**
{report}
**Citation audit:** accepted={audit_accepted}; unresolved={unresolved}
**Findings packet:**
{packet}
Return ONLY JSON:
{{"accepted": true, "detail": "citations resolve", "rejected_claims": []}}"""

REPAIR_PROMPT = """Repair the research report so every claim is supported by the finding packet.
**Question:** {q}
**Current report:**
{report}
**Citation audit:** accepted={audit_accepted}; unresolved={unresolved}
**Review:** accepted={review_accepted}; detail={review_detail}; rejected={rejected_claims}
**Findings packet:**
{packet}
Rewrite only the report. Keep inline [source:<id>] citations."""

_TRUST_KIND = {
    "local": "retrieved-doc",
    "web": "web",
    "cli": "tool-output",
    "browser": "web",
    "browser-ai": "web",
}

_TRUST_ORIGIN = {
    "local": "local",
    "web": "web",
    "cli": "indexed-cli",
    "browser": "web",
    "browser-ai": "browser-ai",
}


def _parse_json(text: str) -> Any | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                return None
    return None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class ResearchEngine:
    lanes: ResearchLanes
    sources: list[Any]
    caps: Caps
    on_phase_started: Callable[[str], None] | None = None
    on_phase_completed: Callable[[str, dict[str, Any]], None] | None = None
    fallbacks: list[FallbackRecord] = field(default_factory=list)
    _cancelled: bool = field(default=False, init=False)
    _start: float = field(default=0.0, init=False)
    _synthesis_backend: Any | None = field(default=None, init=False)
    _rounds: int = field(default=0, init=False)

    def cancel(self) -> None:
        self._cancelled = True

    def run(self, question: str, *, resume: ResumeState | None = None) -> ResearchResult:
        self._start = time.time()
        self.fallbacks = []
        plan = self._resume_or_plan(question, resume)
        sources = self._resume_or_discover(question, plan, resume)
        findings = self._resume_or_extract(question, sources, resume)
        report, synthesis = self._synthesize_with_fallback(question, findings)
        audit = audit_citations(report, findings)
        review = self._review(question, report, findings, audit, synthesis.attempt_id)
        if not audit.accepted or not review.accepted:
            report = self._repair_once(question, report, findings, audit, review)
            audit = audit_citations(report, findings)
            review = self._review(question, report, findings, audit, synthesis.attempt_id)
        if not audit.accepted or not review.accepted:
            raise ResearchRunError("review", "review-rejected", review.detail)
        return ResearchResult(
            report=report,
            findings=findings,
            sources=sources,
            citation_audit=audit,
            review=review,
            synthesis_seat=synthesis.seat,
            synthesis_attempt_id=synthesis.attempt_id,
            fallbacks=tuple(self.fallbacks),
            stats=self._stats(findings, sources),
        )

    def _resume_or_plan(self, question: str, resume: ResumeState | None) -> str:
        if resume is not None and resume.plan is not None:
            return resume.plan
        return self._plan(question)

    def _resume_or_discover(
        self, question: str, plan: str, resume: ResumeState | None
    ) -> tuple[SourceEnvelope, ...]:
        if resume is not None and resume.sources is not None:
            return resume.sources
        return self._discover(question, plan)

    def _resume_or_extract(
        self, question: str, sources: tuple[SourceEnvelope, ...], resume: ResumeState | None
    ) -> tuple[Finding, ...]:
        if resume is not None and resume.findings is not None:
            return resume.findings
        return self._extract(question, sources)

    def _plan(self, question: str) -> str:
        self._phase_start("planning")
        try:
            text = self.lanes.planner.complete(
                [{"role": "user", "content": PLAN_PROMPT.format(q=question)}],
                max_tokens=1024,
                timeout=30,
            )
        except ResearchSeatError as exc:
            raise ResearchRunError("planning", exc.failure_kind, str(exc)) from exc
        data = _parse_json(text)
        if not isinstance(data, dict):
            raise ResearchRunError("planning", "invalid-json", "planner returned invalid JSON")
        self._phase_done("planning", artifact="plan.json", digest=_digest(text), value=text)
        return text

    def _discover(self, question: str, plan: str) -> tuple[SourceEnvelope, ...]:
        self._phase_start("discovery")
        queries = self._discovery_queries(question, plan)
        providers = list(self.sources)
        if not providers:
            self._phase_done("discovery", artifact="sources.json", digest=_digest("[]"), count=0)
            return ()

        def _provider_batch(index: int, provider: Any) -> tuple[int, list[SourceEnvelope]]:
            envelopes: list[SourceEnvelope] = []
            seen: set[str] = set()
            for query in queries:
                for hit in provider.search(query, self.caps.max_urls_per_round):
                    url = str(hit.get("url") or "")
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    page = provider.fetch(url)
                    if not page.get("success") or not page.get("content"):
                        continue
                    trust = self._normalize_trust(
                        hit.get("trust") or getattr(provider, "trust", "web")
                    )
                    envelopes.append(
                        SourceEnvelope.build(
                            origin=_TRUST_ORIGIN[trust],  # type: ignore[arg-type]
                            provider=str(getattr(provider, "source_id", provider.__class__.__name__)),
                            uri=url,
                            content=str(page["content"]),
                            trust=trust,
                            acquired_at=datetime.now(timezone.utc).isoformat(),
                        )
                    )
            return index, envelopes

        ordered: list[tuple[int, list[SourceEnvelope]]] = []
        workers = min(len(providers), 4)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_provider_batch, index, provider): index
                for index, provider in enumerate(providers)
            }
            for future in as_completed(futures):
                ordered.append(future.result())
        ordered.sort(key=lambda item: item[0])
        sources = tuple(envelope for _, batch in ordered for envelope in batch)
        payload = json.dumps([s.source_id for s in sources])
        self._phase_done(
            "discovery",
            artifact="sources.json",
            digest=_digest(payload),
            count=len(sources),
        )
        self._rounds = 1
        return sources

    def _extract(self, question: str, sources: tuple[SourceEnvelope, ...]) -> tuple[Finding, ...]:
        self._phase_start("extraction")
        findings: list[Finding] = []
        lane = getattr(self.lanes.extractor, "seat", "luna")
        try:
            for source in sources:
                finding = _extract.extract_finding(
                    self.lanes.extractor,
                    goal=question,
                    source=source,
                    extraction_lane=str(lane),
                    max_content_chars=self.caps.max_content_chars,
                )
                if finding is not None:
                    findings.append(finding)
        except ResearchSeatError as exc:
            raise ResearchRunError("extraction", exc.failure_kind, str(exc)) from exc
        except ValueError as exc:
            raise ResearchRunError(
                "extraction",
                "invalid-json",
                "extractor returned invalid JSON",
            ) from exc
        payload = json.dumps([f.source_ids for f in findings])
        self._phase_done(
            "extraction",
            artifact="findings.json",
            digest=_digest(payload),
            count=len(findings),
        )
        return tuple(findings)

    def _synthesize_with_fallback(
        self, question: str, findings: tuple[Finding, ...]
    ) -> tuple[str, SynthesisRecord]:
        self._phase_start("synthesis")
        synthesizers = self.lanes.synthesizers
        if not synthesizers:
            raise ResearchRunError("synthesis", "no-synthesizer", "no synthesis seats configured")
        last_error: ResearchSeatError | None = None
        for index, backend in enumerate(synthesizers):
            try:
                report = self._synthesize(backend, question, findings)
            except ResearchSeatError as exc:
                last_error = exc
                if index + 1 >= len(synthesizers):
                    raise ResearchRunError("synthesis", exc.failure_kind, str(exc)) from exc
                self.fallbacks.append(
                    FallbackRecord(
                        phase="synthesis",
                        from_seat=backend.seat,
                        to_seat=synthesizers[index + 1].seat,
                        failure_kind=exc.failure_kind,
                        detail=str(exc),
                    )
                )
                continue
            attempt_id = getattr(backend, "last_attempt_id", None) or f"{backend.seat}:synthesis"
            record = SynthesisRecord(
                seat=backend.seat,
                attempt_id=attempt_id,
                requested_model=getattr(backend, "requested_model", None),
                observed_model=str(getattr(backend, "observed_model", "unverified")),
            )
            self._synthesis_backend = backend
            self._phase_done(
                "synthesis",
                artifact="report.md",
                digest=_digest(report),
                seat=record.seat,
                attempt_id=record.attempt_id,
            )
            return report, record
        assert last_error is not None
        raise ResearchRunError("synthesis", last_error.failure_kind, str(last_error)) from last_error

    def _synthesize(self, backend: Any, question: str, findings: tuple[Finding, ...]) -> str:
        packet = self._finding_packet(question, findings)
        return backend.complete(
            [{"role": "user", "content": SYNTH_PROMPT.format(q=question, packet=packet)}],
            max_tokens=self.caps.max_report_tokens,
        )

    def _review(
        self,
        question: str,
        report: str,
        findings: tuple[Finding, ...],
        audit: CitationAudit,
        synthesis_attempt_id: str,
    ) -> ReviewResult:
        self._phase_start("review")
        packet = self._finding_packet(question, findings)
        prompt = REVIEW_PROMPT.format(
            q=question,
            report=self._wrap_model_output(report),
            audit_accepted=audit.accepted,
            unresolved=list(audit.unresolved),
            packet=packet,
        )
        try:
            text = self.lanes.reviewer.complete(
                [{"role": "user", "content": prompt}],
                max_tokens=1024,
                temperature=0.1,
            )
        except ResearchSeatError as exc:
            raise ResearchRunError("review", exc.failure_kind, str(exc)) from exc
        data = _parse_json(text)
        if not isinstance(data, dict):
            raise ResearchRunError("review", "invalid-json", "reviewer returned invalid JSON")
        accepted = data.get("accepted")
        rejected_raw = data.get("rejected_claims", [])
        if rejected_raw is None:
            rejected_raw = []
        if not isinstance(accepted, bool) or not isinstance(rejected_raw, (list, tuple)):
            raise ResearchRunError("review", "invalid-json", "reviewer returned invalid JSON")
        if not all(isinstance(item, (str, int, float, bool)) for item in rejected_raw):
            raise ResearchRunError("review", "invalid-json", "reviewer returned invalid JSON")
        attempt_id = getattr(self.lanes.reviewer, "last_attempt_id", None) or "review:unknown"
        if attempt_id == synthesis_attempt_id:
            raise ResearchRunError(
                "review",
                "same-attempt",
                "review attempt_id must differ from synthesis attempt_id",
            )
        result = ReviewResult(
            accepted=accepted,
            detail=str(data.get("detail", "")),
            rejected_claims=tuple(str(item) for item in rejected_raw),
            seat=str(getattr(self.lanes.reviewer, "seat", "reviewer")),
            attempt_id=attempt_id,
        )
        self._phase_done(
            "review",
            artifact="citation-audit.json",
            digest=_digest(text),
            accepted=result.accepted,
            attempt_id=result.attempt_id,
        )
        return result

    def _repair_once(
        self,
        question: str,
        report: str,
        findings: tuple[Finding, ...],
        audit: CitationAudit,
        review: ReviewResult,
    ) -> str:
        self._phase_start("repair")
        backend = self._synthesis_backend or self.lanes.synthesizers[0]
        packet = self._finding_packet(question, findings)
        rejected_text = json.dumps(list(review.rejected_claims))
        prompt = REPAIR_PROMPT.format(
            q=question,
            report=self._wrap_model_output(report),
            audit_accepted=audit.accepted,
            unresolved=list(audit.unresolved),
            review_accepted=review.accepted,
            review_detail=self._wrap_model_output(review.detail),
            rejected_claims=self._wrap_model_output(rejected_text),
            packet=packet,
        )
        try:
            repaired = backend.complete(
                [{"role": "user", "content": prompt}],
                max_tokens=self.caps.max_report_tokens,
            )
        except ResearchSeatError as exc:
            raise ResearchRunError("repair", exc.failure_kind, str(exc)) from exc
        self._phase_done("repair", artifact="report.md", digest=_digest(repaired))
        return repaired

    def _wrap_model_output(self, text: str) -> str:
        return wrap_untrusted(
            text,
            source_kind="tool-output",
            max_chars=self.caps.max_content_chars,
        )

    def _finding_packet(self, question: str, findings: tuple[Finding, ...]) -> str:
        blocks: list[str] = []
        for finding in findings:
            token = citation_token(finding.source_ids[0])
            kind = _TRUST_KIND[finding.trust]
            summary = wrap_untrusted(finding.summary, source_kind=kind, goal=question)
            evidence = wrap_untrusted(finding.evidence, source_kind=kind, goal=question)
            blocks.append(
                f"{token}\nsummary:\n{summary}\nevidence:\n{evidence}"
            )
        return "\n\n".join(blocks) if blocks else "(no findings)"

    def _discovery_queries(self, question: str, plan: str) -> list[str]:
        data = _parse_json(plan)
        queries = [question]
        if isinstance(data, dict):
            for key in ("sub_questions", "key_topics"):
                values = data.get(key) or []
                if isinstance(values, list):
                    queries.extend(str(item) for item in values if str(item).strip())
        # Deduplicate while preserving order; cap to one discovery round's breadth.
        seen: set[str] = set()
        out: list[str] = []
        for query in queries:
            if query in seen:
                continue
            seen.add(query)
            out.append(query)
            if len(out) >= max(1, self.caps.max_urls_per_round):
                break
        return out

    @staticmethod
    def _normalize_trust(value: Any) -> Trust:
        trust = str(value or "web")
        if trust not in _TRUST_KIND:
            return "web"
        return trust  # type: ignore[return-value]

    def _phase_start(self, phase: str) -> None:
        if self.on_phase_started is not None:
            self.on_phase_started(phase)

    def _phase_done(self, phase: str, **artifacts: Any) -> None:
        if self.on_phase_completed is not None:
            self.on_phase_completed(phase, artifacts)

    def _stats(
        self, findings: tuple[Finding, ...], sources: tuple[SourceEnvelope, ...]
    ) -> dict[str, Any]:
        return {
            "rounds": self._rounds,
            "findings": len(findings),
            "sources": len(sources),
            "elapsed": round(time.time() - self._start, 1) if self._start else 0.0,
            "fallbacks": len(self.fallbacks),
        }
