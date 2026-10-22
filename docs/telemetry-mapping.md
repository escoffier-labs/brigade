# Receipt telemetry mapping

`brigade receipts export otel-genai` and `brigade receipts export openinference`
project local Brigade receipts into JSONL rows that follow the OpenTelemetry
GenAI and OpenInference attribute conventions. The projections are export
formats only. Brigade receipts stay the storage authority, and nothing is sent
anywhere: the command writes to stdout or to the file passed with `--out`.

Every row carries a `mapping` object naming the upstream revision the mapping
was audited against. This page lists every attribute the projections emit.

## Upstream pins

| Projection | Mapping version | Upstream repository | Pinned revision | Revision date |
| --- | --- | --- | --- | --- |
| `otel-genai` | 1 | `open-telemetry/semantic-conventions-genai` | `9ffc7e00c639be7712e544e3e54331e885dccf84` | 2026-10-21 |
| `openinference` | 2 | `Arize-ai/openinference` | `python-openinference-semantic-conventions-v0.1.41` | 2026-10-01 |

The GenAI conventions moved out of `open-telemetry/semantic-conventions` into
`open-telemetry/semantic-conventions-genai`, which publishes no release tags,
so the OTel pin is a commit. Every `gen_ai.*` attribute below exists in that
commit's `model/gen-ai/registry.yaml`, and `invoke_agent` and `execute_tool`
are listed values of `gen_ai.operation.name` there. The OTel attribute set is
unchanged from the previous pin, so the OTel mapping version stays at 1.

OpenInference mapping version 2 adds `llm.request.model_name` and
`llm.response.model_name`. The names were checked against the released
`openinference-semantic-conventions` 0.1.41 package and the current
specification. An earlier upstream changelog entry used
`llm.input_model_name` and `llm.output_model_name`. Those names are not in the
released package and Brigade does not emit them.

## Status values

- **Development**: the attribute is marked `stability: development` at the
  pinned OTel revision. All GenAI attributes are at this level, so names can
  still change upstream.
- **Stable**: the attribute comes from the stable core OpenTelemetry
  conventions.
- **Released**: OpenInference does not assign stability levels. The attribute
  is defined in the pinned released package.
- **Brigade**: a Brigade-namespaced attribute with no upstream equivalent.

## Worker spans (`brigade.run.worker`)

One row per worker result in `.brigade/runs/<run>/worker-results.json`.

| Brigade field | OTel attribute | OpenInference attribute | Status |
| --- | --- | --- | --- |
| Span kind (constant) | `gen_ai.operation.name` = `invoke_agent` | `openinference.span.kind` = `AGENT` | OTel: Development. OpenInference: Released |
| Roster `cli` (`codex`, `claude`, `grok` only) | `gen_ai.provider.name` | Not emitted | Development |
| `requested_model` | `gen_ai.request.model` | `llm.request.model_name` | OTel: Development. OpenInference: Released |
| `effective_model` | `gen_ai.response.model` | `llm.response.model_name` | OTel: Development. OpenInference: Released |
| `effective_model`, else `requested_model` | Not emitted | `llm.model_name` | Released |
| `stop_reason` | `gen_ai.response.finish_reasons` | Not emitted | Development |
| Normalized error class | `error.type` | Not emitted | Stable |
| Seat name | `brigade.seat.name` | `brigade.seat.name` | Brigade |
| Adapter transport | `brigade.adapter.name` | `brigade.adapter.name` | Brigade |

Each model attribute is omitted when the receipt does not record that model.
Multi-provider CLIs get no `gen_ai.provider.name`, because the provider cannot
be inferred from the CLI name.

## Verify spans (`brigade.work.verify`)

One row per command in `.brigade/work/verify-runs/<id>/receipt.json`.

| Brigade field | OTel attribute | OpenInference attribute | Status |
| --- | --- | --- | --- |
| Span kind (constant) | `gen_ai.operation.name` = `execute_tool` | `openinference.span.kind` = `TOOL` | OTel: Development. OpenInference: Released |
| Tool name (constant) | `gen_ai.tool.name` | `tool.name` | OTel: Development. OpenInference: Released |
| `<receipt id>:<ordinal>` | `gen_ai.tool.call.id` | `tool.id` | OTel: Development. OpenInference: Released |
| Normalized error class | `error.type` | `error.type` | Stable |
| Receipt id | `brigade.receipt.id` | `brigade.receipt.id` | Brigade |
| Target directory name | `brigade.target.repo` | `brigade.target.repo` | Brigade |
| Executable basename only | `brigade.verify.command.executable` | `brigade.verify.command.executable` | Brigade |
| Command status | `brigade.verify.status` | `brigade.verify.status` | Brigade |
| Exit code | `brigade.verify.exit_code` | `brigade.verify.exit_code` | Brigade |
| Harness session harness | `brigade.harness.session.harness` | `brigade.harness.session.harness` | Brigade |
| Harness session fingerprint | `brigade.harness.session.fingerprint` | `brigade.harness.session.fingerprint` | Brigade |

## Outcome spans (`brigade.outcome.capture`)

One row per valid record in `memory/outcome/records.jsonl` that links to an
exported run or verify receipt.

| Brigade field | OTel attribute | OpenInference attribute | Status |
| --- | --- | --- | --- |
| Span kind (constant) | `gen_ai.operation.name` = `execute_tool` | `openinference.span.kind` = `TOOL` | OTel: Development. OpenInference: Released |
| Tool name (constant) | `gen_ai.tool.name` | `tool.name` | OTel: Development. OpenInference: Released |
| Outcome record id | `gen_ai.tool.call.id` | `tool.id` | OTel: Development. OpenInference: Released |
| Record digest | `brigade.receipt.id` | `brigade.receipt.id` | Brigade |
| Target directory name | `brigade.target.repo` | `brigade.target.repo` | Brigade |
| `artifact_id` | `brigade.outcome.artifact.id` | `brigade.outcome.artifact.id` | Brigade |
| `artifact_kind` | `brigade.outcome.artifact.kind` | `brigade.outcome.artifact.kind` | Brigade |
| `source` | `brigade.outcome.source` | `brigade.outcome.source` | Brigade |
| `signal_value` | `brigade.outcome.signal` | `brigade.outcome.signal` | Brigade |
| Current cohort score | `brigade.outcome.current.score` | `brigade.outcome.current.score` | Brigade |
| Current cohort helped count | `brigade.outcome.current.helped` | `brigade.outcome.current.helped` | Brigade |
| Current cohort hurt count | `brigade.outcome.current.hurt` | `brigade.outcome.current.hurt` | Brigade |
| Current cohort neutral count | `brigade.outcome.current.neutral` | `brigade.outcome.current.neutral` | Brigade |
| Promotion status | `brigade.outcome.current.status` | `brigade.outcome.current.status` | Brigade |

## Content that is never exported

The projections carry identifiers, model names, timings, and status only. They
never emit prompts, task text, worker output, error detail, full verify command
lines, or retrieved sources. None of these attributes appear in either
projection, and the exporter has no option to add them:

- OTel: `gen_ai.input.messages`, `gen_ai.output.messages`,
  `gen_ai.system_instructions`, `gen_ai.tool.call.arguments`,
  `gen_ai.tool.call.result`.
- OpenInference: `input.value`, `output.value`, `llm.input_messages`,
  `llm.output_messages`, `retrieval.documents`.

Adding any content-bearing attribute would need a new explicit opt-in and a new
mapping version.
