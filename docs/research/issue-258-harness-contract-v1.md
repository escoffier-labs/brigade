# Harness contract v1 research

Issue 258 records an evidence model for comparing harness surfaces without
turning a version check into a configuration or session probe. The normative
data is in `docs/research/fixtures/harness-contract.v1/`, validated by
`docs/proposals/harness-contract.v1.schema.json` and the portable probe.

## Evidence policy

Every capability cell records provenance, support state, implementation layers,
evidence, tested version, and platform. `native` means the harness provides
the surface. `hook_composed`, `extension_composed`, and `brigade_adapter` name
layers that can compose together through `implementation_layers`. They are not
interchangeable with native support.

`unsupported` means the research did not establish support. `externally_blocked`
means a specified probe could not run. Neither state is a claim that the vendor
can never provide the capability.

## Receipt index

Current-commit verification receipts live in PR and CI evidence. This report's
receipt index is limited to historical research and version evidence.

Historical receipts from the first research pass remain in fixture evidence
references only where they document observed vendor versions:

- first pass: `20260718-042433-8e55d674`
- scoped runs: `20260718-044429-114cf3b9` (Codex), `20260718-044429-fce38b9d`
  (Cursor), `20260718-044429-090d0ae6` (Grok)
- version receipts: `20260718-044953-work-verify-28cebe` (Codex),
  `20260718-044953-work-verify-8f1b1a` (Claude Code),
  `20260718-044953-work-verify-a284a1` (Cursor),
  `20260718-044953-work-verify-98988d` (Grok),
  `20260718-044954-work-verify-e85ad4` (Pi),
  `20260718-044954-work-verify-680cf2` (Antigravity),
  `20260718-044954-work-verify-b3e4ea` (Hermes),
  `20260718-044954-work-verify-3b30f4` (OpenClaw),
  `20260718-044955-work-verify-182132` (OpenCode)

The receipt-backed Linux versions are Codex CLI `0.144.5`, Claude Code
`2.1.211`, Cursor CLI `2026.07.16-899851b`, Grok CLI `0.2.103`, Pi `0.80.6`,
OpenCode `1.17.18`, and OpenClaw `2026.7.1-beta.2`.

Native Windows command facts live in
`docs/research/evidence/direct-native-windows-command-probe-2026-07-18.json`.
That direct probe observed Codex CLI `codex-cli 0.144.5`, Codex desktop package
`26.707.9981.0`, Cursor agent `2026.07.13-7fe37d2`, Cursor desktop present
`true`, `agy` present `false`, and `antigravity` present `false`. There is no
MiseLedger evidence ID for that direct probe.

The Linux Antigravity receipt only attempted the `antigravity` command and
reported it as not resolvable. The fixture models both observed candidates
`agy` and `antigravity` without overstating what the Linux receipt proved.

## Primary documentation

- Codex: [CLI](https://learn.chatgpt.com/docs/codex/cli),
  [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md),
  [hooks](https://learn.chatgpt.com/docs/hooks), and
  [MCP](https://learn.chatgpt.com/docs/extend/mcp).
- Claude Code: [hooks](https://code.claude.com/docs/en/hooks),
  [memory](https://code.claude.com/docs/en/memory),
  [MCP](https://code.claude.com/docs/en/mcp), and
  [skills](https://code.claude.com/docs/en/skills).
- Cursor: [CLI usage](https://docs.cursor.com/en/cli/using) and
  [rules](https://docs.cursor.com/context/rules).
- Grok: [CLI reference](https://docs.x.ai/build/cli/reference).
- Pi: [extensions](https://pi.dev/docs/latest/extensions),
  [settings](https://pi.dev/docs/latest/settings),
  [session format](https://pi.dev/docs/latest/session-format),
  [SDK](https://pi.dev/docs/latest/sdk), and [RPC](https://pi.dev/docs/latest/rpc).
- Antigravity: [skills codelab](https://codelabs.developers.google.com/getting-started-with-antigravity-skills).
- OpenCode: [plugins](https://opencode.ai/docs/plugins),
  [skills](https://opencode.ai/docs/skills), [tools](https://opencode.ai/docs/tools),
  and [agents](https://opencode.ai/docs/agents).
- Hermes: [primary repository](https://github.com/NousResearch/hermes-agent).
- OpenClaw: [agent concepts](https://docs.openclaw.ai/concepts/agent) and
  [skills](https://docs.openclaw.ai/skills).

## Brigade assumptions that need a contract gate

`src/brigade/selection.py:KNOWN_HARNESSES` accepts a single identifier for a
harness where the contract needs separate CLI, GUI, and desktop surfaces.
`WRITER_INBOXES` assigns a fixed project path per identifier, which is a
Brigade convention, not evidence that every harness discovers or enforces it.

`src/brigade/install.py:install_selection` writes each adapter's declared
directory and its skill projection. Its `wire_skills` loop treats a local
install path as sufficient to wire a harness, while the contract requires an
observed discovery result before calling the capability native.

`src/brigade/doctor.py:build_context` defaults an unconfigured target to
`["claude"]`, except for OpenClaw and Hermes. `core_station_checks` then runs
Claude-specific work-loop checks. This is a useful compatibility fallback, but
it generalizes Claude behavior to unspecified harnesses.

`src/brigade/skills_cmd.py:HARNESS_ADAPTERS` labels several mappings
`built-in`. The mapping is a Brigade projection contract. It must stay separate
from vendor-native skills, hooks, MCP, reload, and session behavior.

`src/brigade/skills_cmd.py:_hermes_home` uses the presence of a Hermes home
directory as an installation proxy. The current probe found no binary, so a
home directory cannot substitute for a runtime conformance result.

## Safe conformance probe

Run this command from a checkout:

```bash
python tools/harness_conformance_probe.py \
  --fixtures-dir docs/research/fixtures/harness-contract.v1
```

Default mode resolves bare executable availability only. Pass `--run-version`
to execute validated CLI fixtures with `--version` inside an isolated temporary
cwd and temporary HOME or config directories. Even with those guards, executing
a third-party binary can still read ambient environment data, perform network
I/O, or run vendor-defined side effects. Treat `--run-version` as an explicit
operator opt-in.

The probe validates every fixture against the shipped Draft 7 schema before any
command resolution or execution. Invalid fixtures are not executable. Command
candidates must be bare executable names with exact `["--version"]` args. It
uses empty stdin, a minimal PATH built from the resolved executable parent plus
platform system defaults, a finite positive timeout, reader-thread streaming
collection with a real 64 KiB combined output cap, and process-tree termination
on timeout or overflow. POSIX runs use `start_new_session` process groups;
Windows runs use `CREATE_NEW_PROCESS_GROUP` and `taskkill /PID <pid> /T /F`
before direct kill. Exceptions after spawn always clean up the process tree.
Availability JSON emits command booleans or safe basenames only, never resolved
absolute executable paths. Output redaction covers real host home, temporary
home, inherited PATH home segments, assignment secrets, authorization headers,
bare Bearer credentials, and quoted JSON credential fields. Deep instruction,
skill, hook, MCP, workspace, session, verification, handoff, reload, telemetry,
and platform probes stay declared in the fixture and are never executed by this
command.

Codex Desktop and Cursor GUI are external-only surfaces. Antigravity is a GUI
surface whose availability probe resolves only the `agy` and `antigravity`
command candidates without executing vendor commands or claiming more than each
candidate check observed.

Older receipts such as `20260718-050147-work-verify-618e4b` remain failed
historical evidence when cited from fixture cells.

## Follow-up issue split

1. **Add surface-aware harness selection**
   - Acceptance: selection can identify `cursor-cli` and `cursor-gui` without
     collapsing their evidence, and installation refuses a fixture with an
     externally blocked binary unless the caller explicitly requests a
     projection-only install.

2. **Make doctor evidence-aware**
   - Acceptance: `build_context` does not default an unspecified harness to
     Claude, and doctor output labels adapter checks as adapter checks.

3. **Probe live discovery in isolated sandboxes**
   - Acceptance: every non-blocked fixture has one receipt for each declared
     deep probe, with no user configuration writes, sessions, or secrets in
     captured output.

4. **Validate Hermes and Antigravity runtimes**
   - Acceptance: record a vendor-binary version and platform receipt, or keep
     the cells externally blocked with the exact missing-binary result.
