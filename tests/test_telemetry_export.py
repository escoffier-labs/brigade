from __future__ import annotations

import json

from brigade import cli


def _run(tmp_path):
    run = tmp_path / ".brigade" / "runs" / "run-1"
    run.mkdir(parents=True)
    (run / "run.json").write_text(
        json.dumps({"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:02Z", "task": "SECRET"})
    )
    (run / "roster.json").write_text(json.dumps({"agents": {"worker": {"cli": "grok", "model": "grok-4.5"}}}))
    (run / "worker-results.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "worker": "worker",
                        "ok": True,
                        "text": "PRIVATE OUTPUT",
                        "duration_seconds": 2.0,
                        "transport": "acpx",
                        "requested_model": "grok-4.5",
                        "effective_model": "grok-4.5",
                        "stop_reason": "end_turn",
                        "exit_code": 0,
                    }
                ]
            }
        )
    )


def test_otel_projection_uses_genai_names_and_omits_content(tmp_path, capsys):
    _run(tmp_path)
    assert cli.main(["receipts", "export", "otel-genai", "--target", str(tmp_path)]) == 0
    raw = capsys.readouterr().out
    row = json.loads(raw)
    assert row["attributes"]["gen_ai.operation.name"] == "invoke_agent"
    assert row["attributes"]["gen_ai.provider.name"] == "x_ai"
    assert row["attributes"]["gen_ai.request.model"] == "grok-4.5"
    assert "SECRET" not in raw and "PRIVATE OUTPUT" not in raw


def test_openinference_projection_is_content_free(tmp_path, capsys):
    _run(tmp_path)
    assert cli.main(["receipts", "export", "openinference", "--target", str(tmp_path)]) == 0
    raw = capsys.readouterr().out
    row = json.loads(raw)
    assert row["attributes"]["openinference.span.kind"] == "AGENT"
    assert row["attributes"]["llm.model_name"] == "grok-4.5"
    assert "input.value" not in raw and "output.value" not in raw
