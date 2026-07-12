"""Property tests for the router: generate random valid catalogs and states,
then assert the invariants a composed route must always satisfy.

No hypothesis dependency (brigade ships zero runtime deps and keeps the dev set
small): a seeded stdlib generator draws CASES random scenarios from a fixed seed,
so a failure is reproducible from the printed seed and case index. Hand-written
cases live in test_router.py; this file is the fuzzer that catches the class of
bug two human reviewers found by reading.
"""

from __future__ import annotations

import random

from brigade import router

# Fixed seed: same run every time, so a failure reproduces. Bump to widen search.
SEED = 20260712
CASES = 2000
MAX_STAGES = 8
MAX_ARTIFACTS = 6
MAX_SIGNALS = 6


def _random_catalog(rng: random.Random) -> tuple[dict, list[str], list[str]]:
    """A random but well-formed catalog: stages with inputs drawn from a shared
    artifact pool, outputs, signal subscriptions, and occasional locks. Returns
    (catalog, all_signal_names, all_artifact_names)."""
    n = rng.randint(0, MAX_STAGES)
    artifacts = [f"art{i}" for i in range(rng.randint(1, MAX_ARTIFACTS))]
    signals = [f"sig{i}" for i in range(rng.randint(1, MAX_SIGNALS))] + list(router.PATHS)
    stages: dict = {}
    for i in range(n):
        name = f"stage{i}"
        required = rng.sample(artifacts, k=rng.randint(0, min(2, len(artifacts))))
        optional = rng.sample(artifacts, k=rng.randint(0, min(2, len(artifacts))))
        output = rng.sample(artifacts, k=rng.randint(0, min(2, len(artifacts))))
        subscribes = rng.sample(signals, k=rng.randint(1, min(3, len(signals))))
        stage = {
            "routes": rng.sample(list(router.PATHS), k=rng.randint(1, len(router.PATHS))),
            "data": {"input": {"required": required, "optional": optional}, "output": output},
            "signals": {"subscribes": subscribes, "publishes": []},
        }
        if rng.random() < 0.25:
            stage["lock"] = [{"while": rng.choice(signals), "until": rng.choice(signals)}]
        stages[name] = stage
    return {"stages": stages}, signals, artifacts


def _acyclic(catalog: dict, names: set[str]) -> bool:
    """True when the input/output graph over the given stages has no cycle."""
    producers: dict[str, set[str]] = {}
    for name in names:
        for art in catalog["stages"][name]["data"]["output"]:
            producers.setdefault(art, set()).add(name)
    color: dict[str, int] = {}

    def visit(node: str) -> bool:
        color[node] = 1
        stage = catalog["stages"][node]
        deps = stage["data"]["input"]["required"] + stage["data"]["input"]["optional"]
        for art in deps:
            for pred in producers.get(art, set()):
                if pred == node:
                    continue
                if color.get(pred) == 1:
                    return False
                if color.get(pred, 0) == 0 and not visit(pred):
                    return False
        color[node] = 2
        return True

    return all(color.get(n, 0) != 0 or visit(n) for n in names)


def test_router_invariants_over_random_catalogs() -> None:
    rng = random.Random(SEED)
    for case in range(CASES):
        catalog, signals, artifacts = _random_catalog(rng)
        live = rng.sample(signals, k=rng.randint(0, len(signals)))
        available = rng.sample(artifacts, k=rng.randint(0, len(artifacts)))
        already = (
            rng.sample(list(catalog["stages"]), k=rng.randint(0, len(catalog["stages"]))) if catalog["stages"] else []
        )
        ctx = f"seed={SEED} case={case}"

        # A dependency cycle among runnable stages must raise, not return a lie.
        stages = catalog["stages"]
        try:
            result = router.compute_route(catalog, live, available=available, already_run=already)
        except ValueError as exc:
            assert "cycle" in str(exc), f"{ctx}: unexpected ValueError: {exc}"
            continue

        route = result["route"]
        held = result["held"]
        waves = result["waves"]
        route_set = set(route)

        # 1. No stage is both routed and held.
        assert route_set.isdisjoint(held), f"{ctx}: stage both routed and held"

        # 2. already_run stages never reappear.
        assert route_set.isdisjoint(already), f"{ctx}: already-run stage re-triggered"
        assert set(held).isdisjoint(already), f"{ctx}: already-run stage held"

        # 3. Every routed stage is on the live path (or no path is live).
        live_paths = {p for p in router.PATHS if p in live}
        if live_paths:
            for name in route:
                assert set(stages[name]["routes"]) & live_paths, f"{ctx}: {name} off live path but routed"

        # 4. Every routed stage triggered on a live signal it subscribes to.
        for name in route:
            subs = stages[name]["signals"]["subscribes"]
            assert any(router._matches(s, set(live)) for s in subs), f"{ctx}: {name} routed with no live trigger"

        # 5. Every routed stage's required inputs are satisfiable: available, or
        #    produced by another routed stage (never by itself).
        produced_by_route = set()
        for name in route:
            produced_by_route |= set(stages[name]["data"]["output"])
        for name in route:
            for art in stages[name]["data"]["input"]["required"]:
                others = {p for p in route if p != name and art in stages[p]["data"]["output"]}
                assert art in available or others, f"{ctx}: {name} required input {art} unsatisfiable"

        # 6. No active lock survives into the route.
        for name in route:
            assert not router._active_locks(stages[name], set(live)), f"{ctx}: {name} routed with an active lock"

        # 7. Held stages have a genuinely active lock and report its until signal.
        for name, untils in held.items():
            active = router._active_locks(stages[name], set(live))
            assert active, f"{ctx}: {name} held with no active lock"
            assert set(untils) == {lk["until"] for lk in active}, f"{ctx}: {name} held-until mismatch"

        # 8. waves flatten to route in order, and each wave is a valid topo level:
        #    an in-route producer sits in a STRICTLY earlier wave than its
        #    consumer. Strict, not <=, so a cyclic pair forced into one wave
        #    (equal position) is a failure rather than a false pass.
        assert [n for wave in waves for n in wave] == route, f"{ctx}: waves do not flatten to route"
        position = {name: i for i, wave in enumerate(waves) for name in wave}
        for name in route:
            deps = stages[name]["data"]["input"]["required"] + stages[name]["data"]["input"]["optional"]
            for art in deps:
                for producer in route:
                    if producer != name and art in stages[producer]["data"]["output"]:
                        assert position[producer] < position[name], (
                            f"{ctx}: {name} not strictly after producer {producer}"
                        )

        # 9. size label agrees with the route length bucket.
        assert result["size"] == router.size_label(len(route)), f"{ctx}: size label mismatch"


def test_generator_produces_both_cyclic_and_acyclic_catalogs() -> None:
    # Guard the fuzzer itself: if the generator only ever made acyclic graphs,
    # the cycle-raising branch would never be exercised and invariant 8 would be
    # vacuous. Confirm both shapes appear in the search space.
    rng = random.Random(SEED)
    saw_cycle = saw_acyclic = False
    for _ in range(CASES):
        catalog, _, _ = _random_catalog(rng)
        names = set(catalog["stages"])
        if not names:
            continue
        if _acyclic(catalog, names):
            saw_acyclic = True
        else:
            saw_cycle = True
        if saw_cycle and saw_acyclic:
            break
    assert saw_acyclic, "generator never produced an acyclic catalog"
    assert saw_cycle, "generator never produced a cyclic catalog"
