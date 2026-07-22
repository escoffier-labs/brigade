"""Brigade's default route catalog and the signal derivation that feeds it.

The catalog names the stages a run can require and the signals that pull each
one in. `derive_signals` maps a task description (plus optional template and
changed paths) to signals deterministically - keyword and path heuristics, no
model call - so the same task always composes the same route.

The route is advisory-but-checked: the orchestrator agent still writes the
plan, but the plan prompt carries the route brief and `uncovered_stages`
verifies every required stage is covered by some assignment. Held stages
(ship, destructive system steps) must not be executed by any worker; they wait
for explicit user approval signals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import router

# Signals a user or caller grants explicitly; never derived from task text.
APPROVAL_SIGNALS = ("ship-approved", "destructive-approved")


def _stage(
    routes: list[str],
    subscribes: list[str],
    brief: str,
    *,
    required: list[str] | None = None,
    optional: list[str] | None = None,
    output: list[str] | None = None,
    publishes: list[str] | None = None,
    lock: list[dict[str, str]] | None = None,
) -> dict:
    stage: dict = {
        "routes": routes,
        "brief": brief,
        "data": {
            "input": {"required": required or [], "optional": optional or []},
            "output": output or [],
        },
        "signals": {"subscribes": subscribes, "publishes": publishes or []},
    }
    if lock:
        stage["lock"] = lock
    return stage


DEFAULT_CATALOG: dict = {
    "stages": {
        "investigate": _stage(
            ["code", "system"],
            ["bug"],
            "Root-cause the reported defect before any fix: hypothesize, reproduce, trace.",
            required=["task"],
            output=["diagnosis"],
        ),
        "plan": _stage(
            ["code"],
            ["significant-build"],
            "Turn intent into concrete ordered steps before code is written.",
            required=["task"],
            optional=["diagnosis"],
            output=["plan"],
        ),
        "test-author": _stage(
            ["code"],
            ["needs-tests"],
            "Write the failing tests first, from acceptance criteria, before implementation.",
            required=["task"],
            optional=["plan", "diagnosis"],
            output=["red-tests"],
        ),
        "implement": _stage(
            ["code"],
            ["code"],
            "Make the change. Runs after plan and red tests when those stages are in-route.",
            required=["task"],
            optional=["plan", "red-tests", "diagnosis"],
            output=["diff"],
            publishes=["code-written"],
        ),
        "correctness-review": _stage(
            ["code"],
            ["code"],
            "Review the diff for logic errors, unhandled failure paths, and unguarded premises.",
            required=["diff"],
            output=["findings"],
            publishes=["findings:correctness"],
        ),
        "security-review": _stage(
            ["code", "system"],
            ["auth-surface"],
            "Review auth, secrets, and permission surfaces touched by the change.",
            required=["diff"],
            output=["findings"],
            publishes=["findings:security"],
        ),
        "perf-review": _stage(
            ["code"],
            ["perf-surface"],
            "Check the touched hot paths: complexity, allocations, query patterns.",
            required=["diff"],
            output=["findings"],
            publishes=["findings:performance"],
        ),
        "ux-review": _stage(
            ["code"],
            ["ui-touched"],
            "Review UI changes for interaction regressions and visual consistency.",
            required=["diff"],
            output=["findings"],
            publishes=["findings:ux"],
        ),
        "migration-review": _stage(
            ["code", "system"],
            ["migration"],
            "Check schema or data migrations for retry safety, deploy-window hazards, and rollback.",
            required=["diff"],
            output=["findings"],
            publishes=["findings:migration"],
        ),
        "test-gap-review": _stage(
            ["code"],
            ["needs-tests"],
            "Verify the tests actually cover the change: missing cases, assertions that cannot fail.",
            required=["diff", "red-tests"],
            output=["findings"],
            publishes=["findings:test-gap"],
        ),
        "verify": _stage(
            ["code", "docs"],
            ["code", "docs"],
            "Run the project checks through `brigade work verify run` so the result lands as a receipt.",
            required=["diff"],
            output=["verify-receipt"],
            publishes=["verified"],
        ),
        "docs-edit": _stage(
            ["docs"],
            ["docs"],
            "Make the documentation change.",
            required=["task"],
            output=["diff"],
        ),
        "system-plan": _stage(
            ["system"],
            ["system"],
            "Plan the machine change as ordered, reversible steps with backup and rollback.",
            required=["task"],
            optional=["diagnosis"],
            output=["system-plan"],
        ),
        "system-execute": _stage(
            ["system"],
            ["system"],
            "Run the planned steps one at a time.",
            required=["system-plan"],
            output=["system-state"],
            lock=[{"while": "destructive-op", "until": "destructive-approved"}],
        ),
        "system-verify": _stage(
            ["system"],
            ["system"],
            "Confirm the change actually reached its intended state.",
            required=["system-state"],
            output=["findings"],
        ),
        "ship": _stage(
            ["code", "docs"],
            ["ship-requested"],
            "Commit, push, open the PR - only after explicit approval.",
            required=["diff"],
            output=["shipped"],
            lock=[{"while": "ship-requested", "until": "ship-approved"}],
        ),
    }
}

# Keyword/path heuristics. Word-boundary matching keeps `ui` from firing on
# `build`, `auth` on `author` is excluded by the negative guard below.
_SIGNAL_PATTERNS: list[tuple[str, str]] = [
    ("bug", r"\b(bug|fix(es|ed)?|broken|regression|crash(es|ing)?|fails?|failing|error)\b"),
    (
        "auth-surface",
        r"\b(auth(entication|orization)?|login|logout|token|secret|password|credential|permission|oauth|session|api.?key)s?\b",
    ),
    (
        "ui-touched",
        r"\b(ui|frontend|front-end|component|css|styling|layout|button|modal|dialog|form|page|screen|responsive"
        r"|panel|dashboard|widget|sidebar|navbar|nav menu|tooltip|view)s?\b",
    ),
    (
        "perf-surface",
        r"\b(performance|perf|latency|slow|optimi[sz]e|cache|caching|benchmark|n\+1|throughput|memory)\b",
    ),
    ("migration", r"\b(migration|migrate|schema|alter table|backfill|data model)\b"),
    (
        "destructive-op",
        r"\b(rm -rf|drop (table|database)|delete (all|the) |force.?push|reset --hard|wipe|purge)\b",
    ),
    ("ship-requested", r"\b(ship|commit and push|open a pr|pull request|release|publish|deploy)\b"),
    (
        "significant-build",
        r"\b(refactor|architecture|end.to.end|redesign|rewrite|new (feature|module|service|command)|integrate|implement)\b",
    ),
]

_AUTH_FALSE_POSITIVE = re.compile(r"\bauthor(s|ed|ing|ship)?\b")

_DOCS_TEMPLATE_HINTS = {"docs", "documentation"}
_SYSTEM_HINT = re.compile(
    r"\b(systemd|cron(tab)?|install (a )?package|apt |dns|firewall|server config|nginx|reverse proxy)\b"
)
# System words inside a repo-editing task ("fix the nginx config template in the
# repo") describe code, not the machine. The repo hint vetoes the system path.
_REPO_HINT = re.compile(
    r"\b(in (the|this|our) (repo|codebase|project)|repo file|template|source (code|file)|\.py|\.ts|\.go|\.rs)\b"
)
_DOCS_HINT = re.compile(
    r"\b(readme|changelog|docs?( page| site)?|documentation|typo|quickstart|quick start"
    r"|guide|tutorial|contributing)\b"
)
# A conventional-commit code prefix means the task is code work even when it
# mentions docs in passing: `fix(install): ... referencing docs` is a code fix,
# not a docs edit. Prose "fix typo in README" has no colon and stays docs. A
# docs-ish scope (`fix(docs): broken link`) is genuinely docs, so it is exempt.
_CODE_COMMIT_PREFIX = re.compile(r"^(feat|fix|refactor|perf)(\(([^)]*)\))?!?:", re.IGNORECASE)
_DOCS_SCOPE = {"docs", "doc", "readme", "changelog", "guide"}
_UI_PATH = re.compile(r"\.(tsx|jsx|vue|svelte|css|scss)$")
# Path surfaces match whole path segments, never substrings: `author.py` must
# not fire the auth surface, `tokenizer.md` must not fire on token.
_MIGRATION_SEGMENTS = {"migration", "migrations", "schema", "schemas"}
_AUTH_SEGMENTS = {
    "auth",
    "authn",
    "authz",
    "token",
    "tokens",
    "secret",
    "secrets",
    "session",
    "sessions",
    "permission",
    "permissions",
    "oauth",
    "login",
    "credentials",
}
_PATH_SPLIT = re.compile(r"[/\\._\-]")


def _path_segments(raw: str) -> set[str]:
    return {segment for segment in _PATH_SPLIT.split(raw.lower()) if segment}


# Templates whose changes carry logic and therefore need tests written first.
_TESTED_TEMPLATES = {"vertical-slice", "bugfix", "security-follow-up"}


def _override_name(raw: str) -> str:
    """The signal a `+x` / `-x` / `~x` / bare token names, or '' for an empty token."""
    token = raw.strip()
    if not token:
        return ""
    return token[1:].strip() if token[0] in "+-~" else token


def validate_overrides(overrides) -> None:
    """Reject an override that targets a path signal. The path (code/docs/system)
    is a derive-time decision driven by the task and --template, not something a
    signal override may add or delete: suppressing it strips the whole route to a
    pathless remnant, and adding a second path scrambles the filter. Raises
    ValueError naming the offending token."""
    for raw in overrides:
        name = _override_name(raw)
        if name in router.PATHS:
            raise ValueError(
                f"cannot override the path signal {name!r} with --route-signal; "
                "the path is set by the task and --template"
            )


def _apply_overrides(signals: list[str], overrides) -> list[str]:
    """Apply operator overrides in order. `+x` appends x if absent; `-x` or `~x`
    drops every copy of x (`~` is the argparse-safe suppress form, since a bare
    `-x` value is read as a flag). A bare token is treated as `+`. Returns a new
    list, order-preserving and deduped. Rejects path-signal overrides first."""
    validate_overrides(overrides)
    result = list(signals)
    for raw in overrides:
        token = raw.strip()
        if not token:
            continue
        op, name = (token[0], token[1:].strip()) if token[0] in "+-~" else ("+", token)
        if not name:
            continue
        if op in "-~":
            result = [s for s in result if s != name]
        elif name not in result:
            result.append(name)
    return result


def derive_signals(task: str, template: str | None = None, changed_paths=(), overrides=()) -> list[str]:
    """Map a task description to route signals. Deterministic: same inputs,
    same signals. The path signal (code/docs/system) is always first.

    `overrides` are operator tokens (`+auth-surface`, `-ship-requested`) applied
    before the needs-tests derivation, so a forced `+auth-surface` still earns
    tests and a `-` suppression removes a signal from all downstream logic."""
    text = task.lower()
    signals: list[str] = []

    if template in _DOCS_TEMPLATE_HINTS or (
        _DOCS_HINT.search(text) and not _code_shaped_hit(text) and not _code_commit_hit(task)
    ):
        path = "docs"
    elif _SYSTEM_HINT.search(text) and not _REPO_HINT.search(text):
        path = "system"
    else:
        path = "code"
    signals.append(path)

    if path == "docs":
        # Docs tasks still carry the approval-gated signals: "fix typo in the
        # README and open a PR" must keep the ship hold.
        for signal, pattern in _SIGNAL_PATTERNS:
            if signal in ("ship-requested", "destructive-op") and re.search(pattern, text):
                signals.append(signal)
    else:
        for signal, pattern in _SIGNAL_PATTERNS:
            if re.search(pattern, text):
                if signal == "auth-surface" and not _real_auth_hit(text):
                    continue
                signals.append(signal)

    for raw in changed_paths:
        segments = _path_segments(raw)
        if _UI_PATH.search(raw) and "ui-touched" not in signals:
            signals.append("ui-touched")
        if segments & _MIGRATION_SEGMENTS and "migration" not in signals:
            signals.append("migration")
        if segments & _AUTH_SEGMENTS and "auth-surface" not in signals:
            signals.append("auth-surface")

    # Overrides land before the needs-tests derivation so a forced surface still
    # pulls its dependents and a suppression is gone before they are computed.
    if overrides:
        signals = _apply_overrides(signals, overrides)

    if signals and signals[0] == "code":
        # Auth-surface and migration work always earn tests: security-critical
        # logic and a data backfill are the last places to skip them.
        tested = (
            template in _TESTED_TEMPLATES
            or "significant-build" in signals
            or "bug" in signals
            or "auth-surface" in signals
            or "migration" in signals
        )
        if tested and "needs-tests" not in signals:
            signals.append("needs-tests")

    return signals


# Signals that mark a task as code work even when a docs hint is present:
# concrete code surfaces. `bug` and `ship-requested` are excluded ("fix typo in
# README" stays docs), and so is `significant-build` - "rewrite" and "redesign"
# describe a docs rewrite as readily as a code one, so a docs hint should win.
# "rewrite the QUICKSTART" is docs; "rewrite the auth module" stays code on its
# auth-surface hit.
_CODE_SHAPED = {"auth-surface", "ui-touched", "perf-surface", "migration"}


def _code_shaped_hit(text: str) -> bool:
    return any(re.search(pattern, text) for signal, pattern in _SIGNAL_PATTERNS if signal in _CODE_SHAPED)


def _code_commit_hit(task: str) -> bool:
    """A conventional-commit code prefix (feat/fix/refactor/perf) with a
    non-docs scope. Matches the raw task, not the lowercased copy, but the
    pattern is case-insensitive; the scope check is what excludes `fix(docs):`."""
    match = _CODE_COMMIT_PREFIX.match(task.strip())
    if match is None:
        return False
    scope = (match.group(3) or "").strip().lower()
    return scope not in _DOCS_SCOPE


def _real_auth_hit(text: str) -> bool:
    """`author`/`authored` must not fire the auth surface."""
    stripped = _AUTH_FALSE_POSITIVE.sub("", text)
    return bool(re.search(_SIGNAL_PATTERNS[1][1], stripped))


@dataclass(frozen=True)
class RouteBrief:
    """Deterministic route computed before planning; attached to the plan prompt."""

    attached: bool
    text: str = ""
    signals: tuple[str, ...] = ()
    approvals: tuple[str, ...] = ()
    overrides: tuple[str, ...] = ()
    route: tuple[str, ...] = ()
    waves: tuple[tuple[str, ...], ...] = ()
    held: dict = field(default_factory=dict)
    size: str = "empty"
    triggered_by: dict = field(default_factory=dict)
    dependencies: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def payload(self) -> dict:
        """Telemetry shape for run.json. Signals, approvals, and overrides
        reproduce the route decision exactly; triggered_by names the live
        signal that pulled each routed stage in."""
        return {
            "attached": self.attached,
            "signals": list(self.signals),
            "approvals": list(self.approvals),
            "overrides": list(self.overrides),
            "route": list(self.route),
            "waves": [list(w) for w in self.waves],
            "held": dict(self.held),
            "size": self.size,
            "triggered_by": dict(self.triggered_by),
            "dependencies": {k: list(v) for k, v in self.dependencies.items()},
        }


ROUTE_HEADING = "Route brief (deterministic):"


def route_brief(
    task: str,
    template: str | None = None,
    changed_paths=(),
    approvals=(),
    catalog: dict | None = None,
    overrides=(),
) -> RouteBrief:
    catalog = catalog or DEFAULT_CATALOG
    signals = derive_signals(task, template=template, changed_paths=changed_paths, overrides=overrides)
    granted = tuple(a for a in approvals if a in APPROVAL_SIGNALS)
    live = list(signals) + list(granted)
    result = router.compute_route(catalog, live, available=["task"])
    stages = catalog["stages"]
    lines = [ROUTE_HEADING]
    lines.append(f"Signals: {', '.join(signals)}. Route size: {result['size']}.")
    lines.append("Required stages, in dependency order (same wave = may run in parallel):")
    for index, wave in enumerate(result["waves"], start=1):
        for name in wave:
            reason = result["triggered_by"].get(name, "")
            brief = stages[name].get("brief", "")
            lines.append(f"- wave {index}: {name} (pulled by #{reason}) - {brief}")
    for name, untils in result["held"].items():
        lines.append(
            f"- HELD: {name} - waiting on {', '.join('#' + u for u in untils)}. "
            "No worker may perform this stage's actions; it needs explicit user approval."
        )
    lines.append(
        'Cover every required stage: each assignment may carry "covers": ["<stage>", ...] '
        "naming the stages it satisfies. One assignment may cover several stages; every "
        "listed stage must be covered by at least one assignment."
    )
    deps = router.stage_dependencies(catalog, [*result["route"], *result["held"]])
    return RouteBrief(
        attached=True,
        text="\n".join(lines) + "\n",
        signals=tuple(signals),
        approvals=granted,
        overrides=tuple(o.strip() for o in overrides if o.strip()),
        route=tuple(result["route"]),
        waves=tuple(tuple(w) for w in result["waves"]),
        held=result["held"],
        size=result["size"],
        triggered_by=result["triggered_by"],
        dependencies={name: tuple(sorted(preds)) for name, preds in deps.items()},
    )


def uncovered_stages(route: RouteBrief, assignments) -> list[str]:
    """Required stages no assignment covers. Empty when the plan is complete."""
    covered: set[str] = set()
    for assignment in assignments:
        covered.update(getattr(assignment, "covers", ()) or ())
    return [name for name in route.route if name not in covered]


def unknown_covers(route: RouteBrief, assignments) -> list[str]:
    """Covers tags that name no stage in the route: the plan claims to satisfy
    a stage that was never required, so the claim is hollow. Ordered, deduped.
    Distinct from uncovered_stages (real stages left uncovered)."""
    route_set = set(route.route)
    seen: dict[str, None] = {}
    for assignment in assignments:
        for tag in getattr(assignment, "covers", ()) or ():
            if tag not in route_set:
                seen.setdefault(tag, None)
    return list(seen)
