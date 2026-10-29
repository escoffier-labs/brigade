"""Bounded, local task intent classification shared by clients and the Hub."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from .roster import Roster

TASK_WORKLOADS: tuple[str, ...] = ("writing", "frontend")
MAX_TASK_CHARS = 8000
# Recognize requested actions at clause boundaries, rather than incidental nouns.
_WRITING_VERBS = frozenset(
    {"draft", "write", "rewrite", "edit", "copyedit", "proofread", "polish", "tighten", "revise", "update"}
)
_VISUAL_VERBS = frozenset(
    {
        "design",
        "redesign",
        "restyle",
        "style",
        "mock up",
        "lay out",
        "critique",
        "improve",
        "make",
        "implement",
        "build",
    }
)
_ACTION_WORDS = (
    r"draft|write|rewrite|edit|copyedit|proofread|polish|tighten|revise|update|"
    r"design|redesign|restyle|style|mock up|lay out|critique|improve|make|implement|build|"
    r"add|fix|create|migrate|alter|remove|refactor|optimize|wire|bump|configure"
)
_ACTION = re.compile(r"^\s*(?:(?:please|only)\s+)?(?P<verb>" + _ACTION_WORDS + r")\b")
_CLAUSE_BOUNDARY = re.compile(
    r"[.!?;\n]+(?!\w)|(?:,\s*(?:(?:and(?:\s+also)?|then|also|plus|but)\s+)?|\s+(?:and(?:\s+also)?|then|also|plus|but)\s+)"
    r"(?=(?:please\s+)?(?:(?:do not|don't|never|avoid)\b|(?:" + _ACTION_WORDS + r")\b))"
)
_QUOTED = re.compile(r"\"[^\"\n]*\"|(?<!\w)'[^'\n]*'(?!\w)")
_NEGATED = re.compile(r"^\s*(?:please\s+)?(?:do not|don't|never|avoid)\b")
_TOPIC = re.compile(
    r"\b(?:about|explaining|describing|discussing|titled|called|for|on|of|covering|announcing|with|in|to)\b"
)
_HOW_TO = re.compile(r"\bhow to\b")
_WRITING_OBJECT = re.compile(
    r"(?<![\w-])(?:blog(?: post)?|article|essay|newsletter|announcement|post|thread|tweet|email|"
    r"cover letter|resume|bio|linkedin(?: post| summary)?|paragraph|tutorial|guide|docs page|prose|readme (?:prose|intro)|"
    r"pr (?:description|body|messages?)|pull request (?:description|body)|landing copy|headline|"
    r"tagline|abstract|proposal|changelog entr(?:y|ies)|release notes|commit messages?)(?![\w-])"
)
_VISUAL_OBJECT = re.compile(
    r"(?<![\w-])(?:ui|page|hero(?: section)?|layout|contrast|typography|colou?r|palette|spacing|css|tailwind|theme|"
    r"dark mode|mobile view|visual hierarchy|screenshot|animation|navbar|mockup)(?![\w-])"
)
# A known object head must close its phrase. Unknown modifiers and later keywords
# cannot turn a different object into audience or visual work.
_OBJECT_PREFIX = re.compile(
    r"\s*(?:(?:a|an|the|this|that|my|our|your|their|[a-z][a-z-]*'s)\s+){0,2}"
    r"(?:(?:short|long|new|existing|release|weekly|monthly|responsive|restyled|"
    r"landing|login|settings|dashboard|pricing|home|visual|mobile|dark|light)\s+){0,3}"
)
_WRITING_COMPLEMENT = re.compile(r"(?:'s\s+)?(?:intro|introduction|summary|body|title|copy)\s*$")
_VISUAL_COMPLEMENT = re.compile(
    r"(?:'s\s+)?(?:spacing|typography|palette|contrast)(?:\s+and\s+(?:spacing|typography|palette|contrast))*\s*$"
    r"|visually\s*$|look\s+(?:better|good)(?:\s+.*)?$"
)
_VISUAL_MAKE = re.compile(r"\blook (?:better|good)|\b(?:spacing|typography|palette|responsive)\b")
_PHRASE = re.compile(r"^\s*(prose polish|visual design|visual polish|visual critique|ui design|frontend design)\b")
# Inspect only the bounded action object, before prose topics or audience qualifiers.
# Categories, never matched text, are the only evidence returned to callers.
_CODE_OBJECTS = (
    ("endpoint", re.compile(r"\b(?:endpoints?|handlers?|routes?|requests?)\b|\b(?:get|post|put|patch|delete)\s+/")),
    ("migration", re.compile(r"\bmigrat(?:e|ion|ions)\b")),
    ("database", re.compile(r"\b(?:database|tables?|schema|sql|queries|query)\b")),
    ("api", re.compile(r"\bapi\b")),
    (
        "code",
        re.compile(
            r"\b(?:functions?|class(?:es)?|logic|parser|hooks?|tests?|cache|scheduler|backend|server|"
            r"allocator|memory|pool|worker|importer|validation|bug|cron|queue|cli flag|file write|database write|"
            r"build (?:tool|tooling|pipeline|script|config)|vite|webpack|esbuild|rollup|bundler|tsconfig|eslint)\b"
        ),
    ),
)


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


def _instruction_clauses(text: str) -> list[str]:
    clauses = []
    start = 0
    for boundary in _CLAUSE_BOUNDARY.finditer(text):
        context = text[start : boundary.start()]
        # Conjoined steps can continue a negation or an explicit how-to topic.
        # Commas, sentences, and "then" still introduce independent actions.
        if (
            boundary.group().strip() in {"and", "and also", "plus"}
            and not context.rstrip().endswith(",")
            and (_NEGATED.match(context) or _HOW_TO.search(context))
        ):
            continue
        clauses.append(context)
        start = boundary.end()
    clauses.append(text[start:])
    return clauses


def _closed_object(context: str, pattern: re.Pattern[str], complement: re.Pattern[str]) -> bool:
    """Accept only the first relevant head, with a bounded prefix and closed tail."""
    head = pattern.search(context)
    if head is None or _OBJECT_PREFIX.fullmatch(context[: head.start()]) is None:
        return False
    tail = context[head.end() :].strip()
    return not tail or complement.fullmatch(tail) is not None


def classify(text: str) -> TaskRoleDecision:
    text = text[:MAX_TASK_CHARS].lower()
    matched: set[str] = set()
    vetoed: set[str] = set()
    markers: set[str] = set()
    if _QUOTED.search(text):
        vetoed.add("quoted-content")
    text = _QUOTED.sub("", text)
    for clause in _instruction_clauses(text):
        if _NEGATED.search(clause):
            vetoed.add("negated-action")
            continue
        phrase = _PHRASE.match(clause)
        if phrase is not None and not _TOPIC.split(clause[phrase.end() :], maxsplit=1)[0].strip():
            matched.add("writing" if phrase.group(1) == "prose polish" else "frontend")
            continue
        action = _ACTION.match(clause)
        if action is None:
            continue
        verb = action.group("verb")
        # No task text from this context may enter diagnostic or persisted evidence.
        context = _TOPIC.split(clause[action.end() :], maxsplit=1)[0][:320]
        code_category = next((category for category, pattern in _CODE_OBJECTS if pattern.search(context)), None)
        writing = verb in _WRITING_VERBS and _closed_object(context, _WRITING_OBJECT, _WRITING_COMPLEMENT)
        visual = verb in _VISUAL_VERBS and _closed_object(context, _VISUAL_OBJECT, _VISUAL_COMPLEMENT)
        if verb == "make" and not _VISUAL_MAKE.search(context):
            visual = False
        # Implementation-only verbs are requests even with an unfamiliar object.
        # Build/implement are visual only when the requested object proves that.
        implementation = verb not in _WRITING_VERBS | _VISUAL_VERBS or (verb in {"build", "implement"} and not visual)
        if code_category is not None or implementation:
            vetoed.add("code-context")
            markers.add(f"backend-request:{code_category or 'code'}")
            continue
        if writing:
            matched.add("writing")
        if visual:
            matched.add("frontend")
    ordered = tuple(role for role in TASK_WORKLOADS if role in matched)
    if matched and markers:
        return TaskRoleDecision("ambiguous", "general", ordered, tuple(sorted(vetoed)), tuple(sorted(markers)))
    if matched:
        workload = "frontend" if "frontend" in matched else "writing"
        return TaskRoleDecision("matched", workload, ordered, tuple(sorted(vetoed)))
    return TaskRoleDecision("none", "general", vetoed=tuple(sorted(vetoed)), markers=tuple(sorted(markers)))


def strict_roster(roster: Roster, seat: str) -> Roster:
    """Clear both seat fallback mechanisms on a copy of the selected seat."""
    agents = dict(roster.agents)
    agents[seat] = replace(agents[seat], fallback=(), invalid_final_fallback=None)
    return replace(roster, agents=agents)
