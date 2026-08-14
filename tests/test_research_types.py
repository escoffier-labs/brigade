from brigade.research import types as t


def test_finding_defaults_and_trust():
    f = t.Finding(
        source_ids=("/notes/a.md",),
        title="A",
        summary="s",
        evidence="e",
        trust="local",
        extraction_lane="luna",
        extracted_at="2026-08-13T12:00:00+00:00",
    )
    assert f.trust == "local"
    assert f.source_ids == ("/notes/a.md",)
    assert f.extraction_lane == "luna"
    assert f.extracted_at == "2026-08-13T12:00:00+00:00"
    assert f.parent_source_ids == ()
    assert f.source == "/notes/a.md"


def test_caps_from_overrides():
    caps = t.Caps.build(max_rounds=3)
    assert caps.max_rounds == 3
    assert caps.max_time > 0  # default retained
    assert caps.max_urls_per_round >= 1
