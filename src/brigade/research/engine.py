# src/brigade/research/engine.py
from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event
from typing import Any, Callable

from ..run_budget import BudgetPolicyError
from ..run_lifecycle import LifecycleJournalError
from ..untrusted import wrap_untrusted
from . import extract as _extract
from .llm import ResearchSeatError
from .provenance import audit_citations, citation_token
from .sources.browser_ai import BrowserAiDiscoveryError
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
    persist_sources: Callable[[tuple[SourceEnvelope, ...]], Any] | None = None
    persist_findings: Callable[[tuple[Finding, ...]], Any] | None = None
    persist_draft_report: Callable[[str], Any] | None = None
    persist_citation_audit: Callable[[CitationAudit], Any] | None = None
    cancelled: Event | None = None
    fallbacks: list[FallbackRecord] = field(default_factory=list)
    _cancelled: bool = field(default=False, init=False)
    _start: float = field(default=0.0, init=False)
    _synthesis_backend: Any | None = field(default=None, init=False)
    _rounds: int = field(default=0, init=False)
    _active_phase: str = field(default="planning", init=False)

    def cancel(self) -> None:
        self._cancelled = True
        if self.cancelled is not None:
            self.cancelled.set()

    @property
    def active_phase(self) -> str:
        return self._active_phase

    def run(self, question: str, *, resume: ResumeState | None = None) -> ResearchResult:
        self._start = time.time()
        self.fallbacks = []
        self._ensure_time("planning")
        plan = self._resume_or_plan(question, resume)
        self._check_cancel("planning")
        self._ensure_time("discovery")
        sources = self._resume_or_discover(question, plan, resume)
        self._check_cancel("discovery")
        self._ensure_time("extraction")
        findings = self._resume_or_extract(question, sources, resume)
        self._check_cancel("extraction")
        self._ensure_time("synthesis")
        report, synthesis = self._resume_or_synthesize(question, findings, resume)
        self._check_cancel("synthesis")
        if resume is not None and resume.audit is not None and resume.review is not None and resume.review.accepted:
            audit = resume.audit
            review = resume.review
            self._phase_start("review")
            self._phase_done(
                "review",
                artifact=None,
                accepted=review.accepted,
                attempt_id=review.attempt_id,
                seat=review.seat,
                resumed=True,
            )
        else:
            audit = audit_citations(report, findings)
            self._ensure_time("review")
            review = self._review(question, report, findings, audit, synthesis.attempt_id)
            self._check_cancel("review")
            if not audit.accepted or not review.accepted:
                self._ensure_time("repair")
                report = self._repair_once(question, report, findings, audit, review)
                self._check_cancel("repair")
                audit = audit_citations(report, findings)
                review = self._review(question, report, findings, audit, synthesis.attempt_id)
                self._check_cancel("review")
        if not audit.accepted or not review.accepted:
            raise ResearchRunError("review", "review-rejected", review.detail or "citation audit or review rejected")
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

    def _resume_or_synthesize(
        self, question: str, findings: tuple[Finding, ...], resume: ResumeState | None
    ) -> tuple[str, SynthesisRecord]:
        if resume is not None and resume.report is not None:
            record = resume.synthesis or SynthesisRecord(
                seat=str(getattr(self.lanes.synthesizers[0], "seat", "synthesizer"))
                if self.lanes.synthesizers
                else "synthesizer",
                attempt_id="resumed-synthesis",
                requested_model=None,
                observed_model="resumed",
            )
            self._phase_start("synthesis")
            self._phase_done(
                "synthesis",
                artifact=None,
                seat=record.seat,
                attempt_id=record.attempt_id,
                resumed=True,
            )
            return resume.report, record
        return self._synthesize_with_fallback(question, findings)

    def _plan(self, question: str) -> str:
        self._phase_start("planning")
        try:
            text = self.lanes.planner.complete(
                [{"role": "user", "content": PLAN_PROMPT.format(q=question)}],
                max_tokens=1024,
                timeout=30,
            )
        except ResearchSeatError as exc:
            self._check_cancel("planning")
            raise ResearchRunError("planning", exc.failure_kind, str(exc)) from exc
        self._check_cancel("planning")
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
            empty: tuple[SourceEnvelope, ...] = ()
            artifact: Any = "sources.json"
            if self.persist_sources is not None:
                artifact = self.persist_sources(empty)
            self._phase_done("discovery", artifact=artifact, digest=_digest("[]"), count=0)
            self._rounds = 0
            return ()

        seen_urls: set[str] = set()
        collected: list[SourceEnvelope] = []
        empty_rounds = 0
        round_no = 0
        query_index = 0
        max_rounds = max(1, self.caps.max_rounds)
        empty_limit = max(1, self.caps.max_empty_rounds)

        # Progressive discovery: consume each planned query once, in stable order.
        # Never reissue a query to pad min_rounds; stop on exhaustion, max_rounds,
        # or consecutive empty rounds.
        while round_no < max_rounds and query_index < len(queries):
            self._ensure_time("discovery")
            self._check_cancel("discovery")
            round_queries = [queries[query_index]]
            query_index += 1
            round_no += 1
            before = len(collected)
            try:
                round_sources = self._discover_round(round_queries, providers, seen_urls)
            except ResearchSeatError as exc:
                self._check_cancel("discovery")
                raise ResearchRunError("discovery", exc.failure_kind, str(exc)) from exc
            except BrowserAiDiscoveryError as exc:
                raise ResearchRunError(
                    "discovery",
                    self._discovery_failure_kind(exc),
                    str(exc),
                ) from exc
            collected.extend(round_sources)
            if len(collected) > before:
                empty_rounds = 0
            else:
                empty_rounds += 1
                if empty_rounds >= empty_limit:
                    break

        sources = tuple(collected)
        payload = json.dumps([s.source_id for s in sources])
        artifact = "sources.json"
        digest = _digest(payload)
        if self.persist_sources is not None:
            artifact = self.persist_sources(sources)
        self._phase_done(
            "discovery",
            artifact=artifact,
            digest=digest if isinstance(artifact, str) else None,
            count=len(sources),
        )
        self._rounds = round_no
        return sources

    def _discover_round(
        self,
        queries: list[str],
        providers: list[Any],
        seen_urls: set[str],
    ) -> list[SourceEnvelope]:
        def _provider_batch(index: int, provider: Any) -> tuple[int, list[SourceEnvelope]]:
            self._check_cancel("discovery")
            envelopes: list[SourceEnvelope] = []
            local_seen: set[str] = set()
            trust_hint = self._normalize_trust(getattr(provider, "trust", "web"))
            limit = (
                self.caps.max_local_docs_per_round
                if trust_hint == "local"
                else self.caps.max_urls_per_round
            )
            for query in queries:
                self._check_cancel("discovery")
                for hit in provider.search(query, limit):
                    url = str(hit.get("url") or "")
                    if not url or url in seen_urls or url in local_seen:
                        continue
                    local_seen.add(url)
                    page = provider.fetch(url)
                    if not page.get("success") or not page.get("content"):
                        continue
                    trust = self._normalize_trust(
                        hit.get("trust") or getattr(provider, "trust", "web")
                    )
                    # Bound before identity construction so source_id and
                    # content_digest describe exactly the stored content.
                    raw_content = str(page["content"])
                    content = raw_content[: self.caps.max_content_chars]
                    envelopes.append(
                        SourceEnvelope.build(
                            origin=_TRUST_ORIGIN[trust],  # type: ignore[arg-type]
                            provider=str(getattr(provider, "source_id", provider.__class__.__name__)),
                            uri=url,
                            content=content,
                            trust=trust,
                            acquired_at=datetime.now(timezone.utc).isoformat(),
                            producing_lane=getattr(provider, "lane", None),
                            requested_model=getattr(provider, "requested_model", None),
                            observed_model=getattr(provider, "observed_model", None),
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
                self._check_cancel("discovery")
                try:
                    ordered.append(future.result())
                except BudgetPolicyError:
                    raise
                except LifecycleJournalError as exc:
                    raise ResearchRunError(
                        "discovery",
                        "lifecycle-journal",
                        str(exc),
                    ) from exc
                except (ResearchSeatError, BrowserAiDiscoveryError):
                    raise
                except ResearchRunError:
                    raise
                except Exception as exc:
                    raise ResearchRunError("discovery", "provider-failed", str(exc)) from exc
        ordered.sort(key=lambda item: item[0])
        envelopes = [envelope for _, batch in ordered for envelope in batch]
        for envelope in envelopes:
            seen_urls.add(envelope.uri)
        return envelopes

    @staticmethod
    def _discovery_failure_kind(exc: BrowserAiDiscoveryError) -> str:
        detail = str(exc).lower()
        if "invalid json" in detail:
            return "invalid-json"
        if "https" in detail or "url" in detail:
            return "invalid-url"
        return "provider-failed"

    def _extract(self, question: str, sources: tuple[SourceEnvelope, ...]) -> tuple[Finding, ...]:
        self._phase_start("extraction")
        findings: list[Finding] = []
        lane = getattr(self.lanes.extractor, "seat", "luna")
        try:
            for source in sources:
                self._check_cancel("extraction")
                finding = _extract.extract_finding(
                    self.lanes.extractor,
                    goal=question,
                    source=source,
                    extraction_lane=str(lane),
                    max_content_chars=self.caps.max_content_chars,
                )
                self._check_cancel("extraction")
                if finding is not None:
                    findings.append(finding)
        except ResearchSeatError as exc:
            self._check_cancel("extraction")
            raise ResearchRunError("extraction", exc.failure_kind, str(exc)) from exc
        except ValueError as exc:
            raise ResearchRunError(
                "extraction",
                "invalid-json",
                "extractor returned invalid JSON",
            ) from exc
        payload = json.dumps([f.source_ids for f in findings])
        result = tuple(findings)
        artifact: Any = "findings.json"
        if self.persist_findings is not None:
            artifact = self.persist_findings(result)
        self._phase_done(
            "extraction",
            artifact=artifact,
            digest=_digest(payload),
            count=len(findings),
        )
        return result

    def _synthesize_with_fallback(
        self, question: str, findings: tuple[Finding, ...]
    ) -> tuple[str, SynthesisRecord]:
        self._phase_start("synthesis")
        synthesizers = self.lanes.synthesizers
        if not synthesizers:
            raise ResearchRunError("synthesis", "no-synthesizer", "no synthesis seats configured")
        for index, backend in enumerate(synthesizers):
            self._check_cancel("synthesis")
            try:
                report = self._synthesize(backend, question, findings)
            except ResearchSeatError as exc:
                self._check_cancel("synthesis")
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
            self._check_cancel("synthesis")
            attempt_id = getattr(backend, "last_attempt_id", None) or f"{backend.seat}:synthesis"
            record = SynthesisRecord(
                seat=backend.seat,
                attempt_id=attempt_id,
                requested_model=getattr(backend, "requested_model", None),
                observed_model=str(getattr(backend, "observed_model", "unverified")),
            )
            self._synthesis_backend = backend
            artifact: Any = "report.draft.md"
            if self.persist_draft_report is not None:
                artifact = self.persist_draft_report(report)
            self._phase_done(
                "synthesis",
                artifact=artifact,
                digest=_digest(report),
                seat=record.seat,
                attempt_id=record.attempt_id,
                requested_model=record.requested_model,
                observed_model=record.observed_model,
            )
            return report, record
        raise ResearchRunError("synthesis", "no-synthesizer", "no synthesis seats configured")

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
            self._check_cancel("review")
            raise ResearchRunError("review", exc.failure_kind, str(exc)) from exc
        self._check_cancel("review")
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
        artifact: Any = "citation-audit.json"
        if self.persist_citation_audit is not None:
            artifact = self.persist_citation_audit(audit)
        self._phase_done(
            "review",
            artifact=artifact,
            digest=_digest(text),
            accepted=result.accepted,
            attempt_id=result.attempt_id,
            seat=result.seat,
            detail=result.detail,
            rejected_claims=list(result.rejected_claims),
            review={
                "accepted": result.accepted,
                "detail": result.detail,
                "rejected_claims": list(result.rejected_claims),
                "seat": result.seat,
                "attempt_id": result.attempt_id,
            },
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
            self._check_cancel("repair")
            raise ResearchRunError("repair", exc.failure_kind, str(exc)) from exc
        self._check_cancel("repair")
        artifact: Any = "report.draft.md"
        if self.persist_draft_report is not None:
            artifact = self.persist_draft_report(repaired)
        self._phase_done("repair", artifact=artifact, digest=_digest(repaired))
        return repaired

    def _wrap_model_output(self, text: str) -> str:
        # Synthesized/reviewed model text must stay fully available for the next
        # seat. Cap applies to retrieved source content, not report fencing.
        return wrap_untrusted(
            text,
            source_kind="tool-output",
        )

    def _finding_packet(self, question: str, findings: tuple[Finding, ...]) -> str:
        window = findings[-max(1, self.caps.synthesis_window) :]
        blocks: list[str] = []
        for finding in window:
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
        # Deduplicate while preserving order; keep enough queries for progressive rounds.
        seen: set[str] = set()
        out: list[str] = []
        budget = max(1, self.caps.max_rounds)
        for query in queries:
            if query in seen:
                continue
            seen.add(query)
            out.append(query)
            if len(out) >= budget:
                break
        return out

    @staticmethod
    def _normalize_trust(value: Any) -> Trust:
        trust = str(value or "web")
        if trust not in _TRUST_KIND:
            return "web"
        return trust  # type: ignore[return-value]

    def _ensure_time(self, phase: str) -> None:
        self._active_phase = phase
        self._check_cancel(phase)
        if self._start and (time.time() - self._start) >= self.caps.max_time:
            raise ResearchRunError(phase, "timeout", "research run exceeded max_time")

    def _check_cancel(self, phase: str) -> None:
        if self._cancelled or (self.cancelled is not None and self.cancelled.is_set()):
            raise ResearchRunError(phase, "cancelled", "research run cancelled")

    def _phase_start(self, phase: str) -> None:
        self._active_phase = phase
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
