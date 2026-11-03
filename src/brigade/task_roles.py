"""Bounded, local task intent classification shared by clients and the Hub."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any
from pathlib import Path

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
_ACTION_START = r"(?:(?:please|only)\s+)?(?P<verb>" + _ACTION_WORDS + r")\b"
_NEGATED_START = r"(?:please\s+)?(?:do not|don't|never|avoid)\b"
_ACTION = re.compile(r"^\s*" + _ACTION_START)
_CLAUSE_BOUNDARY = re.compile(
    r"[.!?;\n]+(?!\w)|(?:,\s*(?:(?:and(?:\s+also)?|then|also|plus|but)\s+)?|\s+(?:and(?:\s+also)?|then|also|plus|but)\s+)"
    r"(?=(?:" + _NEGATED_START + "|" + _ACTION_START + "))"
)
_QUOTED = re.compile(r"\"[^\"\n]*\"|(?<!\w)'[^'\n]*'(?!\w)")
_NEGATED = re.compile(r"^\s*" + _NEGATED_START)
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
    """Accept only the first relevant head with a supported prefix and ending."""
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
        # Every recognized action contributes a role or implementation evidence.
        # An unsupported object must not disappear when combined with audience work.
        if code_category is not None or not (writing or visual):
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


def policy_roster(roster: Roster, seat: str, fallback: tuple[str, ...]) -> Roster:
    """Allow exactly the validated policy list, with no local fallback escape."""
    agents = dict(roster.agents)
    for name in (seat, *fallback):
        agents[name] = replace(agents[name], fallback=fallback if name == seat else (), invalid_final_fallback=None)
    return replace(roster, agents=agents)


def load_instructions(roster: Roster, role: str) -> tuple[str, dict[str, str] | None]:
    """Read host-local rules without persisting their contents or full path."""
    configured = roster.task_role_instructions.get(role)
    if not configured:
        return "", None
    path = Path(configured).expanduser()
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("file exceeds 64 KiB")
        text = raw.decode("utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"task role {role} instructions file {path.name!r} is unreadable or exceeds 64 KiB") from exc
    return text, {"file": path.name, "sha256": hashlib.sha256(raw).hexdigest()[:12]}


def retarget_worker_evidence(roster: Roster, *, requested: str, effective: str) -> Roster:
    """Point task-role evidence at the fallback seat that actually ran."""
    routing = tuple(
        {
            **entry,
            "task_role": {**evidence, "seat": effective, "requested_seat": requested, "effective_seat": effective},
        }
        if isinstance(evidence := entry.get("task_role"), dict) and evidence.get("seat") == requested
        else entry
        for entry in roster.seat_routing
    )
    return replace(roster, seat_routing=routing)
