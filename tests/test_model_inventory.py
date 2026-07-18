from brigade import agents, model_inventory


def _cursor_listing(*ids: str) -> str:
    lines = ["Available models", ""]
    lines.extend(f"{model_id} - Label for {model_id}" for model_id in ids)
    return "\n".join(lines) + "\n"


def _grok_listing(*ids: str) -> str:
    lines = ["You are logged in with grok.com.", "", "Available models:"]
    lines.extend(f"  * {model_id} (default)" for model_id in ids)
    return "\n".join(lines) + "\n"


def _ollama_listing(*ids: str) -> str:
    lines = ["NAME                       ID              SIZE      MODIFIED"]
    lines.extend(f"{model_id}  abcdef123456  2.0 GB  2 days ago" for model_id in ids)
    return "\n".join(lines) + "\n"


def test_cursor_inventory_exact_match(monkeypatch):
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, _cursor_listing("composer-2.5"), ""),
    )

    result = model_inventory.ModelInventoryInspector().inspect("cursor", "composer-2.5")

    assert result is not None
    assert result.state == "exact"
    assert result.matches == ("composer-2.5",)


def test_cursor_inventory_narrow_fuzzy_match(monkeypatch):
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(
            0,
            _cursor_listing("cursor-grok-4.5-low", "cursor-grok-4.5-high", "cursor-grok-4.6-high"),
            "",
        ),
    )

    result = model_inventory.ModelInventoryInspector().inspect("cursor", "grok-4.5-xhigh")

    assert result is not None
    assert result.state == "fuzzy-resolved"
    assert result.matches == ("cursor-grok-4.5-high", "cursor-grok-4.5-low")


def test_cursor_inventory_different_version_is_missing(monkeypatch):
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, _cursor_listing("cursor-grok-4.6-high"), ""),
    )

    result = model_inventory.ModelInventoryInspector().inspect("cursor", "grok-4.5-xhigh")

    assert result is not None
    assert result.state == "missing"
    assert result.matches == ()


def test_cursor_inventory_requires_versioned_family_for_fuzzy_match(monkeypatch):
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, _cursor_listing("cursor-auto-low"), ""),
    )

    result = model_inventory.ModelInventoryInspector().inspect("cursor", "auto-high")

    assert result is not None
    assert result.state == "missing"
    assert result.matches == ()


def test_cursor_inventory_command_failure_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(1, "", "not logged in"),
    )

    result = model_inventory.ModelInventoryInspector().inspect("cursor", "composer-2.5")

    assert result is not None
    assert result.state == "unavailable"
    assert "not logged in" in result.detail


def test_cursor_inventory_unrecognized_output_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, "model output changed\n", ""),
    )

    result = model_inventory.ModelInventoryInspector().inspect("cursor", "composer-2.5")

    assert result is not None
    assert result.state == "unavailable"
    assert "unrecognized inventory shape" in result.detail


def test_cursor_inventory_error_shaped_success_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, "Warning - authentication required\n", ""),
    )

    result = model_inventory.ModelInventoryInspector().inspect("cursor", "composer-2.5")

    assert result is not None
    assert result.state == "unavailable"


def test_cursor_inventory_accepts_parameterized_exact_model(monkeypatch):
    base_model = "claude-opus-4-8-thinking-high"
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, _cursor_listing(base_model), ""),
    )

    result = model_inventory.ModelInventoryInspector().inspect(
        "cursor",
        f"{base_model}[context=1m,effort=high,fast=false]",
    )

    assert result is not None
    assert result.state == "exact"
    assert result.matches == (base_model,)


def test_cursor_inventory_is_loaded_once_for_repeated_seats(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return agents.proc.Result(0, _cursor_listing("composer-2.5", "gpt-5.5-high"), "")

    monkeypatch.setattr(model_inventory.proc, "run", fake_run)
    inspector = model_inventory.ModelInventoryInspector()

    assert inspector.inspect("cursor", "composer-2.5").state == "exact"
    assert inspector.inspect("cursor", "gpt-5.5-high").state == "exact"
    assert calls == [["cursor-agent", "models"]]


def test_grok_inventory_parses_exact_and_fuzzy_models(monkeypatch):
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, _grok_listing("grok-4.5"), ""),
    )
    inspector = model_inventory.ModelInventoryInspector()

    assert inspector.inspect("grok", "grok-4.5").state == "exact"
    fuzzy = inspector.inspect("grok", "grok-4.5-xhigh")
    assert fuzzy.state == "fuzzy-resolved"
    assert fuzzy.matches == ("grok-4.5",)


def test_ollama_local_model_exact_match(monkeypatch):
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, _ollama_listing("llama3.2:3b"), ""),
    )

    result = model_inventory.ModelInventoryInspector().inspect("ollama:llama3.2:3b", "llama3.2:3b")

    assert result is not None
    assert result.state == "exact"
    assert "pulled locally" in result.detail


def test_ollama_absent_model_is_missing(monkeypatch):
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, _ollama_listing("other:cloud"), ""),
    )

    result = model_inventory.ModelInventoryInspector().inspect("ollama:missing:cloud", "missing:cloud")

    assert result is not None
    assert result.state == "missing"


def test_ollama_list_failure_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(1, "", "connection refused"),
    )

    result = model_inventory.ModelInventoryInspector().inspect("ollama:llama3.2:3b", "llama3.2:3b")

    assert result is not None
    assert result.state == "unavailable"


def test_ollama_malformed_successful_list_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, "output format changed\n", ""),
    )

    result = model_inventory.ModelInventoryInspector().inspect("ollama:llama3.2:3b", "llama3.2:3b")

    assert result is not None
    assert result.state == "unavailable"


def test_ollama_recognized_header_with_malformed_body_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(
            0,
            "NAME ID SIZE MODIFIED\nformat changed\n",
            "",
        ),
    )

    result = model_inventory.ModelInventoryInspector().inspect("ollama:llama3.2:3b", "llama3.2:3b")

    assert result is not None
    assert result.state == "unavailable"


def test_ollama_warning_shaped_body_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(
            0,
            "NAME ID SIZE MODIFIED\nwarning - - not inventory\n",
            "",
        ),
    )

    result = model_inventory.ModelInventoryInspector().inspect("ollama:llama3.2:3b", "llama3.2:3b")

    assert result is not None
    assert result.state == "unavailable"


def test_ollama_retired_cloud_model_is_missing(monkeypatch):
    def fake_run(argv, **kwargs):
        if argv == ["ollama", "list"]:
            return agents.proc.Result(0, _ollama_listing("glm-5:cloud"), "")
        return agents.proc.Result(1, "", "Error: glm-5 was retired at 2026-07-15")

    monkeypatch.setattr(model_inventory.proc, "run", fake_run)

    result = model_inventory.ModelInventoryInspector().inspect("ollama:glm-5:cloud", "glm-5:cloud")

    assert result is not None
    assert result.state == "missing"
    assert "retired" in result.detail


def test_ollama_cloud_probe_network_failure_is_unavailable(monkeypatch):
    def fake_run(argv, **kwargs):
        if argv == ["ollama", "list"]:
            return agents.proc.Result(0, _ollama_listing("kimi-k2.7-code:cloud"), "")
        return agents.proc.Result(1, "", "pull model manifest: Get registry: dial tcp: connection refused")

    monkeypatch.setattr(model_inventory.proc, "run", fake_run)

    result = model_inventory.ModelInventoryInspector().inspect("ollama:kimi-k2.7-code:cloud", "kimi-k2.7-code:cloud")

    assert result is not None
    assert result.state == "unavailable"
    assert "dial tcp" in result.detail


def test_ollama_inventory_is_loaded_once_for_different_seats(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return agents.proc.Result(0, _ollama_listing("llama3.2:3b", "qwen3-embedding:8b"), "")

    monkeypatch.setattr(model_inventory.proc, "run", fake_run)
    inspector = model_inventory.ModelInventoryInspector()

    assert inspector.inspect("ollama:llama3.2:3b", "llama3.2:3b").state == "exact"
    assert inspector.inspect("ollama:qwen3-embedding:8b", "qwen3-embedding:8b").state == "exact"
    assert calls == [["ollama", "list"]]


def test_unsupported_harness_has_no_live_inventory_check():
    assert model_inventory.ModelInventoryInspector().inspect("codex", "gpt-5.5") is None
