# acpx Cursor Adapter

## Goal

Add an opt-in, one-shot Cursor ACP transport without adding a runtime
dependency or adopting acpx session ownership.

## Boundary

- Require user-installed acpx at a reviewed exact version and Cursor CLI with `cursor-agent acp`.
- Invoke acpx with its raw agent command escape hatch.
- Use strict JSON, no terminal capability, explicit timeout, cwd, model, and
  permission mode.
- Allow writable ACP only in a Brigade-created worktree.
- Parse ACP protocol 1 NDJSON into the common agent result.
- Keep direct Cursor as the default and never fall back silently.
- Do not add persistent sessions, cancellation, queue recovery, Node, an ACP
  SDK, or a separate repository.

## Verification

- [x] Test exact version and command construction.
- [x] Test strict NDJSON, protocol metadata, final text, and model acknowledgment.
- [x] Test invalid output, empty final output, timeout, and permission failures.
- [x] Test roster validation and dispatch selection.
- [x] Run focused tests and `./scripts/verify` through Brigade.
- [x] Attempt a live local smoke test and record the environmental blocker or result.

## Live result

The installed Cursor CLI `2026.07.09-a3815c0` completed an ACP initialize
handshake at protocol version 1 and advertised load-session, MCP, prompt, and
session capabilities. No model prompt was sent. A full acpx smoke run could not
start because `acpx` is not installed on this machine. Brigade does not install
it automatically.


## Reviewed exact versions

| acpx version | Source and fixture check date | Authenticated Cursor battery |
| --- | --- | --- |
| `0.12.0` | 2026-10-22 | Previously recorded for `composer-2.5` and `grok-4.5` in the technical guide. |
| `0.19.4` | 2026-10-22 | Pending orchestrator execution. This worker did not run authenticated prompts. |

The allowlist contains only these two exact strings. A seat's
`transport_version` must also match the installed executable exactly, even when
both versions are reviewed. `0.20.0`, prereleases, and version ranges are refused.
The earlier live result above describes the initial adapter attempt, before the
subsequent authenticated 0.12.0 checks recorded in the technical guide.

### Upstream compatibility review

Retrieved on 2026-10-22 from `openclaw/acpx`. The
[v0.19.4 release](https://github.com/openclaw/acpx/releases/tag/v0.19.4)
was published on 2026-10-01. Its changelog section is dated 2026-09-30.
The review compared tagged source at v0.12.0 and v0.19.4 and read the intervening
[changelog](https://github.com/openclaw/acpx/blob/v0.19.4/CHANGELOG.md).

- Stream shape: the [JSON formatter](https://github.com/openclaw/acpx/blob/v0.19.4/src/cli/output/json-formatter.ts)
  still writes one raw ACP JSON-RPC object per line, without an event envelope.
  `--json-strict` still requires `--format json` and suppresses informational
  stderr. Direction-aware read suppression changed internally, but Brigade does
  not enable `--suppress-reads`. v0.17 added local lifecycle records to saved
  `.stream.ndjson` journals. Brigade consumes live stdout, so that journal change
  does not require a parser change.
- Permissions: [permission resolution](https://github.com/openclaw/acpx/blob/v0.19.4/src/permissions.ts)
  still auto-approves read/search kinds under `--approve-reads` and fails an
  unavailable prompt under `--non-interactive-permissions fail`. Tool-title
  inference now matches complete leading names instead of substrings. Permission
  cancellation, stale-owner checks, and visible terminal controls in prompts were
  hardened. Existing permission error and late-error fixtures remain applicable.
- Capabilities: the [ACP client](https://github.com/openclaw/acpx/blob/v0.19.4/src/acp/client.ts)
  now registers filesystem and terminal callbacks only when enabled. v0.17.1
  strengthened enforcement of `--no-terminal` against direct requests.
- Flags and exits: the [CLI flags](https://github.com/openclaw/acpx/blob/v0.19.4/src/cli/flags.ts)
  retain every flag used by `build_argv`, including the raw `--agent` command and
  `exec`. Windows launch handling changed in v0.13, but the two-token
  `cursor-agent acp` command remains unambiguous. The
  [exit codes](https://github.com/openclaw/acpx/blob/v0.19.4/docs/exit-codes.md)
  remain 0 success, 1 runtime/protocol error, 2 usage, 3 timeout, 4 no session,
  5 all permissions denied, and 130 interruption.
- Model acknowledgment: newer versions prefer the actual model configuration
  control and preserve accepted selections and omitted option catalogs. Brigade
  already parses `configOptions` and legacy model acknowledgments. v0.14 added
  one-shot `--config-option`, which remains outside this change's scope.
- The [v0.20.0 release](https://github.com/openclaw/acpx/releases/tag/v0.20.0)
  was published on 2026-10-20. Its permission callback can select an exact
  offered `optionId` and cancels unknown IDs. It also updates the ACP SDK and
  repairs failed-session lifecycle handling. Those embedding changes do not
  establish Cursor compatibility. Version 0.20.0 is excluded.

No wire-format or invocation adaptation was required. Existing success, timeout,
permission, startup, and late-permission fixtures now exercise both reviewed
versions. Unreviewed versions fail before executable probing.

### Authenticated live procedure for the orchestrator

No dedicated live-battery script was found in the repository. On the authorized
host with acpx 0.19.4 and an authenticated `cursor-agent`, save the following as
`.brigade/acpx-live-battery.py`. Run it through Brigade with:

```bash
brigade work verify run --target . --argv-json '[".venv/bin/python", ".brigade/acpx-live-battery.py"]' --timeout 360 --capture brigade-work
```

```python
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from brigade import acpx_adapter

version, error = acpx_adapter.installed_version()
assert version == "0.19.4", error or "install the exact acpx version under review"
assert acpx_adapter.cursor_auth_status().state == "authenticated"
with TemporaryDirectory(prefix="acpx-battery-") as directory:
    target = Path(directory)
    fixture = target / "probe.txt"
    fixture.write_text("ACP_READ_PROBE_1972\n", encoding="utf-8")
    for model in ("composer-2.5", "grok-4.5"):
        result = acpx_adapter.run_cursor(
            "Read probe.txt using a read tool. Return its token verbatim in your "
            "final assistant answer. Do not write files or run terminal commands.",
            cwd=target,
            timeout=120,
            model=model,
            version=version,
            read_only=True,
        )
        assert result.ok, (model, result.detail)
        assert result.protocol_version == 1
        assert result.stop_reason == "end_turn"
        assert result.effective_model
        assert "ACP_READ_PROBE_1972" in result.text
        frames = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        assert all(frame.get("jsonrpc") == "2.0" for frame in frames)
        initialize = next(frame for frame in frames if frame.get("method") == "initialize")
        assert not initialize["params"]["clientCapabilities"].get("terminal", False)
        assert any(frame.get("method") == "session/request_permission" for frame in frames)
        assert list(target.iterdir()) == [fixture]
        assert fixture.read_text(encoding="utf-8") == "ACP_READ_PROBE_1972\n"
        print(model, "passed", result.effective_model, result.transport_warning)
```

`run_cursor` uses `--approve-reads --non-interactive-permissions fail`,
`--format json --json-strict`, and `--no-terminal`. The procedure checks both
models, protocol metadata, permission traffic, final text, and the unchanged
temporary fixture. Inspect the acknowledged model IDs and any transport warning
in the receipt before recording live acceptance. Keep raw authenticated streams
out of tracked fixtures and docs. Do not mark the pending battery as passed
without the resulting receipt.
