# Beads reconsideration check for #770

Date: 2026-10-20. This record summarizes the supplied isolated Linux spike
against Brigade `c6af3df43`. Its release and documentation observations were
retrieved on 2026-10-20. Publication dates below are separate from that retrieval
date. The command blocks describe a repeatable scratch setup for a later check.
They were reconstructed from the spike report and artifacts. The importer
implementation did not rerun those experiments.

## Decision and trigger

Keep Brigade's native work store. #770, closed on 2026-08-09, requires all four
clauses below before reconsidering Beads. #846 R3 keeps the machine-local JSON
ledger, and #1595 makes Hub work records the shared authority. A second Dolt
service would duplicate that authority. A read-only, additive export importer
lets bd-authored plans enter Brigade's existing review and receipt flow.

| Clause | Result on bd 1.3.1 | Observed evidence |
| --- | --- | --- |
| Stable shared-server mode | Partial | Shared mode worked with a separate Dolt binary and sql-server. Proxied mode is labelled experimental in `bd init --help`. `bd serve` refused an embedded workspace. |
| Claim CAS across clients | Met only in server mode | Six concurrent clients on one server produced one winner and five claim conflicts. Two embedded clones each claimed successfully before syncing. The losing clone's sync exited 2, then an explicit `theirs` merge replaced its claim. |
| Nested metadata queries | Not met | Nested `--metadata-field`, `--has-metadata-key`, and `bd query` filters returned `[]`. SQL `JSON_EXTRACT` worked only in server mode. |
| Storage stable across a major version | Not met | No 2.0 release existed. The 1.3.0 notes describe schema v53 to v66. Migration 0071 was on main, and the 1.4 roadmap proposed a new graph model. |

The trigger remains unmet. Gate conditions and an exclusive merge resource
belong in the Hub dependency and resource work proposed on #1595.

## Versions and binary provenance

The spike ran `bd version 1.3.1 (c1c4b642a)` and Dolt `2.5.0` on Linux amd64.
The latest stable listed was v1.3.1, published 2026-09-30. The latest prerelease
listed was v1.3.2-rc.2, published 2026-10-10. The prerelease was not tested.

| Artifact | SHA-256 | Verification in the spike |
| --- | --- | --- |
| `beads_1.3.1_linux_amd64.tar.gz` | `3219443a9734b89b93fb16ee8d65844759fa1b3cd3cf139c606b7353cfb0715c` | Local hash matched `checksums.txt` and the GitHub asset digest. `gh attestation verify` exited 0 with the repository release workflow at `refs/tags/v1.3.1`. |
| `dolt-linux-amd64.tar.gz`, v2.5.0 | `639b8042d1ea46ebf6b645a002fc787134dbe1dc02888cc51a51d1d3eb557c93` | Local hash matched the GitHub asset digest. This was a same-origin check, with no separate checksum file or attestation verification. |

The bd executable was 154,267,672 bytes and dynamically linked only libc.
An SPDX SBOM was published. The earlier #770 checksum mismatch was not
reproduced. These observations do not establish provenance for another release.

## Reproduction environment

Use Bash on a clean Linux amd64 machine with Git, GitHub CLI, curl, tar,
sha256sum, and Python 3. Run every initialization below in disposable repos.
`bd init` writes agent instructions, integrations, hooks, and Git commits.
The setup isolates bd's home through a per-process environment, and does not
install either binary globally.

```bash
spike_root=$(mktemp -d)
mkdir -p "$spike_root"/{bin,downloads,home,scratch}
cd "$spike_root/downloads"
gh release download v1.3.1 -R gastownhall/beads \
  -p beads_1.3.1_linux_amd64.tar.gz -p checksums.txt
sha256sum beads_1.3.1_linux_amd64.tar.gz
sha256sum --check --ignore-missing checksums.txt
gh api repos/gastownhall/beads/releases/tags/v1.3.1 \
  --jq '.assets[] | select(.name == "beads_1.3.1_linux_amd64.tar.gz") | .digest'
gh attestation verify beads_1.3.1_linux_amd64.tar.gz -R gastownhall/beads
tar -xzf beads_1.3.1_linux_amd64.tar.gz -C "$spike_root/bin" bd
curl -fL https://github.com/dolthub/dolt/releases/download/v2.5.0/dolt-linux-amd64.tar.gz \
  -o dolt-linux-amd64.tar.gz
sha256sum dolt-linux-amd64.tar.gz
gh api repos/dolthub/dolt/releases/tags/v2.5.0 \
  --jq '.assets[] | select(.name == "dolt-linux-amd64.tar.gz") | .digest'
tar -xzf dolt-linux-amd64.tar.gz
cp dolt-linux-amd64/bin/dolt "$spike_root/bin/dolt"
export PATH="$spike_root/bin:$PATH"
bd() { env HOME="$spike_root/home" "$spike_root/bin/bd" "$@"; }
bd version
dolt version
bd metrics off
git config --file "$spike_root/home/.gitconfig" user.name 'Example Tester'
git config --file "$spike_root/home/.gitconfig" user.email 'tester@example.invalid'
new_repo() {
  git init -q -b main "$spike_root/scratch/$1"
  git -C "$spike_root/scratch/$1" config user.name 'Example Tester'
  git -C "$spike_root/scratch/$1" config user.email 'tester@example.invalid'
}
```

For a later release, record its version, checksum, attestation result, schema
changes, and release date. Do not reuse these hashes for a different archive.

## Embedded metadata and two-clone claim race

```bash
new_repo A
cd "$spike_root/scratch/A"
bd init --prefix sp
item=$(bd create 'Claim race' --type task --priority 1 --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
bd update "$item" --metadata '{"flat":"x","footprint":{"files":["src/a.py"],"snapshot_hash":"abc"}}'
bd list --metadata-field flat=x --json
bd list --has-metadata-key footprint --json
bd list --metadata-field footprint.snapshot_hash=abc --json
bd list --metadata-field 'footprint.files[0]=src/a.py' --json
bd list --has-metadata-key footprint.files --json
bd query 'metadata.footprint.snapshot_hash=abc' --json
bd sql "SELECT id FROM issues WHERE JSON_EXTRACT(metadata, '$.footprint.snapshot_hash') = 'abc'"
bd serve
mkdir -p "$spike_root/scratch/doltremote"
bd dolt remote add origin "file://$spike_root/scratch/doltremote"
bd config set sync.remote "file://$spike_root/scratch/doltremote"
bd dolt push
new_repo B
cd "$spike_root/scratch/B"
bd init --prefix sp
mv .beads/embeddeddolt .beads/unused-initial-database
mkdir .beads/embeddeddolt
cd .beads/embeddeddolt
dolt clone "file://$spike_root/scratch/doltremote" sp
cd "$spike_root/scratch/B"
bd config set sync.remote "file://$spike_root/scratch/doltremote"
cd "$spike_root/scratch/A"
bd update "$item" --claim --actor agentA
printf 'A claim exit=%s\n' "$?"
cd "$spike_root/scratch/B"
bd update "$item" --claim --actor agentB
printf 'B claim exit=%s\n' "$?"
cd "$spike_root/scratch/A"
bd dolt push
cd "$spike_root/scratch/B"
bd sync
printf 'B sync exit=%s\n' "$?"
bd vc merge origin/main --strategy theirs
bd show "$item" --json
```

Observed: both claims exited 0. B's sync exited 2 with a merge conflict and
nothing pushed. The explicit recovery merge changed B's assignee to agentA.
Top-level metadata filters worked. All nested filters returned `[]`, except
the array-index filter, which failed key validation. Embedded SQL was refused.
The spike also observed `bd serve` refusing embedded mode. The HTTP API itself
was not exercised.

## Shared server and six-client race

Choose an unused loopback port. The server below is only for the scratch test.

```bash
mkdir -p "$spike_root/server"
cd "$spike_root/server"
dolt sql-server --host 127.0.0.1 --port 13317 > server.log 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null' EXIT
python3 - <<'PY'
import socket
import time
for attempt in range(100):
    try:
        with socket.create_connection(("127.0.0.1", 13317), timeout=0.2):
            break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit("scratch Dolt server did not start")
PY
export BEADS_DOLT_SERVER_PORT=13317
export BEADS_DOLT_SERVER_HOST=127.0.0.1
for client in C D
do
  new_repo "$client"
  cd "$spike_root/scratch/$client"
  bd init --shared-server --external --database sh --prefix sh
done
cd "$spike_root/scratch/C"
shared=$(bd create 'Shared claim race' --type task --priority 1 --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
for racer in 1 2 3 4 5 6
do
  (
    if (( racer % 2 == 0 ))
    then cd "$spike_root/scratch/D"
    else cd "$spike_root/scratch/C"
    fi
    bd update "$shared" --claim --actor "racer$racer"
    printf 'claim exit=%s\n' "$?"
  ) > "$spike_root/race$racer.out" 2>&1 &
done
wait $(jobs -p | sed "/^$server_pid\$/d")
cat "$spike_root"/race*.out
bd update "$shared" --if-assignee nobody --status blocked
printf 'guard exit=%s\n' "$?"
bd update "$shared" --metadata '{"flat":"x","footprint":{"files":["src/a.py"],"snapshot_hash":"abc"}}'
bd list --metadata-field footprint.snapshot_hash=abc --json
bd list --has-metadata-key footprint.files --json
bd query 'metadata.footprint.snapshot_hash=abc' --json
bd sql "SELECT id FROM issues WHERE JSON_EXTRACT(metadata, '$.footprint.files[0]') = 'src/a.py'"
bd export -o exp1.jsonl
deleted=$(bd create 'Export omission' --type task --priority 1 --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
bd export -o before-delete.jsonl
bd delete "$deleted" --force
bd export -o exp2.jsonl
kill "$server_pid"
wait "$server_pid"
trap - EXIT
```

Observed: one claim succeeded, five exited 1 with an already-claimed error.
The conditional update exited 13. Nested filters still returned `[]`, while
SQL extracted the nested value. The deleted issue disappeared from a subsequent
export, without a tombstone row. The retained spike exports contain two issue
rows before deletion and one afterward. Each export describes the remaining
issues at that point in time.

## Import contract and limits

`brigade work import --from-bd-jsonl export.jsonl --bd-version 1.3.1` stages
pending task proposals through the existing inbox gate. It never invokes bd.
The operator supplies the producer version because export rows do not contain
one. The receipt records the operator's version declaration.
Each attempt that reaches publication has its own
`.brigade/work/import-runs/<run-id>/receipt.json`, including duplicate-only
imports. Publication exceptions mark the receipt `failed` with the reason before
propagating the error. Successful publication marks it `completed`.
The input SHA-256 covers exact file bytes. Each row SHA-256 covers its
JSON line bytes without the line ending. Both digests and the version accompany
the locally stamped import provenance in `metadata.bd`.
Receipt storage requires the inbox gate's descriptor-relative directory
operations, supplied by POSIX descriptors or Windows handle-relative operations.
Hosts without either capability fail closed with `bd_import_io_error`.
The bd adapter was tested on Linux. Native Windows execution was not tested.

The adapter accepts issue rows and their nested dependency records. Unknown
row kinds or fields fail closed before any inbox items are written. The input
limit is 4 MiB. Every dependency endpoint must exist in the snapshot. A filtered
export that omits a prerequisite, or a cross-project dependency such as
`external:<project>:<id>` whose endpoint is absent, rejects the entire file with
`unknown_node_ref`. Export the complete local dependency closure and resolve
external dependencies before importing. Native
validation rejects unknown edge types, self-edges, and readiness cycles before
publication. `blocks` and `parent-child` point from the prerequisite or parent
to the issue. `discovered-from` points from the issue to its origin. Duplicate
dependency records produce one proposal. Proposed endpoints retain bd IDs for
review and must be mapped to native task IDs before applying them to the ledger.
The import does not activate edges or automatically promote tasks.

All bd issues become pending task proposals, with their original status and
type retained as evidence. Priorities 0 and 1 map to high, 2 to normal, and 3
and 4 to low. Description, design, notes, and acceptance criteria enter the
untrusted import surface. Producer metadata cannot supply provenance or
operational edge authority. Omitted issues never delete existing imports,
tasks, or edges. Deduplication fingerprints cover mapped text, priority,
acceptance criteria, proposed edges, bd status, and bd issue type. Timestamp,
heartbeat, lease, count, and producer metadata changes alone do not create new
items or revive dismissed items. The raw row digest still records those changes.
A change to fingerprinted content adds a new pending candidate for review,
including after dismissal. It does not replace the previous item.

Parent `--target` and `--json` options require `--from-bd-jsonl`. For existing
import subcommands, place their options after the subcommand, for example
`brigade work import add 'Review parser' --target example-repo`.

The claim result is one six-way run on one machine and one shared server.
Network partitions, federation, lease expiry, `--any-replica`, Windows,
proxied mode, HTTP serving, gates, merge slots, MCP, populated-store bd import,
and performance were not tested. The v53 to v66 migration count comes from
release notes. No schema diff was performed. Re-run every trigger clause before
reopening #770.

## Sources

- [#770 decision](https://github.com/escoffier-labs/brigade/issues/770)
- [#846 R3 characterization](issue-846-work-store-characterization-r3.md)
- [#1595 Hub dependency and resource proposal](https://github.com/escoffier-labs/brigade/issues/1595#issuecomment-6297902340)
- [bd v1.3.1 release](https://github.com/gastownhall/beads/releases/tag/v1.3.1)
- [bd v1.3.0 release and migrations](https://github.com/gastownhall/beads/releases/tag/v1.3.0)
- [Roadmap observed on 2026-10-20](https://github.com/gastownhall/beads/blob/main/ROADMAP.md)
- [Changelog observed on 2026-10-20](https://github.com/gastownhall/beads/blob/main/CHANGELOG.md)
- [Pinned exporter](https://github.com/gastownhall/beads/blob/v1.3.1/cmd/bd/export.go)
- [Pinned dependency row schema](https://github.com/gastownhall/beads/blob/v1.3.1/internal/types/types.go)
