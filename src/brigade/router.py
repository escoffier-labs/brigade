"""Deterministic route composer: assembles a stage route from a catalog + live signals.

Pure function of (catalog, live signals, available artifacts, already run). No I/O,
no LLM: routing decisions are code, so they are testable and reproducible. The
orchestrator agent still writes the plan; the route constrains what the plan must
cover and records why each stage joined.

Algorithm adapted from alp-river (https://github.com/alp82/alp-river, MIT,
(c) 2026 Alper Ortac): signal subscriptions with family-prefix matching, path
filtering, unsatisfiable-input drops, while/until scheduling locks, and a
topological sort into parallel waves.

Catalog shape (see route_catalog.DEFAULT_CATALOG):

    {"stages": {"<name>": {
        "routes": ["code", ...],            # paths the stage runs on
        "data": {"input": {"required": [...], "optional": [...]},
                  "output": [...]},          # artifact names, no sigils
        "signals": {"subscribes": [...], "publishes": [...]},
        "lock": [{"while": "<signal>", "until": "<signal>"}],  # optional
        "brief": "<one-line role>",          # shown in the route brief
    }}}

A stage joins the route when a live signal matches one of its subscriptions.
A held stage (active lock) is reported in `held`, never dispatched. Stages whose
required inputs no in-route stage produces are dropped as unsatisfiable.
"""

from __future__ import annotations

_SIZES = [(1, "XS"), (3, "S"), (6, "M"), (10, "L"), (15, "XL")]
# The routing paths a stage may run on; derive_signals publishes exactly one.
PATHS = ("code", "docs", "system")


def size_label(n: int) -> str:
    if n <= 0:
        return "empty"
    for hi, label in _SIZES:
        if n <= hi:
            return label
    return "XXL"


def _required(stage: dict) -> list[str]:
    return stage["data"]["input"]["required"]


def _inputs(stage: dict) -> list[str]:
    # Required and optional both create an ordering edge when their producer is
    # in-route; an optional input absent from the route just creates no edge.
    return stage["data"]["input"]["required"] + stage["data"]["input"]["optional"]


def _matches(sub: str, live: set[str]) -> bool:
    """A subscription matches a live topic exactly or as a family base:
    subscribing `findings` matches `findings:correctness`."""
    return any(topic == sub or topic.startswith(sub + ":") for topic in live)


def _active_locks(stage: dict, live: set[str]) -> list[dict]:
    """Locks currently holding the stage: `while` live, `until` not yet live."""
    return [
        lock for lock in stage.get("lock", []) if _matches(lock["while"], live) and not _matches(lock["until"], live)
    ]


def _trigger(stages: dict, live: set[str], already_run: set[str]) -> dict[str, str]:
    triggered = {}
    for name, stage in stages.items():
        if name in already_run:
            continue
        match = next((sig for sig in stage["signals"]["subscribes"] if _matches(sig, live)), None)
        if match is not None:
            triggered[name] = match
    return triggered


def _on_live_path(stages: dict, triggered: dict[str, str], live: set[str]) -> dict[str, str]:
    """Drop a triggered stage whose `routes` exclude the live path. With no path
    signal live, there is nothing to filter against - keep everything."""
    live_paths = {p for p in PATHS if p in live}
    if not live_paths:
        return dict(triggered)
    return {n: sig for n, sig in triggered.items() if set(stages[n]["routes"]) & live_paths}


def _drop_unsatisfiable(stages: dict, triggered: dict[str, str], available: set[str]) -> dict[str, str]:
    kept = dict(triggered)
    while True:
        producers: dict[str, set[str]] = {}
        for name in kept:
            for art in stages[name]["data"]["output"]:
                producers.setdefault(art, set()).add(name)
        unsatisfiable = [
            name
            for name in kept
            # A stage never satisfies its own required input: producers must
            # include someone else, or the artifact must already be available.
            if any(art not in available and not (producers.get(art, set()) - {name}) for art in _required(stages[name]))
        ]
        if not unsatisfiable:
            return kept
        for name in unsatisfiable:
            del kept[name]


def _toposort(stages: dict, names) -> tuple[list[str], list[list[str]]]:
    names = set(names)
    producers: dict[str, set[str]] = {}
    for name in names:
        for art in stages[name]["data"]["output"]:
            producers.setdefault(art, set()).add(name)
    edges: dict[str, set[str]] = {n: set() for n in names}
    indegree = {n: 0 for n in names}
    for name in names:
        preds: set[str] = set()
        for art in _inputs(stages[name]):
            preds |= producers.get(art, set())
        preds.discard(name)
        for pred in preds:
            if name not in edges[pred]:
                edges[pred].add(name)
                indegree[name] += 1
    # Kahn's algorithm by levels: each frontier is a parallel wave whose data
    # dependencies are all satisfied by earlier waves.
    frontier = sorted(n for n in names if indegree[n] == 0)
    waves: list[list[str]] = []
    seen: set[str] = set()
    while frontier:
        waves.append(frontier)
        seen.update(frontier)
        upcoming: set[str] = set()
        for name in frontier:
            for consumer in edges[name]:
                indegree[consumer] -= 1
                if indegree[consumer] == 0:
                    upcoming.add(consumer)
        frontier = sorted(upcoming)
    if len(seen) < len(names):
        # A cycle can only come from a malformed catalog. Refuse loudly rather
        # than emit a wave whose inputs can never exist when it starts.
        cycle = ", ".join(sorted(names - seen))
        raise ValueError(f"route catalog has a dependency cycle involving: {cycle}")
    order = [name for wave in waves for name in wave]
    return order, waves


def compute_route(catalog: dict, live_signals, available=(), already_run=()) -> dict:
    """Compose the route for the current state.

    Returns {"route", "waves", "size", "triggered_by", "dropped", "held"}.
    Held stages live in `held` keyed by their unmet `until` signals; a stage
    dropped only because its producer is held reads as unsatisfiable-input.
    """
    stages = catalog["stages"]
    live, available = set(live_signals), set(available)
    triggered = _trigger(stages, live, set(already_run))
    on_path = _on_live_path(stages, triggered, live)
    kept = _drop_unsatisfiable(stages, on_path, available)
    active = {name: _active_locks(stages[name], live) for name in kept}
    locked = {name for name, locks in active.items() if locks}
    # A held stage contributes no output, so re-drop downstream consumers that
    # now lack a producer.
    runnable = _drop_unsatisfiable(stages, {n: kept[n] for n in kept if n not in locked}, available)
    order, waves = _toposort(stages, runnable)
    held = {name: [lock["until"] for lock in active[name]] for name in locked}
    dropped = {}
    for name in triggered:
        if name not in on_path:
            dropped[name] = "off-path"
        elif name not in order and name not in held:
            dropped[name] = "unsatisfiable-input"
    return {
        "route": order,
        "waves": waves,
        "size": size_label(len(order)),
        "triggered_by": {name: triggered[name] for name in order},
        "dropped": dropped,
        "held": held,
    }


# alp-river's sticky-stage merge (a guard stage triggered earlier survives its
# signal going quiet) is deliberately not ported: it only means something across
# recomposes, and brigade composes the route once per run. If a recompose loop
# lands, port it hold-aware - the original re-adds stages without re-checking
# locks or input satisfiability.
