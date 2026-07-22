"""Router engine, catalog coherence, and signal derivation tests."""

from __future__ import annotations

import json
import re

from brigade import route_catalog, router
from brigade.route_catalog import DEFAULT_CATALOG, derive_signals, route_brief, uncovered_stages


def _catalog(stages: dict) -> dict:
    return {"stages": stages}


def _stage(
    subscribes,
    *,
    routes=("code",),
    required=(),
    optional=(),
    output=(),
    lock=None,
):
    stage = {
        "routes": list(routes),
        "data": {
            "input": {"required": list(required), "optional": list(optional)},
            "output": list(output),
        },
        "signals": {"subscribes": list(subscribes), "publishes": []},
    }
    if lock:
        stage["lock"] = lock
    return stage


# --- engine ---


def test_trigger_by_subscription() -> None:
    catalog = _catalog(
        {
            "a": _stage(["code"], required=["task"], output=["diff"]),
            "b": _stage(["never-fires"], required=["task"]),
        }
    )
    result = router.compute_route(catalog, ["code"], available=["task"])
    assert result["route"] == ["a"]
    assert result["triggered_by"] == {"a": "code"}
    assert "b" not in result["route"]


def test_family_prefix_matching() -> None:
    catalog = _catalog({"fixer": _stage(["findings"], required=["task"])})
    result = router.compute_route(catalog, ["code", "findings:security"], available=["task"])
    assert result["route"] == ["fixer"]
    assert result["triggered_by"]["fixer"] == "findings"


def test_off_path_stage_dropped() -> None:
    catalog = _catalog(
        {
            "code-only": _stage(["go"], routes=["code"], required=["task"]),
            "docs-only": _stage(["go"], routes=["docs"], required=["task"]),
        }
    )
    result = router.compute_route(catalog, ["docs", "go"], available=["task"])
    assert result["route"] == ["docs-only"]
    assert result["dropped"] == {"code-only": "off-path"}


def test_unsatisfiable_required_input_drops_stage() -> None:
    catalog = _catalog({"review": _stage(["code"], required=["diff"])})
    result = router.compute_route(catalog, ["code"], available=["task"])
    assert result["route"] == []
    assert result["dropped"] == {"review": "unsatisfiable-input"}


def test_in_route_producer_satisfies_required_input() -> None:
    catalog = _catalog(
        {
            "implement": _stage(["code"], required=["task"], output=["diff"]),
            "review": _stage(["code"], required=["diff"]),
        }
    )
    result = router.compute_route(catalog, ["code"], available=["task"])
    assert result["route"] == ["implement", "review"]
    assert result["waves"] == [["implement"], ["review"]]


def test_optional_input_orders_but_never_drops() -> None:
    catalog = _catalog(
        {
            "plan": _stage(["big"], required=["task"], output=["plan"]),
            "implement": _stage(["code"], required=["task"], optional=["plan"], output=["diff"]),
        }
    )
    with_plan = router.compute_route(catalog, ["code", "big"], available=["task"])
    assert with_plan["waves"] == [["plan"], ["implement"]]
    without_plan = router.compute_route(catalog, ["code"], available=["task"])
    assert without_plan["route"] == ["implement"]


def test_parallel_wave_shares_no_edge() -> None:
    catalog = _catalog(
        {
            "implement": _stage(["code"], required=["task"], output=["diff"]),
            "review-a": _stage(["code"], required=["diff"]),
            "review-b": _stage(["code"], required=["diff"]),
        }
    )
    result = router.compute_route(catalog, ["code"], available=["task"])
    assert result["waves"] == [["implement"], ["review-a", "review-b"]]


def test_active_lock_holds_stage_and_redrops_consumers() -> None:
    catalog = _catalog(
        {
            "execute": _stage(
                ["system"],
                routes=["system"],
                required=["task"],
                output=["state"],
                lock=[{"while": "destructive-op", "until": "destructive-approved"}],
            ),
            "verify": _stage(["system"], routes=["system"], required=["state"]),
        }
    )
    held = router.compute_route(catalog, ["system", "destructive-op"], available=["task"])
    assert held["route"] == []
    assert held["held"] == {"execute": ["destructive-approved"]}
    assert held["dropped"] == {"verify": "unsatisfiable-input"}

    released = router.compute_route(catalog, ["system", "destructive-op", "destructive-approved"], available=["task"])
    assert released["route"] == ["execute", "verify"]
    assert released["held"] == {}


def test_inactive_lock_is_noop() -> None:
    catalog = _catalog(
        {
            "execute": _stage(
                ["system"],
                routes=["system"],
                required=["task"],
                lock=[{"while": "destructive-op", "until": "destructive-approved"}],
            )
        }
    )
    result = router.compute_route(catalog, ["system"], available=["task"])
    assert result["route"] == ["execute"]


def test_already_run_stage_never_retriggers() -> None:
    catalog = _catalog({"a": _stage(["code"], required=["task"])})
    result = router.compute_route(catalog, ["code"], available=["task"], already_run=["a"])
    assert result["route"] == []


def test_stage_never_satisfies_its_own_required_input() -> None:
    catalog = _catalog({"loner": _stage(["code"], required=["thing"], output=["thing"])})
    result = router.compute_route(catalog, ["code"], available=["task"])
    assert result["route"] == []
    assert result["dropped"] == {"loner": "unsatisfiable-input"}


def test_dependency_cycle_raises() -> None:
    catalog = _catalog(
        {
            "a": _stage(["code"], required=["b-out"], output=["a-out"]),
            "b": _stage(["code"], required=["a-out"], output=["b-out"]),
        }
    )
    try:
        router.compute_route(catalog, ["code"], available=["task"])
    except ValueError as exc:
        assert "cycle" in str(exc)
    else:
        raise AssertionError("expected ValueError on catalog cycle")


def test_stage_dependencies_edges() -> None:
    catalog = _catalog(
        {
            "plan": _stage(["big"], output=["plan"]),
            "implement": _stage(["code"], required=["plan"], output=["diff"]),
            "review": _stage(["code"], required=["diff"], output=["findings"]),
        }
    )
    deps = router.stage_dependencies(catalog, ["plan", "implement", "review"])
    assert deps == {"plan": set(), "implement": {"plan"}, "review": {"implement"}}


def test_size_labels() -> None:
    assert router.size_label(0) == "empty"
    assert router.size_label(1) == "XS"
    assert router.size_label(3) == "S"
    assert router.size_label(6) == "M"
    assert router.size_label(10) == "L"
    assert router.size_label(15) == "XL"
    assert router.size_label(16) == "XXL"


# --- default catalog coherence (invariants, alp-river check_catalog style) ---


def _all_stages() -> dict:
    return DEFAULT_CATALOG["stages"]


def test_catalog_every_stage_declares_known_routes() -> None:
    for name, stage in _all_stages().items():
        assert stage["routes"], f"{name} declares no routes"
        unknown = set(stage["routes"]) - set(router.PATHS)
        assert not unknown, f"{name} routes unknown paths: {unknown}"


def test_catalog_every_required_input_has_a_producer_or_seed() -> None:
    produced = {"task"}
    for stage in _all_stages().values():
        produced.update(stage["data"]["output"])
    for name, stage in _all_stages().items():
        for art in stage["data"]["input"]["required"]:
            assert art in produced, f"{name} requires {art!r} which nothing produces"


def test_catalog_every_lock_signal_is_derivable_or_approval() -> None:
    derivable = {signal for signal, _ in route_catalog._SIGNAL_PATTERNS}
    derivable.update(router.PATHS)
    derivable.add("needs-tests")
    granted = set(route_catalog.APPROVAL_SIGNALS)
    for name, stage in _all_stages().items():
        for lock in stage.get("lock", []):
            assert lock["while"] in derivable, f"{name} lock while={lock['while']!r} underivable"
            assert lock["until"] in granted, f"{name} lock until={lock['until']!r} not an approval"


def test_catalog_every_subscription_is_derivable_or_published() -> None:
    known = {signal for signal, _ in route_catalog._SIGNAL_PATTERNS}
    known.update(router.PATHS)
    known.add("needs-tests")
    for stage in _all_stages().values():
        for published in stage["signals"]["publishes"]:
            known.add(published)
    for name, stage in _all_stages().items():
        for sub in stage["signals"]["subscribes"]:
            base = sub.split(":", 1)[0]
            assert sub in known or base in known, f"{name} subscribes to unknown signal {sub!r}"


def test_catalog_signal_patterns_compile() -> None:
    for _, pattern in route_catalog._SIGNAL_PATTERNS:
        re.compile(pattern)


# --- signal derivation ---


def test_derive_plain_code_task() -> None:
    assert derive_signals("rename the config loader helper") == ["code"]


def test_derive_bug_pulls_tests() -> None:
    signals = derive_signals("fix the crash when saving an empty file")
    assert signals[0] == "code"
    assert "bug" in signals
    assert "needs-tests" in signals


def test_derive_auth_surface() -> None:
    assert "auth-surface" in derive_signals("add rate limiting to the login endpoint")


def test_derive_author_is_not_auth() -> None:
    assert "auth-surface" not in derive_signals("add an authors section generator")


def test_derive_docs_task() -> None:
    assert derive_signals("fix typo in README") == ["docs"]
    assert derive_signals("update the changelog") == ["docs"]


def test_derive_docs_template_wins() -> None:
    assert derive_signals("update the guide", template="docs") == ["docs"]


def test_derive_system_task() -> None:
    assert derive_signals("add a crontab entry for the backup script")[0] == "system"


def test_derive_significant_build_pulls_plan_and_tests() -> None:
    signals = derive_signals("implement a new export module end to end")
    assert "significant-build" in signals
    assert "needs-tests" in signals


def test_derive_template_pulls_tests() -> None:
    signals = derive_signals("add pagination to the list endpoint", template="vertical-slice")
    assert "needs-tests" in signals


def test_derive_changed_paths_add_surfaces() -> None:
    signals = derive_signals(
        "polish the settings screen", changed_paths=["web/src/Settings.tsx", "db/migrations/012.sql"]
    )
    assert "ui-touched" in signals
    assert "migration" in signals


def test_derive_is_deterministic() -> None:
    task = "fix the login form crash on empty password"
    assert derive_signals(task) == derive_signals(task)


# --- route brief ---


def test_route_brief_composes_expected_stages() -> None:
    brief = route_brief("add rate limiting to the login endpoint and implement tests")
    assert "implement" in brief.route
    assert "correctness-review" in brief.route
    assert "security-review" in brief.route
    assert "verify" in brief.route
    assert brief.attached
    assert route_catalog.ROUTE_HEADING in brief.text


def test_route_brief_ship_is_held_without_approval() -> None:
    brief = route_brief("implement the export module and open a PR")
    assert "ship" in brief.held
    assert brief.held["ship"] == ["ship-approved"]
    assert "HELD: ship" in brief.text


def test_route_brief_ship_released_by_approval() -> None:
    brief = route_brief("implement the export module and open a PR", approvals=["ship-approved"])
    assert "ship" in brief.route
    assert brief.held == {}


def test_route_brief_reviews_run_in_one_wave() -> None:
    brief = route_brief("implement rate limiting for the login endpoint")
    wave_of = {name: i for i, wave in enumerate(brief.waves) for name in wave}
    assert wave_of["correctness-review"] == wave_of["security-review"]
    assert wave_of["implement"] < wave_of["correctness-review"]


def test_route_brief_payload_shape() -> None:
    payload = route_brief("fix typo in README").payload()
    assert payload["attached"] is True
    assert payload["signals"] == ["docs"]
    assert isinstance(payload["route"], list)
    assert isinstance(payload["waves"], list)


def test_uncovered_stages_reports_missing() -> None:
    class FakeAssignment:
        def __init__(self, covers):
            self.covers = covers

    brief = route_brief("rename the config loader helper")
    assert brief.route == ("implement", "correctness-review", "verify")
    missing = uncovered_stages(brief, [FakeAssignment(("implement", "verify"))])
    assert missing == ["correctness-review"]
    assert uncovered_stages(brief, [FakeAssignment(("implement", "correctness-review", "verify"))]) == []


def test_derive_auth_surface_pulls_tests() -> None:
    signals = derive_signals("add rate limiting to the login endpoint")
    assert "auth-surface" in signals
    assert "needs-tests" in signals


def test_route_cli_text_and_json(capsys) -> None:
    from brigade.cli import main

    assert main(["route", "add rate limiting to the login endpoint"]) == 0
    out = capsys.readouterr().out
    assert "signals: code, auth-surface, needs-tests" in out
    assert "security-review" in out

    assert main(["route", "implement the export module and open a PR", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["held"] == {"ship": ["ship-approved"]}

    assert main(["route", "implement the export module and open a PR", "--approve-ship", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["held"] == {}
    assert "ship" in payload["route"]


def test_derive_docs_task_keeps_ship_hold() -> None:
    signals = derive_signals("fix typo in README and open a PR")
    assert signals[0] == "docs"
    assert "ship-requested" in signals
    brief = route_brief("fix typo in README and open a PR")
    assert brief.held == {"ship": ["ship-approved"]}


def test_derive_changed_path_segments_not_substrings() -> None:
    signals = derive_signals("tidy helpers", changed_paths=["src/author.py", "docs/tokenizer.md"])
    assert "auth-surface" not in signals
    signals = derive_signals("tidy helpers", changed_paths=["src/auth/session.py"])
    assert "auth-surface" in signals


def test_derive_repo_context_vetoes_system_path() -> None:
    assert derive_signals("fix the bug in the nginx config template in the repo")[0] == "code"
    assert derive_signals("point nginx reverse proxy at the new port")[0] == "system"


def test_route_brief_payload_records_approvals() -> None:
    granted = route_brief("implement the export module and open a PR", approvals=["ship-approved"])
    assert granted.payload()["approvals"] == ["ship-approved"]
    plain = route_brief("implement the export module and open a PR")
    assert plain.payload()["approvals"] == []


# --- docs-misroute fix: conventional-commit code prefix vetoes docs path ---


def test_conventional_fix_prefix_routes_code_despite_docs_word() -> None:
    signals = derive_signals("fix(install): stop bootstrap docs referencing workspace-only files")
    assert signals[0] == "code"


def test_conventional_feat_prefix_routes_code_despite_changelog() -> None:
    signals = derive_signals("feat(skills): ship skill.json and CHANGELOG.md with bundled templates")
    assert signals[0] == "code"


def test_prose_fix_typo_still_routes_docs() -> None:
    assert derive_signals("fix typo in README")[0] == "docs"


def test_docs_scope_prefix_stays_docs() -> None:
    assert derive_signals("fix(docs): repair a broken changelog link")[0] == "docs"
    assert derive_signals("docs: refresh the roadmap")[0] == "docs"


def test_bare_conventional_prefix_routes_code() -> None:
    assert derive_signals("perf: speed up the changelog generator")[0] == "code"


# --- unknown covers (hallucinated coverage) ---


def test_unknown_covers_flags_bogus_stage_names() -> None:
    class FakeAssignment:
        def __init__(self, covers):
            self.covers = covers

    brief = route_brief("rename the config loader helper")  # route: implement, correctness-review, verify
    from brigade.route_catalog import unknown_covers

    assert unknown_covers(brief, [FakeAssignment(("implement", "typo-review"))]) == ["typo-review"]
    assert unknown_covers(brief, [FakeAssignment(("implement", "verify"))]) == []
    # dedup + order preserved across assignments
    result = unknown_covers(
        brief, [FakeAssignment(("ghost-a", "ghost-b")), FakeAssignment(("ghost-a", "correctness-review"))]
    )
    assert result == ["ghost-a", "ghost-b"]


# --- route-signal overrides + triggered_by telemetry ---


def test_override_force_add_pulls_dependents() -> None:
    # +auth-surface on a plain code task pulls needs-tests and security-review.
    brief = route_brief("clean up the config loader", overrides=["+auth-surface"])
    assert "auth-surface" in brief.signals
    assert "needs-tests" in brief.signals
    assert "security-review" in brief.route
    assert brief.overrides == ("+auth-surface",)


def test_override_suppress_with_tilde_and_dash() -> None:
    for token in ("~ship-requested", "-ship-requested"):
        brief = route_brief("implement export and open a PR", overrides=[token])
        assert "ship-requested" not in brief.signals
        assert "ship" not in brief.held
        assert "ship" not in brief.route


def test_override_bare_token_is_add() -> None:
    brief = route_brief("rename a helper", overrides=["perf-surface"])
    assert "perf-surface" in brief.signals
    assert "perf-review" in brief.route


def test_payload_carries_triggered_by_and_overrides() -> None:
    payload = route_brief("add rate limiting to the login endpoint", overrides=["+perf-surface"]).payload()
    assert payload["triggered_by"]["security-review"] == "auth-surface"
    assert payload["overrides"] == ["+perf-surface"]
    assert "triggered_by" in payload


def test_override_rejects_path_signal_suppression() -> None:
    # Found by dogfooding latent-premises on the router diff: ~code stripped the
    # path and collapsed the route. The path is derive-time, not overridable.
    import pytest

    for token in ("~code", "-docs", "+system"):
        with pytest.raises(ValueError, match="path signal"):
            route_brief("fix the crash when saving", overrides=[token])


def test_validate_overrides_allows_non_path_signals() -> None:
    from brigade.route_catalog import validate_overrides

    validate_overrides(["+auth-surface", "~ship-requested", "perf-surface"])  # no raise


# --- calibration pass (2026-07-12): real misroutes found by routing task corpus ---


def test_calibration_docs_rewrite_not_code() -> None:
    # "rewrite the QUICKSTART" routed code because "rewrite" fired significant-build
    # and QUICKSTART was not a docs hint. A docs hint now beats a bare rewrite.
    assert derive_signals("rewrite the QUICKSTART to lead with the operator flow")[0] == "docs"
    assert derive_signals("update the contributing guide")[0] == "docs"


def test_calibration_significant_build_alone_does_not_force_code_over_docs() -> None:
    # significant-build is a depth signal, not a "this is code" surface.
    assert derive_signals("redesign the tutorial")[0] == "docs"
    # but a concrete code surface still wins over a docs word:
    assert derive_signals("rewrite the auth module described in the README")[0] == "code"
    # and a plain rewrite with no docs hint stays code:
    assert derive_signals("rewrite the export module end to end")[0] == "code"


def test_calibration_ui_surface_covers_common_words() -> None:
    for task in (
        "polish the run-status panel in the web dashboard",
        "fix the sidebar widget alignment",
        "add a tooltip to the settings view",
    ):
        assert "ui-touched" in derive_signals(task), task


def test_calibration_migration_pulls_tests() -> None:
    signals = derive_signals("add a nullable owner_id column to the receipts table and backfill it")
    assert "migration" in signals
    assert "needs-tests" in signals
