import json
from pathlib import Path

from brigade.config import Config, write_config, WORKSPACE_DIRNAME
from brigade.install import install_selection
from brigade.selection import Selection
from brigade import doctor as doctor_mod
from brigade import status as status_mod


def test_config_schema_has_no_model_or_key_fields(tmp_target: Path):
    """Brigade is model-neutral: config carries no model/provider/api-key concept."""
    write_config(
        tmp_target,
        Config(version=1, selection=Selection(depth="repo", harnesses=["claude"], owner="claude")),
    )
    payload = json.loads((tmp_target / WORKSPACE_DIRNAME / "config.json").read_text())
    assert set(payload) == {"version", "depth", "harnesses", "owner", "includes", "work"}
    assert payload["work"] == {"mode": "strict"}
    for forbidden in ("model", "provider", "api_key", "apiKey", "token"):
        assert forbidden not in payload


def test_generic_install_needs_no_harness(tmp_target: Path, capsys):
    """A harness-less (generic) install must pass doctor and status end to end."""
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=[], owner="this-repo", includes=[]),
    )
    assert doctor_mod.run(target=tmp_target, harness="generic") == 0
    assert status_mod.run(target=tmp_target) == 0
    out = capsys.readouterr().out
    assert "[fail]" not in out


def test_core_modules_hold_no_provider_sdk_or_key_handling():
    """The core must not import a provider SDK or read a provider API key env var."""
    src = Path(__file__).resolve().parents[1] / "src" / "brigade"
    forbidden_imports = ("import anthropic", "import openai", "from anthropic", "from openai")
    forbidden_env = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    for py in src.rglob("*.py"):
        text = py.read_text()
        for needle in forbidden_imports + forbidden_env:
            assert needle not in text, f"{py.name} references {needle!r}"
