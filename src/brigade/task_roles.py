"""Bounded, local task intent classification shared by clients and the Hub."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from .roster import Roster

TASK_WORKLOADS: tuple[str, ...] = ("writing", "frontend")
MAX_TASK_CHARS = 8000
_WRITING_VERB = re.compile(r"\b(draft|write|rewrite|edit|copyedit|proofread|polish|tighten|revise|update)\b")
_WRITING_OBJECT = re.compile(
    r"\b(blog(?: post)?|article|essay|newsletter|announcement|post|thread|tweet|email|"
    r"cover letter|resume|bio|linkedin|tutorial|guide|docs page|prose|readme (?:prose|intro)|"
    r"pr (?:description|body|message)|pull request (?:description|body)|landing copy|headline|"
    r"tagline|abstract|proposal|changelog entr(?:y|ies)|release notes|commit message)\b"
)
_FRONTEND_VERB = re.compile(r"\b(design|redesign|restyle|restyled|style|mock up|lay out|critique|make it look)\b")
_FRONTEND_OBJECT = re.compile(
    r"\b(ui|page|hero|layout|typography|colou?r|palette|spacing|css|tailwind|theme|"
    r"dark mode|responsive|mobile view|visual hierarchy|screenshot|animation|navbar|mockup)\b"
)
_WRITING_PHRASE = re.compile(r"\b(prose polish|copyedit|proofread)\b")
_FRONTEND_PHRASE = re.compile(r"\b(visual (?:design|polish|critique)|ui design|frontend design)\b")
_MECHANICAL = re.compile(
    r"\b(writes? to (?:disk|file|the database|db|cache|log|s3|stdout)|file write|database write|"
    r"write (?:path|lock)|writer|writable|write (?:unit |integration |regression )?tests?|"
    r"pagination|paging|pager|page (?:table|fault|cache)|component (?:test|registry|graph|model)|"
    r"build (?:tool|tooling|pipeline|script|config)|vite|webpack|esbuild|rollup|bundler|tsconfig|eslint)\b"
)
# An extra implementation request is required. Topic nouns in prose/UI are insufficient.
_BACKEND_REQUEST = re.compile(
    r"(?:^|[.!?;\n]|\band\b|\bthen\b|\balso\b)\s*(?:please\s+)?"
    r"(?:add|implement|fix|create|migrate|alter|remove|refactor|optimize)\s+"
    r"(?:(?:the|a|an|new)\s+)?(?:[\w/-]+\s+){0,3}"
    r"(?:endpoint|migration|table|sql query|database|backend|server|cron|cli flag|queue|api route)\b"
)
_CLAUSES = re.compile(r"[.!?;\n]+(?!\w)")


@dataclass(frozen=True)
class TaskRoleDecision:
    status: str
    workload: str
    matched: tuple[str, ...] = ()
    vetoed: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "schema": "brigade.task_role.v1",
            "status": self.status,
            "workload": self.workload,
            "role": role_for_workload(self.workload),
            "matched": list(self.matched),
            "vetoed": list(self.vetoed),
            "markers": list(self.markers),
        }


def role_for_workload(workload: str) -> str:
    return workload if workload in TASK_WORKLOADS else "impl"


def classify(text: str) -> TaskRoleDecision:
    text = text[:MAX_TASK_CHARS].lower()
    matched: set[str] = set()
    vetoed: set[str] = set()
    for clause in _CLAUSES.split(text):
        mechanical = tuple(match.group() for match in _MECHANICAL.finditer(clause))
        vetoed.update(mechanical)
        # Veto the mechanical phrase rather than unrelated audience work in the clause.
        intent = _MECHANICAL.sub("", clause)
        if _WRITING_PHRASE.search(intent) or (_WRITING_VERB.search(intent) and _WRITING_OBJECT.search(intent)):
            matched.add("writing")
        if _FRONTEND_PHRASE.search(intent) or (_FRONTEND_VERB.search(intent) and _FRONTEND_OBJECT.search(intent)):
            matched.add("frontend")
    markers = tuple(sorted({match.group() for match in _BACKEND_REQUEST.finditer(text)}))
    ordered = tuple(role for role in TASK_WORKLOADS if role in matched)
    if matched and markers:
        return TaskRoleDecision("ambiguous", "general", ordered, tuple(sorted(vetoed)), markers)
    if matched:
        workload = "frontend" if "frontend" in matched else "writing"
        return TaskRoleDecision("matched", workload, ordered, tuple(sorted(vetoed)))
    return TaskRoleDecision("none", "general", vetoed=tuple(sorted(vetoed)), markers=markers)


def strict_roster(roster: Roster, seat: str) -> Roster:
    """Copy only the selected seat's fallback, preserving unrelated routing."""
    agents = dict(roster.agents)
    agents[seat] = replace(agents[seat], fallback=())
    return replace(roster, agents=agents)
