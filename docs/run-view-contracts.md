# Run view contracts, v1

The `brigade runs` JSON contracts are command-level serializers. Consumers call
`runs_list_payload` and `run_detail_payload`, or use `watch` with
`json_output=True`. They must not read run artifacts directly.

## Bounds and text handling

Every artifact-derived string is collapsed to one line and capped at 240
characters. Identifier-like fields use a 128-character cap. Unix, home,
Windows absolute, and UNC paths in serialized text are replaced with `[path]`.
Contract field names and static enum values are not artifact-derived.

## `brigade.runs-list.v1`

Successful `brigade runs list --json` output is one JSON object with:

- `schema`: the literal `brigade.runs-list.v1`.
- `runs`: ordered run summaries, limited by `--limit`.
- `skipped_invalid`: number of child directories with missing or invalid
  `run.json`.

For this JSON serializer only, symlink children and directory names outside
`[A-Za-z0-9][A-Za-z0-9._-]{0,127}` are invalid and count toward
`skipped_invalid`. The existing human list command retains its current
collection behavior.

When a caller passes `missing_root_as_empty=True` to `runs_list_payload`, a
missing runs directory returns the same object with an empty `runs` list,
`skipped_invalid: 0`, and `diagnostic: "runs directory is unavailable"`.
Without that explicit option, a missing root is a command error. The CLI keeps
its existing error status and stderr behavior.

Every summary has exactly these keys:

- `run_id`
- `status`
- `active_phase`
- `task_summary`
- `started_at`
- `status_started_at`
- `finished_at`
- `duration_seconds`
- `failure_phase`
- `mode`
- `stale`
- `resume_available`

Nullable values are represented as JSON `null`. `mode` is one of `normal`,
`read-only`, `dry-run`, or `read-only, dry-run`. `stale` is a safe boolean
derived from the existing timeout check.

## `brigade.run-detail.v1`

Successful `brigade runs show --json` and `brigade runs latest --json` output
has exactly these top-level keys:

- `schema`
- `run`
- `roster`
- `plan`
- `workers`
- `synthesis`
- `verification`
- `briefs`

`run` is the list-summary object above plus nullable, bounded `failure_kind`
and `failure_detail` fields. Those failure fields are detail-only and never
appear in the list or watch summary. `roster` is a list of seat objects
allowing only `name`, `cli`, `model`, `reasoning`, and
`timeout_seconds`. `plan` is a list allowing only `stage`, `order`, `worker`,
and `task_summary`. `workers` is a list allowing only `worker`,
`task_summary`, `status`, `ok`, `detail`, `duration_seconds`, and `exit_code`.

`synthesis` is an object allowing only `orchestrator`, `ok`, and `detail`.
It never contains result text. `verification` entries allow only `receipt_id`,
`status`, `duration_seconds`, `exit_code`, and a bounded `command_label`.
The label comes from `command_label` or `label` when an artifact provides one;
otherwise it is only the parsed executable basename. Arguments are never
serialized.

`briefs` has the fixed keys `code_graph`, `drift_impact`, and `evidence`.
Each brief has only `attached`, `size_bytes`, `count`, and `summary`; the
`evidence` object also has `untrusted: true`. Brief content is not exposed.

## `brigade.run-watch.v1`

`brigade runs watch --json` remains newline-delimited JSON: one object per
line, in the existing record type sequence. Every record has `schema` set to
`brigade.run-watch.v1` and retains its `type`.

- `watch`: `schema`, `type`, `run_id`.
- `run`: `schema`, `type`, `run` (the safe summary object).
- `plan`: `schema`, `type`, `assignments` (safe plan entries).
- `event`: `schema`, `type`, `worker`, optional `method`, optional
  `item_type`.
- `workers`: `schema`, `type`, `workers` (safe worker entries).
- `synthesis`: `schema`, `type`, `synthesis` (safe synthesis object).
- `final`: `schema`, `type`, `available`, `size_bytes`.
- `summary`: `schema`, `type`, `run_id`, `status`, optional
  `duration_seconds`, optional `failure_phase`, optional `resume_available`.

Raw event payloads are omitted. Passing a `record_sink` to `watch` receives
the exact versioned object that would otherwise be printed. With a sink, watch
does not write those JSON records to stdout. Setting
`recover_artifact_collection=False` leaves artifact-collection runs read-only:
watch reports their observed state without invoking stale-run recovery.

## Privacy exclusions

No v1 record exposes a run or workspace path, `cwd`, artifact or handoff path,
environment map or value, endpoint, authentication or control token, secret
reference, prompt, transcript, thread identifier, raw stdout, raw stderr, log
body, final text, or synthesis result text. The serializers use positive field
allowlists, so unrecognized artifact keys are excluded by default.
