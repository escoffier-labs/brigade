from brigade.research import types as t


def test_finding_defaults_and_trust():
    f = t.Finding(source="/notes/a.md", title="A", summary="s", evidence="e", trust="local")
    assert f.trust == "local"
    assert f.as_dict()["source"] == "/notes/a.md"


def test_caps_from_overrides():
    caps = t.Caps.build(max_rounds=3)
    assert caps.max_rounds == 3
    assert caps.max_time > 0  # default retained
    assert caps.max_urls_per_round >= 1
