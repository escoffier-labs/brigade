"""Consistency checks for the v2 hub plan (slice S30).

Reads ``docs/phase-v2-hub-architecture.md`` and cross-checks the slice tables,
the waves, the dependency list and the release gate. Stdlib only. Every failure
names the slice it concerns, and the negative tests at the bottom feed small
synthetic documents to the parser to show each failure mode fires.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

PLAN_PATH = Path(__file__).resolve().parents[1] / "docs" / "phase-v2-hub-architecture.md"

SLICE_ID = r"S\d+[a-z]?"
SLICE_RE = re.compile(rf"\b{SLICE_ID}\b")
RANGE_RE = re.compile(rf"\b({SLICE_ID})\s*(?:\bto\b|\.\.)\s*({SLICE_ID})\b")
EXTERNAL_RE = re.compile(r"#\d+|phase-3|connectivity-pr")
PARENS_RE = re.compile(r"\([^)]*\)")

# Failure kinds. Each failure carries the slice it is about.
MISSING_FROM_TABLES = "missing-from-tables"
MISSING_FROM_WAVES = "missing-from-waves"
MISSING_FROM_LIST = "missing-from-dependency-list"
DUPLICATE_IN_WAVES = "duplicate-in-waves"
DUPLICATE_IN_TABLES = "duplicate-in-tables"
DUPLICATE_IN_LIST = "duplicate-in-dependency-list"
EDGE_NOT_EARLIER = "edge-not-in-earlier-wave"
CELL_MISMATCH = "dependency-cell-mismatch"
GATE_OUTSIDE = "gate-prerequisite-outside-gate"
ACCEPTANCE_UNRELATED = "acceptance-names-unrelated-slice"
GATED_PREREQ_OUTSIDE_WAVES = "gated-prerequisite-outside-waves"
GATE_DEPENDS_ON_GATED = "gate-depends-on-gated"
UNRESOLVED_DEP = "unresolved-dependency"
GATED_IN_GATE = "gated-slice-in-release-gate"
PARSE = "parse"


@dataclass(frozen=True)
class Failure:
    slice: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.slice}: [{self.kind}] {self.detail}"


@dataclass(frozen=True)
class Deps:
    build: frozenset[str] = frozenset()
    verify: frozenset[str] = frozenset()
    external: frozenset[str] = frozenset()

    @property
    def slices(self) -> frozenset[str]:
        return self.build | self.verify


@dataclass
class Row:
    ids: list[str]
    deps: Deps
    acceptance: str


@dataclass
class Plan:
    rows: list[Row] = field(default_factory=list)
    # Slice id -> (wave label, rank). Rank orders waves; larger is later.
    placements: dict[str, tuple[str, int]] = field(default_factory=dict)
    placement_count: dict[str, int] = field(default_factory=dict)
    gated: set[str] = field(default_factory=set)
    deps_list: dict[str, Deps] = field(default_factory=dict)
    list_count: dict[str, int] = field(default_factory=dict)
    gate: set[str] = field(default_factory=set)
    gate_externals: set[str] = field(default_factory=set)
    problems: list[Failure] = field(default_factory=list)

    @property
    def table_count(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows:
            for slice_id in row.ids:
                counts[slice_id] = counts.get(slice_id, 0) + 1
        return counts


def _expand_range(start: str, end: str) -> list[str] | None:
    """Expand ``S23 to S26`` or ``S12a to S12i``. None when the range is not expressible."""
    ms = re.fullmatch(r"S(\d+)([a-z]?)", start)
    me = re.fullmatch(r"S(\d+)([a-z]?)", end)
    if ms is None or me is None:
        return None
    n1, l1 = int(ms.group(1)), ms.group(2)
    n2, l2 = int(me.group(1)), me.group(2)
    if not l1 and not l2 and n1 <= n2:
        return [f"S{n}" for n in range(n1, n2 + 1)]
    if n1 == n2 and l1 and l2 and l1 <= l2:
        return [f"S{n1}{chr(c)}" for c in range(ord(l1), ord(l2) + 1)]
    return None


def slice_ids(text: str, problems: list[Failure] | None = None) -> list[str]:
    """Every slice id in ``text``, with ``A to B`` and ``A..B`` ranges expanded, in order."""
    found: list[str] = []

    def expand(match: re.Match[str]) -> str:
        ids = _expand_range(match.group(1), match.group(2))
        if ids is None:
            if problems is not None:
                problems.append(Failure(match.group(1), PARSE, f"cannot expand the range {match.group(0)!r}"))
            return f" {match.group(1)} {match.group(2)} "
        return " " + " ".join(ids) + " "

    flat = RANGE_RE.sub(expand, text)
    for match in SLICE_RE.finditer(flat):
        found.append(match.group(0))
    return found


def _externals(text: str) -> set[str]:
    lowered = text.lower()
    found = set(re.findall(r"#\d+", lowered))
    if re.search(r"phase-3|cutover phase 3", lowered):
        found.add("phase-3")
    if re.search(r"connectivity-pr|connectivity pr", lowered):
        found.add("connectivity-pr")
    return found


def parse_deps(text: str, problems: list[Failure] | None = None) -> Deps:
    """Parse ``builds, ... Verify: slices`` (table cell) or ``builds verify: slices`` (list)."""
    parts = re.split(r"verify:", text, maxsplit=1, flags=re.IGNORECASE)
    build_text = parts[0]
    verify_text = parts[1] if len(parts) > 1 else ""
    return Deps(
        build=frozenset(slice_ids(build_text, problems)),
        verify=frozenset(slice_ids(verify_text, problems)),
        external=frozenset(_externals(build_text) | _externals(verify_text)),
    )


def _section(text: str, heading: str) -> str:
    """Body under ``heading`` up to the next heading of the same or a higher level."""
    lines = text.splitlines()
    level = len(heading) - len(heading.lstrip("#"))
    start = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start:]:
        match = re.match(r"(#+)\s", line)
        if match and len(match.group(1)) <= level:
            break
        body.append(line)
    return "\n".join(body)


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _fenced_blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append(current)
                current = None
        elif current is not None:
            current.append(line)
    return blocks


def _parse_rows(plan: Plan, text: str) -> None:
    body = _section(text, "## Build plan")
    for line in body.splitlines():
        if not re.match(rf"\|\s*{SLICE_ID}\b", line):
            continue
        cells = _split_row(line)
        if len(cells) != 5:
            plan.problems.append(Failure(cells[0], PARSE, f"table row has {len(cells)} cells, expected 5"))
            continue
        ids = slice_ids(cells[0], plan.problems)
        plan.rows.append(Row(ids=ids, deps=parse_deps(cells[2], plan.problems), acceptance=cells[3]))


def _place(plan: Plan, slice_id: str, label: str, rank: int) -> None:
    plan.placement_count[slice_id] = plan.placement_count.get(slice_id, 0) + 1
    plan.placements.setdefault(slice_id, (label, rank))


def _parse_waves(plan: Plan, block: list[str]) -> None:
    for line in block:
        wave = re.match(r"wave (\d+):\s*(.*)", line)
        if wave:
            for slice_id in slice_ids(PARENS_RE.sub(" ", wave.group(2)), plan.problems):
                _place(plan, slice_id, f"wave {wave.group(1)}", int(wave.group(1)))
            continue
        after = re.match(r"after phase 3:\s*(.*)", line)
        if after:
            segments = [s for s in re.sub(r",\s*then\b", ",", after.group(1)).split(",") if s.strip()]
            for step, segment in enumerate(segments):
                head = re.split(r"\bafter\b", segment, maxsplit=1)[0]
                for slice_id in slice_ids(head, plan.problems):
                    _place(plan, slice_id, f"after phase 3 (step {step + 1})", 1000 + step)
            continue
        gated = re.match(r"gated, outside the release:\s*(.*)", line)
        if gated:
            head = re.split(r"\bafter\b", gated.group(1), maxsplit=1)[0]
            for slice_id in slice_ids(head, plan.problems):
                plan.gated.add(slice_id)
                _place(plan, slice_id, "gated line", 2000)


def _parse_dep_list(plan: Plan, block: list[str]) -> None:
    for line in block:
        match = re.match(rf"({SLICE_ID}(?:\.\.{SLICE_ID})?):\s*(.*)", line)
        if match is None:
            continue
        deps = parse_deps(match.group(2), plan.problems)
        for slice_id in slice_ids(match.group(1), plan.problems):
            plan.list_count[slice_id] = plan.list_count.get(slice_id, 0) + 1
            plan.deps_list[slice_id] = deps


def _parse_gate(plan: Plan, text: str) -> None:
    body = _section(text, "## Release gate for 0.31.0")
    bullets: list[str] = []
    for line in body.splitlines():
        if line.startswith("- "):
            bullets.append(line[2:].strip())
        elif bullets and line.strip():
            bullets[-1] += " " + line.strip()
    for bullet in bullets:
        label, _, rest = bullet.partition(":")
        if label in {"Hub", "Memory", "Connectivity"}:
            first_sentence = re.split(r"\.(?:\s|$)", PARENS_RE.sub(" ", rest), maxsplit=1)[0]
            plan.gate.update(slice_ids(first_sentence, plan.problems))
        elif label == "External prerequisites":
            plan.gate_externals.update(re.findall(r"#\d+", rest))
            if "`connectivity-pr`" in rest:
                plan.gate_externals.add("connectivity-pr")
            if "`phase-3`" in rest:
                plan.gate_externals.add("phase-3")


def parse_plan(text: str) -> Plan:
    plan = Plan()
    _parse_rows(plan, text)
    lanes = _section(text, "### Order and lanes")
    for block in _fenced_blocks(lanes):
        if any(re.match(r"wave \d+:", line) for line in block):
            _parse_waves(plan, block)
        elif any(re.match(rf"{SLICE_ID}(?:\.\.{SLICE_ID})?:", line) for line in block):
            _parse_dep_list(plan, block)
    _parse_gate(plan, text)
    return plan


def _graph(plan: Plan) -> dict[str, Deps]:
    graph: dict[str, Deps] = {}
    for row in plan.rows:
        for slice_id in row.ids:
            graph[slice_id] = row.deps
    graph.update(plan.deps_list)
    return graph


def _closure(graph: dict[str, Deps], start: Iterable[str]) -> tuple[set[str], set[str]]:
    """Every slice and every external reachable from ``start`` through build and verify edges."""
    seen: set[str] = set()
    externals: set[str] = set()
    stack = list(start)
    while stack:
        current = stack.pop()
        deps = graph.get(current)
        if deps is None:
            continue
        externals |= deps.external
        for nxt in deps.slices:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen, externals


def check_plan(plan: Plan) -> list[Failure]:
    failures: list[Failure] = list(plan.problems)
    graph = _graph(plan)
    table_count = plan.table_count
    table_ids = set(table_count)

    # Waves versus the slice tables, and duplicates.
    for slice_id, count in sorted(plan.placement_count.items()):
        if slice_id not in table_ids:
            failures.append(
                Failure(slice_id, MISSING_FROM_TABLES, "placed in the waves but has no row in the slice tables")
            )
        if count > 1:
            failures.append(Failure(slice_id, DUPLICATE_IN_WAVES, f"appears {count} times in the waves"))
    for slice_id, count in sorted(table_count.items()):
        if count > 1:
            failures.append(Failure(slice_id, DUPLICATE_IN_TABLES, f"has {count} rows in the slice tables"))
        if slice_id not in plan.placements:
            failures.append(Failure(slice_id, MISSING_FROM_WAVES, "has a table row but is not placed in the waves"))
        if slice_id not in plan.deps_list:
            failures.append(Failure(slice_id, MISSING_FROM_LIST, "has a table row but no line in the dependency list"))
    for slice_id, count in sorted(plan.list_count.items()):
        if slice_id not in table_ids:
            failures.append(
                Failure(slice_id, MISSING_FROM_TABLES, "has a dependency list line but no row in the slice tables")
            )
        if count > 1:
            failures.append(Failure(slice_id, DUPLICATE_IN_LIST, f"has {count} lines in the dependency list"))

    # Table dependency cell against the dependency list.
    for row in plan.rows:
        for slice_id in row.ids:
            listed = plan.deps_list.get(slice_id)
            if listed is None or listed == row.deps:
                continue
            failures.append(
                Failure(
                    slice_id,
                    CELL_MISMATCH,
                    "table cell has "
                    f"build {sorted(row.deps.build)}, verify {sorted(row.deps.verify)}, external {sorted(row.deps.external)}; "
                    "dependency list has "
                    f"build {sorted(listed.build)}, verify {sorted(listed.verify)}, external {sorted(listed.external)}",
                )
            )

    # Every build or verify edge points at an earlier wave.
    for slice_id, deps in sorted(graph.items()):
        here = plan.placements.get(slice_id)
        if here is None:
            continue
        for kind, targets in (("build", deps.build), ("verify", deps.verify)):
            for target in sorted(targets):
                there = plan.placements.get(target)
                if there is None:
                    if target not in table_ids:
                        # A typo repeated in both dependency representations would
                        # otherwise pass: the target has no row and no placement.
                        failures.append(
                            Failure(
                                slice_id,
                                UNRESOLVED_DEP,
                                f"{kind} edge to {target}, which has no slice table row and no wave placement",
                            )
                        )
                    continue
                if there[1] >= here[1]:
                    failures.append(
                        Failure(
                            slice_id,
                            EDGE_NOT_EARLIER,
                            f"{kind} edge to {target} ({there[0]}) is not in a wave earlier than {slice_id} ({here[0]})",
                        )
                    )

    # A slice marked gated, outside the release, cannot also be a release requirement.
    for slice_id in sorted(plan.gate & plan.gated):
        failures.append(
            Failure(slice_id, GATED_IN_GATE, "is listed in the release gate but marked gated, outside the release")
        )

    # Release gate closure.
    for slice_id in sorted(plan.gate):
        if slice_id not in table_ids:
            failures.append(
                Failure(slice_id, MISSING_FROM_TABLES, "is in the release gate but has no row in the slice tables")
            )
        reach, externals = _closure(graph, [slice_id])
        for dep in sorted(reach):
            if dep in plan.gated:
                failures.append(
                    Failure(slice_id, GATE_DEPENDS_ON_GATED, f"release-gate slice depends on the gated slice {dep}")
                )
            elif dep not in plan.gate:
                failures.append(Failure(slice_id, GATE_OUTSIDE, f"prerequisite {dep} is outside the release gate"))
        for external in sorted(externals - plan.gate_externals):
            failures.append(
                Failure(
                    slice_id,
                    GATE_OUTSIDE,
                    f"external prerequisite {external} is not listed under the release gate's external prerequisites",
                )
            )

    # Gated slices need their prerequisites in the numbered waves.
    for slice_id in sorted(plan.gated):
        reach, _ = _closure(graph, [slice_id])
        for dep in sorted(reach):
            placed = plan.placements.get(dep)
            if placed is None or not placed[0].startswith("wave "):
                label = "not placed in the waves" if placed is None else placed[0]
                failures.append(
                    Failure(
                        slice_id,
                        GATED_PREREQ_OUTSIDE_WAVES,
                        f"gated slice has the prerequisite {dep} ({label}), outside the numbered waves",
                    )
                )

    # Slice ids in an acceptance cell are the row's own slice or a prerequisite.
    for row in plan.rows:
        reach, _ = _closure(graph, row.ids)
        allowed = set(row.ids) | reach
        for slice_id in row.ids:
            for named in dict.fromkeys(slice_ids(row.acceptance)):
                if named not in allowed:
                    failures.append(
                        Failure(
                            slice_id,
                            ACCEPTANCE_UNRELATED,
                            f"acceptance names {named}, which is neither {slice_id} nor one of its prerequisites",
                        )
                    )
    return failures


def _render(failures: list[Failure]) -> str:
    return "\n".join(str(f) for f in failures)


def test_current_plan_is_consistent() -> None:
    plan = parse_plan(PLAN_PATH.read_text(encoding="utf-8"))
    assert plan.rows, "no slice rows found under '## Build plan'"
    assert plan.placements, "no waves found under '### Order and lanes'"
    assert plan.deps_list, "no dependency list found under '### Order and lanes'"
    assert plan.gate, "no release gate slices found under '## Release gate for 0.31.0'"
    failures = check_plan(plan)
    assert not failures, "v2 plan is inconsistent:\n" + _render(failures)


# ---------------------------------------------------------------------------
# Negative tests on small synthetic documents.
# ---------------------------------------------------------------------------

BASE_ROWS = {
    "S1": ("none", "Cases pass."),
    "S2": ("S1", "Builds on S1."),
    "S3": ("S2. Verify: S1", "Needs S2 and S1."),
    "S4": ("S3, the connectivity PR, #9000", "Uses S3."),
    "S5": ("S3", "Gated move."),
}
BASE_WAVES = [
    "wave 0:  S1 (merged, #1)",
    "wave 1:  S2",
    "wave 2:  S3",
    "wave 3:  S4",
    "gated, outside the release:  S5 after S3, with a go-ahead",
]
BASE_LIST = [
    "S1: none",
    "S2: S1",
    "S3: S2 verify: S1",
    "S4: S3 #9000 connectivity-pr",
    "S5: S3",
]
BASE_GATE = [
    "- Hub: S1, S2 (the base), S3 and S4. S5 is not in the release gate.",
    "- External prerequisites: #9000 and `connectivity-pr`.",
]


def make_doc(
    rows: dict[str, tuple[str, str]] | None = None,
    waves: list[str] | None = None,
    dep_list: list[str] | None = None,
    gate: list[str] | None = None,
) -> str:
    rows = BASE_ROWS if rows is None else rows
    table = "\n".join(f"| {sid} | Slice {sid} | {deps} | {acc} | none |" for sid, (deps, acc) in rows.items())
    return "\n".join(
        [
            "## Build plan",
            "",
            "### Hub track",
            "",
            "| ID | Slice | Depends on | Acceptance | Parallel with |",
            "|---|---|---|---|---|",
            table,
            "",
            "### Order and lanes",
            "",
            "```text",
            *(BASE_WAVES if waves is None else waves),
            "```",
            "",
            "```text",
            *(BASE_LIST if dep_list is None else dep_list),
            "```",
            "",
            "## Release gate for 0.31.0",
            "",
            *(BASE_GATE if gate is None else gate),
            "",
            "## Risks",
        ]
    )


def failures_of(doc: str) -> list[Failure]:
    return check_plan(parse_plan(doc))


def has(failures: list[Failure], slice_id: str, kind: str, mention: str = "") -> bool:
    return any(f.slice == slice_id and f.kind == kind and mention in f.detail for f in failures)


def test_synthetic_baseline_is_consistent() -> None:
    assert failures_of(make_doc()) == []


def test_slice_in_waves_missing_from_tables() -> None:
    waves = [line.replace("wave 3:  S4", "wave 3:  S4  S9") for line in BASE_WAVES]
    assert has(failures_of(make_doc(waves=waves)), "S9", MISSING_FROM_TABLES)


def test_slice_twice_in_waves() -> None:
    waves = [line.replace("wave 1:  S2", "wave 1:  S2  S1") for line in BASE_WAVES]
    assert has(failures_of(make_doc(waves=waves)), "S1", DUPLICATE_IN_WAVES)


def test_slice_twice_in_tables() -> None:
    doc = make_doc().replace("| S2 | Slice S2 |", "| S2 | Slice S2 |", 1)
    doc = doc.replace(
        "|---|---|---|---|---|\n", "|---|---|---|---|---|\n| S2 | Again | S1 | Builds on S1. | none |\n", 1
    )
    assert has(failures_of(doc), "S2", DUPLICATE_IN_TABLES)


def test_edge_in_same_wave() -> None:
    waves = [
        line.replace("wave 0:  S1 (merged, #1)", "wave 0:  S1 (merged, #1)  S2").replace("wave 1:  S2", "wave 1:")
        for line in BASE_WAVES
    ]
    failures = failures_of(make_doc(waves=waves))
    assert has(failures, "S2", EDGE_NOT_EARLIER, "build edge to S1")


def test_verify_edge_in_later_wave() -> None:
    dep_list = [line.replace("S2: S1", "S2: S1 verify: S4") for line in BASE_LIST]
    rows = dict(BASE_ROWS)
    rows["S2"] = ("S1. Verify: S4", "Builds on S1.")
    failures = failures_of(make_doc(rows=rows, dep_list=dep_list))
    assert has(failures, "S2", EDGE_NOT_EARLIER, "verify edge to S4")


def test_table_cell_differs_from_dependency_list() -> None:
    dep_list = [line.replace("S3: S2 verify: S1", "S3: S2") for line in BASE_LIST]
    assert has(failures_of(make_doc(dep_list=dep_list)), "S3", CELL_MISMATCH)


def test_dependency_on_an_undefined_slice() -> None:
    rows = dict(BASE_ROWS)
    rows["S5"] = ("S99", "Gated move.")
    dep_list = [line.replace("S5: S3", "S5: S99") for line in BASE_LIST]
    failures = failures_of(make_doc(rows=rows, dep_list=dep_list))
    assert has(failures, "S5", UNRESOLVED_DEP, "S99")


def test_release_gate_slice_has_prerequisite_outside_gate() -> None:
    gate = [line.replace("S1, S2 (the base), S3", "S1, S3") for line in BASE_GATE]
    failures = failures_of(make_doc(gate=gate))
    assert has(failures, "S3", GATE_OUTSIDE, "S2")
    assert has(failures, "S4", GATE_OUTSIDE, "S2")


def test_indirect_prerequisite_outside_gate() -> None:
    gate = [line.replace("S1, S2 (the base), S3", "S2, S3") for line in BASE_GATE]
    assert has(failures_of(make_doc(gate=gate)), "S2", GATE_OUTSIDE, "S1")


def test_external_prerequisite_not_listed_by_the_gate() -> None:
    gate = [line.replace("#9000 and ", "") for line in BASE_GATE]
    assert has(failures_of(make_doc(gate=gate)), "S4", GATE_OUTSIDE, "#9000")


def test_acceptance_names_unrelated_slice() -> None:
    rows = dict(BASE_ROWS)
    rows["S2"] = ("S1", "Builds on S1 and S4.")
    assert has(failures_of(make_doc(rows=rows)), "S2", ACCEPTANCE_UNRELATED, "S4")


def test_acceptance_may_name_an_indirect_prerequisite() -> None:
    rows = dict(BASE_ROWS)
    rows["S4"] = ("S3, the connectivity PR, #9000", "Uses S3 and S1.")
    assert failures_of(make_doc(rows=rows)) == []


def test_gated_slice_with_prerequisite_outside_the_waves() -> None:
    rows = dict(BASE_ROWS)
    rows["S5"] = ("S3, S6", "Gated move.")
    rows["S6"] = ("S3", "After phase 3.")
    waves = [*BASE_WAVES[:4], "after phase 3:  S6", BASE_WAVES[4]]
    dep_list = [line.replace("S5: S3", "S5: S3 S6") for line in BASE_LIST] + ["S6: S3"]
    failures = failures_of(make_doc(rows=rows, waves=waves, dep_list=dep_list))
    assert has(failures, "S5", GATED_PREREQ_OUTSIDE_WAVES, "S6")


def test_gated_slice_listed_in_the_release_gate() -> None:
    gate = [line.replace("S3 and S4. S5 is not in the release gate.", "S3, S4 and S5.") for line in BASE_GATE]
    assert gate != BASE_GATE
    assert has(failures_of(make_doc(gate=gate)), "S5", GATED_IN_GATE)


def test_release_gate_slice_depends_on_gated_slice() -> None:
    rows = dict(BASE_ROWS)
    rows["S4"] = ("S3, S5, the connectivity PR, #9000", "Uses S3.")
    dep_list = [line.replace("S4: S3 ", "S4: S3 S5 ") for line in BASE_LIST]
    failures = failures_of(make_doc(rows=rows, dep_list=dep_list))
    assert has(failures, "S4", GATE_DEPENDS_ON_GATED, "S5")


def test_ranges_expand_in_every_position() -> None:
    assert slice_ids("S23 to S26") == ["S23", "S24", "S25", "S26"]
    assert slice_ids("S12a..S12c and S1") == ["S12a", "S12b", "S12c", "S1"]
    problems: list[Failure] = []
    assert slice_ids("S1 to S2b", problems) == ["S1", "S2b"]
    assert problems and problems[0].kind == PARSE
