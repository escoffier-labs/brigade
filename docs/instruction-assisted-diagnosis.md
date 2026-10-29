# Diagnose Codex project instruction visibility

Use `brigade bug diagnose --component instructions` to explain the project
instruction chain modeled for a chosen working directory. This helps when an
operator expects instructions that do not appear in the selected chain.

The current model supports caller-supplied Codex 0.160.0 settings on POSIX.
Brigade does not launch Codex, detect its installed version or inspect an active
conversation. Actual session loading and model behavior remain unknown.

## Preview a selected project

From an approved project, supply absolute project and working-directory paths:

```sh
brigade bug diagnose --component instructions \
  --target "$PWD" --cwd "$PWD/subproject" --harness codex \
  --codex-version 0.160.0 --trust trusted --read-access full \
  --assume-codex-defaults --instruction-path subproject/AGENTS.md \
  --summary "Expected this project's instructions in the selected working directory"
```

Replace the version, trust and read-access values with the effective settings
you intend to model. Unsupported or unknown values remain unevaluated.
Both `full` and `restricted` read-access declarations are supported by the
existing preview contract. Neither declaration proves a live session has access.

`--assume-codex-defaults` labels omitted project settings as assumptions.
For explicit settings, use `--project-doc-max-bytes`, `--fallback-filenames` and
`--root-markers`. Filename lists use JSON, such as `--root-markers '[".git"]'`.
The producer retains its byte and list limits. There is no automatic home scan
or global instruction scope in this diagnosis mode.

## Read the result

Each selected file shows its project-relative path and modeled contribution:

- `loaded`: the selected file contributes to the ordered chain.
- `empty`: its inspected portion contains no contributing non-whitespace text.
- `truncated`: only part fits within the supplied project byte cap.
- `cap_exhausted`: earlier contributions used the remaining project budget.
- `disabled`: supplied untrusted trust or a zero cap disables project loading.
- `no_selected_files`: a complete modeled inspection selected no project files.
- `not_selected`: the optional expected label is absent from the selected chain.
- `not_evaluated`: assumptions or inspection did not establish a complete chain.

Within each selected directory, `AGENTS.override.md` takes filename precedence
over `AGENTS.md`, followed by supplied fallback names. Files from selected
ancestor directories still contribute in chain order. A nearer file does not
automatically erase the entire ancestor contribution.

`--instruction-path` is only a relative expected label. Brigade compares it with
the producer's selected rows and never opens or stats an additional file for
that label. `not_selected` does not prove the file is missing or establish why
it was excluded. Confirm your working directory, instruction filename and
effective settings before using `brigade context preview` with the same inputs.

Output contains redacted metadata and contribution accounting, not instruction
bodies. Every result keeps actual session observation false, runtime unknown
and verification not-run. The evidence digest hashes only the redacted public
projection. Default preview makes no intentional writes, although filesystem
access times can change during inspection.

## Save a finding or prepare a report

Add `--save-finding ./new-finding.jsonl` to the preview command to create one
new finding file. Existing files and unsafe output paths are refused. Review it
through the existing import controls:

```sh
brigade work import validate ./new-finding.jsonl --json
brigade work import ingest ./new-finding.jsonl --target . --dry-run --json
```

After review, ingest without `--dry-run` creates a pending inbox item. Task
promotion and execution remain separate operator choices. Import dry-run can
create the importer's empty lock sidecars.

Alternatively, add `--prepare-report` to save and preview a local report through
the existing bug-report redaction and security checks. Only the collected
diagnosis is included. No general diagnostic subprocess, failure-history read
or issue submission runs. Public submission requires the existing separate
reviewed-report consent flow. Security concerns retain private-report guidance.

The two output options are mutually exclusive. `--json` emits structured output.
Omitting `--component` keeps the existing MCP diagnosis behavior. Instruction
settings cannot be combined with MCP mode, and MCP's `--name` is not an
instruction-path selector.
