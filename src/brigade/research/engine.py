# src/brigade/research/engine.py
from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from .types import Caps, Finding
from . import extract as _extract

PLAN_PROMPT = """You are a research strategist. Build a research plan for this question.
**Question:** {q}
Return JSON: {{"sub_questions": [...], "key_topics": [...], "success_criteria": "..."}}"""

QUERY_PROMPT = """You are planning search queries.
**Question:** {q}
**Plan:** {plan}
**What we know:** {report}
Generate {n} focused search queries. Return ONLY a JSON array of strings."""

SYNTH_PROMPT = """Update an evolving research report.
**Question:** {q}
**Current report:** {report}
**New findings:** {findings}
Integrate the findings, remove redundancy, keep inline source citations. Write only the report."""

STOP_PROMPT = """Is this report comprehensive enough to answer the question?
**Question:** {q}
**Report:** {report}
Reply ONLY 'YES' or 'NO' then a brief reason."""

FINAL_PROMPT = """Write a detailed, well-structured final report answering:
**Question:** {q}
**Evidence:** {report}
Use ## headings, synthesize, keep inline citations, add an executive summary and a conclusion."""


@dataclass
class ResearchResult:
    report: str
    findings: List[Finding]
    stats: Dict[str, Any]


@dataclass
class DeepResearcher:
    llm: Any
    local_index: Any
    web: Any  # SearchProvider or None
    caps: Caps
    external_sources: List[Any] = field(default_factory=list)
    on_checkpoint: Optional[Callable[[Dict[str, Any]], None]] = None
    on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None
    _cancelled: bool = field(default=False, init=False)

    def cancel(self) -> None:
        self._cancelled = True

    def _emit(self, phase: str, **detail: Any) -> None:
        if self.on_event:
            self.on_event(phase, detail)

    def _ask(self, prompt: str, **kw) -> str:
        return self.llm.complete([{"role": "user", "content": prompt}], **kw)

    @staticmethod
    def _json_array(text: str) -> List[str]:
        m = re.search(r"\[[\s\S]*\]", text)
        if m:
            try:
                v = json.loads(m.group())
                return [str(x) for x in v] if isinstance(v, list) else []
            except json.JSONDecodeError:
                return []
        return []

    def research(self, question: str, *, prior: Optional[Dict[str, Any]] = None) -> ResearchResult:
        start = time.time()
        prior = prior or {}
        report = prior.get("report", "")
        findings: List[Finding] = [Finding(**f) if isinstance(f, dict) else f for f in prior.get("findings", [])]
        seen_q = set(prior.get("queries", []))
        seen_u = set(prior.get("urls", []))
        round_no = prior.get("round", 0)
        empty = 0

        self._emit("planning")
        plan = self._safe_plan(question) if not prior else prior.get("plan", "")

        while round_no < self.caps.max_rounds:
            if self._cancelled or (time.time() - start) > self.caps.max_time:
                break
            round_no += 1
            self._emit("searching", round=round_no)
            queries = [q for q in self._gen_queries(question, plan, report, round_no) if q not in seen_q]
            seen_q.update(queries)
            if not queries:
                break

            round_findings: List[Finding] = []
            for q in queries:
                round_findings += self._gather(q, question, seen_u)
            if round_findings:
                findings += round_findings
                empty = 0
                report = self._synthesize(question, findings, report)
            else:
                empty += 1
                if empty >= self.caps.max_empty_rounds:
                    break

            if self.on_checkpoint:
                self.on_checkpoint(
                    {
                        "round": round_no,
                        "report": report,
                        "findings": [f.as_dict() for f in findings],
                        "urls": sorted(seen_u),
                        "queries": sorted(seen_q),
                        "plan": plan,
                    }
                )
            if round_no >= self.caps.min_rounds and self._should_stop(question, report):
                break

        self._emit("writing")
        final = self._final(question, report) if report else "No information gathered."
        stats = {
            "rounds": round_no,
            "findings": len(findings),
            "sources": len(seen_u) + sum(1 for f in findings if f.trust == "local"),
            "elapsed": round(time.time() - start, 1),
        }
        return ResearchResult(report=final, findings=findings, stats=stats)

    def _safe_plan(self, q: str) -> str:
        try:
            return self._ask(PLAN_PROMPT.format(q=q), max_tokens=1024, timeout=30)
        except Exception:
            return ""

    def _gen_queries(self, q: str, plan: str, report: str, rnd: int) -> List[str]:
        n = 4 if rnd == 1 else 3
        out = self._ask(
            QUERY_PROMPT.format(q=q, plan=plan or "(none)", report=report or "(none)", n=n),
            max_tokens=2048,
            temperature=0.5,
        )
        return self._json_array(out)

    def _gather(self, query: str, goal: str, seen_u: set) -> List[Finding]:
        results: List[Finding] = []
        # trusted local
        if self.local_index is not None:
            for hit in self.local_index.search(query, limit=self.caps.max_local_docs_per_round):
                f = _extract.extract_finding(
                    self.llm,
                    goal=goal,
                    source=hit["source"],
                    title=hit.get("title", ""),
                    content=hit["text"],
                    trust="local",
                    max_content_chars=self.caps.max_content_chars,
                )
                if f:
                    results.append(f)
        # untrusted web (opt-in: web provider supplied)
        if self.web is not None:
            results += self._gather_provider(
                self.web, query, goal, seen_u, default_trust=getattr(self.web, "trust", "web")
            )
        for provider in self.external_sources:
            results += self._gather_provider(
                provider, query, goal, seen_u, default_trust=getattr(provider, "trust", "cli")
            )
        return results

    def _gather_provider(
        self, provider: Any, query: str, goal: str, seen_u: set, *, default_trust: str
    ) -> List[Finding]:
        results: List[Finding] = []
        for r in provider.search(query, self.caps.max_urls_per_round):
            url = r.get("url", "")
            if not url or url in seen_u:
                continue
            seen_u.add(url)
            page = provider.fetch(url)
            if not page.get("success") or not page.get("content"):
                continue
            trust = str(r.get("trust") or getattr(provider, "trust", default_trust))
            if trust not in {"web", "cli", "browser", "local"}:
                trust = default_trust
            f = _extract.extract_finding(
                self.llm,
                goal=goal,
                source=url,
                title=r.get("title", ""),
                content=page["content"],
                trust=trust,  # type: ignore[arg-type]
                max_content_chars=self.caps.max_content_chars,
            )
            if f:
                results.append(f)
        return results

    def _synthesize(self, q: str, findings: List[Finding], report: str) -> str:
        window = findings[-self.caps.synthesis_window :]
        text = "\n\n".join(f"[{f.trust}] {f.title} ({f.source})\n{f.summary}" for f in window)
        try:
            return self._ask(
                SYNTH_PROMPT.format(q=q, report=report or "(none)", findings=text),
                max_tokens=self.caps.max_report_tokens,
            )
        except Exception:
            return report

    def _should_stop(self, q: str, report: str) -> bool:
        try:
            out = self._ask(STOP_PROMPT.format(q=q, report=report), max_tokens=128, temperature=0.1)
            return re.sub(r"^[\s*_`\"'>#-]+", "", out.strip()).upper().startswith("YES")
        except Exception:
            return False

    def _final(self, q: str, report: str) -> str:
        try:
            return self._ask(
                FINAL_PROMPT.format(q=q, report=report), max_tokens=self.caps.max_report_tokens, timeout=180
            )
        except Exception:
            return report
