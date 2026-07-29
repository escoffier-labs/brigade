from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


BENCHMARK_DIR = Path(__file__).resolve().parent
ROOT = BENCHMARK_DIR.parents[1]
sys.path.insert(0, str(ROOT / "src"))

NEW_MODELS = {
    "opencode-go/kimi-k3",
    "opencode-go/glm-5.2",
    "opencode-go/deepseek-v4-pro",
    "opencode-go/qwen3.7-max",
    "opencode-go/kimi-k2.7-code",
}
EARLIER_MODELS = {
    "opencode-go/minimax-m3",
    "opencode-go/qwen3.7-plus",
    "opencode-go/deepseek-v4-flash",
    "opencode-go/mimo-v2.5",
}
CATALOG_MODELS = {
    "opencode-go/deepseek-v4-flash",
    "opencode-go/deepseek-v4-pro",
    "opencode-go/glm-5.1",
    "opencode-go/glm-5.2",
    "opencode-go/grok-4.5",
    "opencode-go/hy3",
    "opencode-go/kimi-k2.6",
    "opencode-go/kimi-k2.7-code",
    "opencode-go/kimi-k3",
    "opencode-go/mimo-v2.5",
    "opencode-go/mimo-v2.5-pro",
    "opencode-go/minimax-m2.7",
    "opencode-go/minimax-m3",
    "opencode-go/qwen3.6-plus",
    "opencode-go/qwen3.7-max",
    "opencode-go/qwen3.7-plus",
}
ALL_TASKS = {"routine", "contract_drift", "security_review"}
EARLIER_TASKS = {"routine", "contract_drift"}
TASK_FINDINGS = {"routine": 1, "contract_drift": 2, "security_review": 2}
NEW_RANKING = [
    "opencode-go/glm-5.2",
    "opencode-go/kimi-k3",
    "opencode-go/kimi-k2.7-code",
    "opencode-go/deepseek-v4-pro",
    "opencode-go/qwen3.7-max",
]
EARLIER_RANKING = [
    "opencode-go/minimax-m3",
    "opencode-go/qwen3.7-plus",
    "opencode-go/deepseek-v4-flash",
    "opencode-go/mimo-v2.5",
]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    require(isinstance(data, dict), f"{path}: expected a JSON object")
    return data


def validate_schema(data: dict[str, Any], schema: dict[str, Any]) -> str:
    try:
        import jsonschema
    except ModuleNotFoundError:
        return "jsonschema package unavailable; syntax and semantic checks only"
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(data, schema)
    return "Draft 2020-12 schema and instance valid"


def validate_models(data: dict[str, Any]) -> None:
    models = {model["model_id"]: model for model in data["models"]}
    require(len(data["models"]) == 9, "expected exactly nine model records")
    require(len(models) == 9, "duplicate model IDs")
    require(
        sum(len(model["cells"]) for model in data["models"]) == 23,
        "expected exactly 23 task/model cells",
    )
    require(set(models) == NEW_MODELS | EARLIER_MODELS, "unexpected model set")
    catalog_ids = data["catalog_discovery"]["subscription_model_ids"]
    require(
        len(catalog_ids) == 16 and set(catalog_ids) == CATALOG_MODELS,
        "subscription catalog does not match discovery",
    )
    task_findings = {task["id"]: task["expected_findings"] for task in data["tasks"]}
    require(task_findings == TASK_FINDINGS, "task finding totals mismatch")

    for model_id, model in models.items():
        expected_tasks = ALL_TASKS if model_id in NEW_MODELS else EARLIER_TASKS
        cells = model["cells"]
        require(
            {cell["task"] for cell in cells} == expected_tasks,
            f"{model_id}: task coverage mismatch",
        )
        require(
            all(cell["requested_model"] == model_id for cell in cells),
            f"{model_id}: requested model mismatch",
        )
        require(
            all(cell["resolved_model"] == model_id for cell in cells),
            f"{model_id}: resolved model mismatch",
        )
        require(
            all(cell["transport"] == "cli" for cell in cells),
            f"{model_id}: non-CLI transport",
        )
        require(
            not any(cell["fallback_used"] for cell in cells),
            f"{model_id}: fallback recorded",
        )
        require(
            all(cell["exit_code"] == 0 and not cell["timed_out"] for cell in cells),
            f"{model_id}: unsuccessful process",
        )
        require(
            all(cell["changed_file_count"] == 0 for cell in cells),
            f"{model_id}: changed files recorded",
        )
        require(
            not any(cell["usage"]["exposed"] for cell in cells),
            f"{model_id}: unexpected usage exposure",
        )
        require(
            all(
                cell["expected_findings_total"] == TASK_FINDINGS[cell["task"]]
                and cell["expected_findings_found"] <= cell["expected_findings_total"]
                for cell in cells
            ),
            f"{model_id}: impossible or task-inconsistent finding recall",
        )

        summary = model["summary"]
        require(summary["cells"] == len(cells), f"{model_id}: cell count mismatch")
        sums = {
            "expected_findings_found": sum(cell["expected_findings_found"] for cell in cells),
            "expected_findings_total": sum(cell["expected_findings_total"] for cell in cells),
            "output_contract_passes": sum(cell["output_contract_valid"] for cell in cells),
            "refusals": sum(cell["refused"] for cell in cells),
        }
        for key, value in sums.items():
            require(summary[key] == value, f"{model_id}: {key} mismatch")
        mean = (sum(Decimal(str(cell["latency_seconds"])) for cell in cells) / len(cells)).quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )
        require(
            Decimal(str(summary["mean_latency_seconds"])) == mean,
            f"{model_id}: mean latency mismatch",
        )


def validate_rankings(data: dict[str, Any]) -> None:
    rankings = data["rankings"]
    new = rankings["new_three_task_matrix"]
    earlier = rankings["earlier_two_task_screen"]
    require([item["rank"] for item in new] == list(range(1, 6)), "new ranks")
    require([item["rank"] for item in earlier] == list(range(1, 5)), "earlier ranks")
    require([item["model_id"] for item in new] == NEW_RANKING, "new ranking order")
    require(
        [item["model_id"] for item in earlier] == EARLIER_RANKING,
        "earlier ranking order",
    )
    roles = rankings["role_selections"]
    require(
        roles["primary_cheap_coder_reviewer"] == "opencode-go/minimax-m3",
        "primary candidate mismatch",
    )
    require(
        roles["security_capable_reviewer"] == "opencode-go/glm-5.2",
        "security reviewer mismatch",
    )
    require(
        roles["cross_file_canary"] == "opencode-go/kimi-k3",
        "cross-file canary mismatch",
    )
    require(
        roles["promotion_gate"]
        == {
            "model_id": "opencode-go/minimax-m3",
            "status": "post-shadow-candidate",
            "required_shadow_runs": 20,
            "current_fallback_during_shadow": "kimi-k2.7",
        },
        "MiniMax promotion gate mismatch",
    )


def validate_roster() -> None:
    from brigade.roster import load_roster

    roster = load_roster(BENCHMARK_DIR / "proposed-opencode-go-roster.toml")
    require(roster.orchestrator == "go_glm_52_security", "orchestrator mismatch")
    require(roster.max_workers == 2, "per-run worker limit mismatch")
    require(roster.sandbox == "read-only", "roster sandbox mismatch")
    require(
        roster.agents["go_minimax_m3_primary"].fallback == ("go_glm_52_worker_fallback",),
        "MiniMax fallback topology mismatch",
    )


def validate_trial(data: dict[str, Any], artifacts: Path | None) -> None:
    trial = data["multi_seat_trial"]
    require(trial["status"] == "ok", "multi-seat status mismatch")
    require(trial["changed_file_count"] == 0, "multi-seat changed files")
    require(trial["plan_assignments"] == 1, "multi-seat assignment count")
    require(trial["final_contains"] == "WIRING_OK", "multi-seat final marker")
    for phase in ("orchestrator", "worker"):
        require(trial[phase]["transport"] == "cli", f"{phase}: transport mismatch")
        require(not trial[phase]["fallback_used"], f"{phase}: fallback recorded")
        require(trial[phase]["exit_code"] == 0, f"{phase}: nonzero exit")

    if artifacts is None:
        return
    run = load_json(artifacts / "run.json")
    plan = load_json(artifacts / "plan.json")
    workers = load_json(artifacts / "worker-results.json")
    synthesis = load_json(artifacts / "synthesis.json")
    enforcement = load_json(artifacts / "read-only-enforcement.json")
    final = (artifacts / "final.txt").read_text()
    worker_stderr = (artifacts / "logs/worker-001-minimax_coder.stderr.log").read_text()
    synthesis_stderr = (artifacts / "logs/synthesis.stderr.log").read_text()

    require(run["status"] == trial["status"], "artifact run status mismatch")
    require(
        Decimal(str(run["duration_seconds"])) == Decimal(str(trial["duration_seconds"])),
        "artifact duration mismatch",
    )
    require(run["read_only"] is True, "artifact run was not read-only")
    require(run["pre_run_snapshot"]["tracked_dirty_files"] == [], "dirty pre-run")
    require(run["pre_run_snapshot"]["untracked_files"] == [], "untracked pre-run")
    require(len(plan["assignments"]) == trial["plan_assignments"], "plan mismatch")
    require(plan["assignments"][0]["worker"] == trial["worker"]["seat"], "worker seat")

    worker = workers["results"][0]
    require(
        worker["requested_model"] == trial["worker"]["requested_model"],
        "worker model mismatch",
    )
    require(
        trial["worker"]["resolved_model"] == worker["requested_model"],
        "worker resolved model mismatch",
    )
    require(
        Decimal(str(worker["duration_seconds"])) == Decimal(str(trial["worker"]["latency_seconds"])),
        "worker latency mismatch",
    )
    require(worker["transport"] == "cli" and worker["exit_code"] == 0, "worker exit")
    require(
        workers["ground_truth"]["changed_files"] == [] and workers["ground_truth"]["untracked_files"] == [],
        "worker changed or untracked files",
    )

    final_result = synthesis["result"]
    require(
        final_result["requested_model"] == trial["orchestrator"]["requested_model"],
        "orchestrator model mismatch",
    )
    require(
        trial["orchestrator"]["resolved_model"] == final_result["requested_model"],
        "orchestrator resolved model mismatch",
    )
    require(
        Decimal(str(final_result["duration_seconds"]))
        == Decimal(str(trial["orchestrator"]["synthesis_latency_seconds"])),
        "synthesis latency mismatch",
    )
    require(
        final_result["transport"] == "cli" and final_result["exit_code"] == 0,
        "synthesis exit",
    )
    require(
        synthesis["ground_truth"]["changed_files"] == [] and synthesis["ground_truth"]["untracked_files"] == [],
        "synthesis changed or untracked files",
    )
    require(trial["final_contains"] in final, "final marker absent")
    require(
        trial["worker"]["resolved_model_marker"] in worker_stderr,
        "worker model marker absent",
    )
    require(
        trial["orchestrator"]["resolved_model_marker"] in synthesis_stderr,
        "orchestrator model marker absent",
    )
    require(
        set(enforcement["best_effort_agents"])
        == {
            "glm_security_reviewer (opencode): read-only is not applied for this CLI",
            "minimax_coder (opencode): read-only is not applied for this CLI",
        },
        "read-only warning mismatch",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()

    data = load_json(BENCHMARK_DIR / "opencode-go-results.json")
    schema = load_json(BENCHMARK_DIR / "opencode-go-schema.json")
    schema_result = validate_schema(data, schema)
    validate_models(data)
    validate_rankings(data)
    validate_roster()
    validate_trial(data, args.artifacts)
    cells = sum(len(model["cells"]) for model in data["models"])
    print(f"benchmark validation: ok; models={len(data['models'])} cells={cells}")
    print(f"schema validation: {schema_result}")
    print(f"multi-seat artifacts: {'checked' if args.artifacts else 'not supplied'}")


if __name__ == "__main__":
    main()
