from pathlib import Path

from brigade.center_cmd.dashboard.views import outcome_rank


def test_fetch_uses_outcome_rank_json(monkeypatch, tmp_path):
    expected = {"ranking": []}
    calls: list[tuple[Path, list[str]]] = []

    def fake_run_json(target: Path, args: list[str]) -> dict:
        calls.append((target, args))
        return expected

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)

    assert outcome_rank.fetch(tmp_path) == expected
    assert calls == [(tmp_path, ["outcome", "rank"])]


def test_render_displays_ranking_rows_with_cohort_and_recency_columns():
    payload = {
        "ranking": [
            {
                "artifact_id": "example-skill-one",
                "score": 0.4567,
                "helped": 12,
                "hurt": 3,
                "capability_score": 0.6789,
                "capability_helped": 9,
                "capability_hurt": 2,
                "stale_records": 1,
                "legacy_records": 4,
            },
            {
                "artifact_id": "example-skill-two",
                "score": 0.1,
                "helped": 2,
                "hurt": 5,
                "capability_score": 0.25,
                "capability_helped": 1,
                "capability_hurt": 3,
                "stale_records": 0,
                "legacy_records": 0,
            },
        ],
        "target": "/fake/worktree",
    }

    fragment = outcome_rank.render(payload, "unused")

    for header in (
        "Artifact",
        "Score",
        "Helped",
        "Hurt",
        "Capability score",
        "Capability helped",
        "Capability hurt",
        "Stale records",
        "Legacy records",
    ):
        assert header in fragment
    assert "example-skill-one" in fragment
    assert "example-skill-two" in fragment
    for value in ("0.457", "12", "3", "0.679", "9", "2", "1", "4"):
        assert f">{value}<" in fragment
    assert "0.100" in fragment
    assert "0.250" in fragment
    # Row order follows the payload ranking order.
    assert fragment.index("example-skill-one") < fragment.index("example-skill-two")


def test_docstring_states_operator_question():
    assert outcome_rank.__doc__ is not None
    assert "helping" in outcome_rank.__doc__
    assert "hurting" in outcome_rank.__doc__


def test_summary_strip_renders_before_table_from_ranking_data():
    payload = {
        "ranking": [
            {"artifact_id": "example-skill-one", "helped": 12, "hurt": 3},
            {"artifact_id": "example-skill-two", "helped": 2, "hurt": 5},
        ]
    }

    fragment = outcome_rank.render(payload, "unused")

    assert "biggest net help" in fragment
    assert "biggest net drag" in fragment
    assert "example-skill-one" in fragment.split("<table")[0]
    assert "example-skill-two" in fragment.split("<table")[0]
    assert fragment.index("biggest net help") < fragment.index("<table")
    assert "<style" not in fragment


def test_render_escapes_untrusted_values():
    payload = {
        "ranking": [
            {
                "artifact_id": '<img src=x onerror="alert(1)">',
                "score": 0.5,
                "helped": 1,
                "hurt": 0,
            }
        ]
    }

    fragment = outcome_rank.render(payload, "unused")

    assert "<img" not in fragment
    assert "&lt;img" in fragment
    assert "&quot;alert(1)&quot;" in fragment


def test_render_missing_entry_values_degrade_to_dash():
    fragment = outcome_rank.render({"ranking": [{"artifact_id": "sparse-skill"}]}, "unused")

    assert "sparse-skill" in fragment
    assert ">-<" in fragment


def test_render_error_payload_renders_error_panel_only():
    fragment = outcome_rank.render({"error": "boom"}, "unused")

    assert "boom" in fragment
    assert 'class="error"' in fragment
    assert "<table" not in fragment


def test_render_empty_payloads_render_not_being_fed_panel():
    for payload in ({}, {"ranking": []}, {"ranking": None}, {"ranking": "not-a-list"}):
        fragment = outcome_rank.render(payload, "unused")
        assert "ranking is empty" in fragment
        assert "outcome loop is not being fed" in fragment
        assert "<table" not in fragment
        assert "biggest net help" not in fragment
