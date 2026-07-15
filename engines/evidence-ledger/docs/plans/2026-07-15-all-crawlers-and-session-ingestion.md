# All Crawlers and Session Ingestion Plan

**Goal:** Add searchable Grok and current Cursor sessions, make Telegram work with Telecrawl 0.1.0's public JSON output, and prove every external crawler wrapper command end to end.

**Architecture:** Native harnesses remain `sources.Generator` implementations that emit `miseledger.adapter.v1` JSONL into the existing importer. Grok reads summary and chat files. Cursor keeps its legacy JSON parser and adds read-only access to Cursor's conversation-search SQLite database. External crawlers keep their adapter exporters, except Telegram, whose public JSON message array is converted at the wrapper boundary.

**Key technology:** Go standard library, `database/sql`, the existing `modernc.org/sqlite` dependency, synthetic fixtures, Brigade verification, and the existing archive smoke script.

Execute each task in order. Keep the checkboxes current, run every RED command before production edits, and commit after each task turns green.

## File map

- Create `internal/sources/grok/grok.go`: Grok summary and chat-history generator.
- Create `internal/sources/grok/grok_test.go`: Grok parser behavior and scan tests.
- Create `testdata/harnesses/grok-sessions.fixture/...`: synthetic Grok session files.
- Modify `internal/app/app.go`: Grok discovery, import, adapter, source counts, and fast-path registration.
- Modify `internal/app/watch.go`: Grok discovered root.
- Modify `internal/sources/cursor/cursor.go`: current Cursor root and conversation-search database reader.
- Modify `internal/sources/cursor/cursor_test.go`: Cursor database and root tests.
- Create `testdata/harnesses/cursor-conversations.fixture.sql`: synthetic Cursor search database schema and rows.
- Modify `internal/app/crawl.go`: Telegram compatibility route and current Cursor default behavior.
- Create `internal/app/telecrawl.go`: Telecrawl JSON-to-adapter wrapper.
- Modify `internal/app/crawlexport.go`: remove Telegram from adapter-exporter routing.
- Modify `internal/app/app_test.go`: CLI coverage for Grok, Cursor, Telegram, and all exporter contracts.
- Modify `scripts/smoke_archive.sh`: import and search the new native fixtures.
- Modify `README.md`, `CHANGELOG.md`, and the routed docs named in the spec: source lists, commands, current paths, and compatibility notes.
- Create `.claude/memory-handoffs/2026-07-15-all-crawlers-and-session-ingestion.md`: durable format and verification notes.

## Demi scope

Actual ask: make every documented crawl and session ingestion path executable and checked.

Smallest useful slice: 2 native parsers, 1 Telegram compatibility wrapper, and complete command-contract coverage for the remaining source-owned exporters.

Highest rung that holds: existing generator and adapter-import primitives, `database/sql`, the installed SQLite driver, and Telecrawl's public JSON command.

Existing patterns: `internal/sources/codex`, `internal/sources/cursor`, `internal/app/crawlexport.go`, and `TestCrawlExternalExporterWrappersIncludeGithubAndTelegram`.

Cut from scope: new network clients, external tool installation, Cursor cloud history, Grok telemetry, and external repository edits.

Growth trigger: add another compatibility converter only when an installed crawler lacks adapter output and has a versioned public read-only JSON command.

Verification: focused RED/GREEN tests per task, then `go vet ./...`, `go test ./...`, and `scripts/smoke_archive.sh` through Brigade.

### Task 1: Grok native sessions

**Files:**
- Create: `internal/sources/grok/grok.go`
- Create: `internal/sources/grok/grok_test.go`
- Create: `testdata/harnesses/grok-sessions.fixture/%2Fworkspaces%2Fdemo-project/demo-session-001/summary.json`
- Create: `testdata/harnesses/grok-sessions.fixture/%2Fworkspaces%2Fdemo-project/demo-session-001/chat_history.jsonl`
- Modify: `internal/app/app.go`
- Modify: `internal/app/watch.go`
- Modify: `internal/app/app_test.go`

- [x] Add the synthetic summary fixture with title `Synthetic Grok crawler audit`, model `grok-code-fast-1`, timestamps, branch, commit, workspace, and a fake session summary. Add chat lines for `system`, `user`, `assistant`, `reasoning`, and `tool_result`; content must use only invented project names and text.
- [x] Write `grok_test.go` with this failing behavior:

```go
func TestGenerateFixtureEmitsSummaryAndMessages(t *testing.T) {
    var buf bytes.Buffer
    res, err := Generate("../../../testdata/harnesses/grok-sessions.fixture", sources.Options{}, &buf)
    if err != nil { t.Fatal(err) }
    recs := decodeRecords(t, &buf)
    if res.Records != 6 || len(recs) != 6 { t.Fatalf("records=%d decoded=%d", res.Records, len(recs)) }
    for _, rec := range recs {
        if rec.Source.Kind != "grok" { t.Fatalf("source=%q", rec.Source.Kind) }
        if rec.Collection.Kind != "agent_session" { t.Fatalf("collection=%q", rec.Collection.Kind) }
        if rec.Collection.ExternalID != "grok:session:demo-session-001" { t.Fatalf("collection id=%q", rec.Collection.ExternalID) }
    }
    if !strings.Contains(buf.String(), "Synthetic Grok crawler audit") || !strings.Contains(buf.String(), "fixture crawl contract") {
        t.Fatal("summary or chat text did not round-trip")
    }
}
```

- [x] Add tests for `DefaultRoot`, `--limit`, `--since`, malformed chat JSON warnings, URL-decoded workspace metadata, and `AfterFile` scan callbacks.
- [x] Run RED through Brigade:

```text
brigade work verify run --target . --command "go test ./internal/sources/grok" --capture brigade-work
```

Expected failure: package `internal/sources/grok` does not exist.

- [x] Implement `grok.Generate` with the existing `sources.Options`, `PrepareFileScan`, `WriteRecord`, `TextFromAny`, `StableID`, `ApplyRedaction`, and `KeepTimestamp` helpers. Walk only `summary.json` and `chat_history.jsonl`; sort paths; use `url.PathUnescape` for the encoded workspace directory; count warnings per file; call `AfterFile` exactly once per candidate file.
- [x] Add Grok to `discoverSources`, `cmdImport`, `cmdAdapter`, `supportsNativeFastPath`, and `discoveredRoots`. Use `filepath.Join(home, ".grok", "sessions")` everywhere.
- [x] Add a CLI test that copies the synthetic fixture into `<temp HOME>/.grok/sessions`, runs `crawl sessions --json`, and finds `fixture crawl contract` through `sessions search --source grok`.
- [x] Run GREEN:

```text
brigade work verify run --target . --command "go test ./internal/sources/grok ./internal/app" --capture brigade-work
```

Expected output: both packages report `ok`.

- [x] Commit:

```text
git add internal/sources/grok testdata/harnesses/grok-sessions.fixture internal/app/app.go internal/app/watch.go internal/app/app_test.go
git commit -m "feat(import): add Grok session ingestion"
```

### Task 2: Current Cursor conversation database

**Files:**
- Modify: `internal/sources/cursor/cursor.go`
- Modify: `internal/sources/cursor/cursor_test.go`
- Create: `testdata/harnesses/cursor-conversations.fixture.sql`
- Modify: `internal/app/app.go`
- Modify: `internal/app/app_test.go`

- [x] Add a SQL fixture that creates `conversations` and an FTS5 `conversation_fts`, then inserts 2 local conversations with fake IDs, titles, bodies, millisecond timestamps, archive states, and root fingerprints.
- [x] Add this test helper and failing test:

```go
func buildConversationFixture(t *testing.T) string {
    t.Helper()
    sqlText, err := os.ReadFile("../../../testdata/harnesses/cursor-conversations.fixture.sql")
    if err != nil { t.Fatal(err) }
    root := t.TempDir()
    dbPath := filepath.Join(root, "globalStorage", "conversation-search.db")
    if err := os.MkdirAll(filepath.Dir(dbPath), 0o700); err != nil { t.Fatal(err) }
    db, err := sql.Open("sqlite", dbPath)
    if err != nil { t.Fatal(err) }
    if _, err := db.Exec(string(sqlText)); err != nil { t.Fatal(err) }
    if err := db.Close(); err != nil { t.Fatal(err) }
    return root
}

func TestGenerateConversationSearchDatabase(t *testing.T) {
    root := buildConversationFixture(t)
    recs, res := parseRecords(t, root, sources.Options{})
    if res.Records != 2 || len(recs) != 2 { t.Fatalf("records=%d decoded=%d", res.Records, len(recs)) }
    if !strings.Contains(recs[0].Item.Text+recs[1].Item.Text, "synthetic migration checklist") {
        t.Fatal("conversation body was not indexed")
    }
    for _, rec := range recs {
        if rec.Collection.Kind != "agent_session" { t.Fatalf("kind=%q", rec.Collection.Kind) }
        if rec.Raw.Path != filepath.Join(root, "globalStorage", "conversation-search.db") { t.Fatalf("raw=%q", rec.Raw.Path) }
    }
}
```

- [x] Add RED tests for a direct database path, `--since`, `--limit`, missing required tables as warnings, read-only mode, and WAL scan-target selection.
- [x] Change `TestDefaultRootRespectsXDG` to expect `/tmp/xdg/Cursor/User` on Linux. Add a platform helper test for Linux, macOS, and Windows path rules without changing the process OS.
- [x] Run RED:

```text
brigade work verify run --target . --command "go test ./internal/sources/cursor" --capture brigade-work
```

Expected failure: 0 database records and the old lowercase default root.

- [x] Extend `cursor.Generate` to recognize a direct `conversation-search.db`, a Cursor user-data root containing `globalStorage/conversation-search.db`, and the existing legacy JSON layout. Open SQLite with `mode=ro`; query only the six named columns; convert `updated_at` from seconds or milliseconds; emit stable IDs `cursor:conversation:<id>` and `cursor:conversation-body:<id>`; apply redaction before writing.
- [x] Use `conversation-search.db-wal` as the `PrepareFileScan` target when it exists. Keep `Raw.Path` and `source_file` on the main database. Treat `no such table` as one warning and a successful zero-record result; return other open/query failures.
- [x] Update `countCursorSessions` to count rows in the current database without returning titles or bodies. Update the app default-root test so `crawl cursor` builds a synthetic database under the current user-data root and proves its body is searchable.
- [x] Run GREEN:

```text
brigade work verify run --target . --command "go test ./internal/sources/cursor ./internal/app" --capture brigade-work
```

Expected output: both packages report `ok`.

- [x] Commit:

```text
git add internal/sources/cursor testdata/harnesses/cursor-conversations.fixture.sql internal/app/app.go internal/app/app_test.go
git commit -m "feat(import): read current Cursor conversation history"
```

### Task 3: Telegram compatibility and all wrapper contracts

**Files:**
- Create: `internal/app/telecrawl.go`
- Modify: `internal/app/crawl.go`
- Modify: `internal/app/crawlexport.go`
- Modify: `internal/app/app_test.go`

- [x] Replace `TestCrawlExternalExporterWrappersIncludeGithubAndTelegram` with a table that covers Discrawl, Gitcrawl, Slacrawl, Graincrawl, Notcrawl, and Mailcrawl. Each fake binary checks the exact prefix, including `mailcrawl gmail export --out -`, emits one valid adapter line, and the test searches the inserted text by wrapper source kind.
- [x] Add a fake Telecrawl script that requires arguments `--json messages`, accepts `--limit`, `--chat`, and `--after`, and emits this public shape:

```json
[
  {
    "source_pk": 41,
    "chat_jid": "fixture-chat",
    "chat_name": "Fixture Telegram Chat",
    "message_id": "fixture-message-41",
    "sender_jid": "fixture-sender",
    "sender_name": "Fixture Sender",
    "timestamp": "2026-07-15T12:00:00Z",
    "from_me": false,
    "text": "telecrawl compatibility fixture",
    "raw_type": 1,
    "message_type": "text",
    "media_type": "",
    "media_title": ""
  }
]
```

- [x] Add tests proving normal import, dry-run count, search, missing-tool preflight, non-zero stderr propagation, `--since` to `--after` mapping, and empty-media filtering.
- [x] Run RED:

```text
brigade work verify run --target . --command "go test ./internal/app -run 'TestCrawlExternal|TestCrawlTelegram'" --capture brigade-work
```

Expected failure: MiseLedger invokes `telecrawl export adapter --out -` instead of `telecrawl --json messages`.

- [x] Define `telecrawlMessage` with the public v0.1.0 JSON fields. Implement `cmdCrawlTelecrawl(args, out, errw)` to split MiseLedger flags, validate pass-through flags, require the binary before archive open, map `--since` to `--after`, execute with the existing timeout, decode the JSON array, convert messages to adapter records, and either count them for dry-run or call `ingest.ImportAdapterReader`.
- [x] Build text as `message.Text`, falling back to `media_title` and then `media_type`. Skip records with no searchable text. Use `message_id`, falling back to decimal `source_pk`, for stable item IDs. Use `ActorFromRole("telegram", role, "message")`, where `role` is `user` for `from_me` and `external` otherwise. Preserve sender and media fields in metadata.
- [x] Route `crawl telegram` directly to `cmdCrawlTelecrawl` and remove Telegram from `nativeExporters`. Keep Telecrawl in doctor wrapper-tool reporting.
- [x] Run GREEN:

```text
brigade work verify run --target . --command "go test ./internal/app -run 'TestCrawlExternal|TestCrawlTelegram|TestMissingExternalTool|TestDoctorWrapperTools'" --capture brigade-work
```

Expected output: `ok github.com/escoffier-labs/miseledger/internal/app`.

- [x] Commit:

```text
git add internal/app/telecrawl.go internal/app/crawl.go internal/app/crawlexport.go internal/app/app_test.go
git commit -m "fix(crawl): support installed Telecrawl JSON output"
```

### Task 4: Smoke coverage and documentation

**Files:**
- Modify: `scripts/smoke_archive.sh`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/ADAPTER_CONTRACT.md`
- Modify: `docs/ADJACENT_TOOLS.md`
- Modify: `docs/EXAMPLES.md`
- Modify: `docs/INSTALL_SMOKE.md`
- Modify: `docs/LIVE_DRY_RUN_CHECKLIST.md`
- Modify: `docs/QUICKSTART.md`
- Modify: `docs/ROADMAP.md`
- Modify: `docs/STATIONTRAIL_PARITY.md`

- [x] Add Grok fixture import and search to `scripts/smoke_archive.sh`. Add Cursor database smoke only if the script can create the SQL fixture with the existing `sqlite3` prerequisite; otherwise keep it in Go tests and state that boundary in `docs/INSTALL_SMOKE.md`.
- [x] Update every native-source list to include Grok. Document the current Cursor database path and retained legacy JSON support. Document Telegram's `telecrawl --json messages` compatibility route. Keep external-tool configuration examples explicit.
- [x] Add Unreleased changelog entries for Grok, current Cursor, complete wrapper tests, and Telecrawl 0.1.0 compatibility.
- [x] Run the public-writing checklist from `~/bin/writing-rules.md`, inspect the changed prose for private infrastructure or identity details, and run the whitespace check:

```text
git diff --check
```

Expected result: no output. Changed prose contains no private machine details or banned writing patterns.

- [x] Run focused documentation and smoke verification:

```text
brigade work verify run --target . --command "scripts/check_docs_drift.sh" --capture brigade-work
brigade work verify run --target . --command "scripts/smoke_archive.sh" --capture brigade-work
```

Expected output: both commands exit 0; archive smoke prints `smoke archive ok`.

- [x] Commit:

```text
git add scripts/smoke_archive.sh README.md CHANGELOG.md docs
git commit -m "docs: cover Grok Cursor and crawler contracts"
```

### Task 5: Final verification and memory handoff

**Files:**
- Modify: `docs/plans/2026-07-15-all-crawlers-and-session-ingestion.md`
- Create: `.claude/memory-handoffs/2026-07-15-all-crawlers-and-session-ingestion.md`

- [x] Read the complete diff against the spec. Confirm no live native-session import command was run and no database, WAL, export, or raw-session files entered git status.
- [x] Run the required final checks fresh after the last edit:

```text
brigade work verify run --target . --command "go vet ./..." --capture brigade-work
brigade work verify run --target . --command "go test ./..." --capture brigade-work
brigade work verify run --target . --command "go build -o bin/miseledger ./cmd/miseledger" --capture brigade-work
brigade work verify run --target . --command "scripts/smoke_archive.sh" --capture brigade-work
```

Expected result: all four exit 0.

- [x] Write and lint the memory handoff using `.claude/memory-handoffs/TEMPLATE.md`. Record observed Grok and Cursor formats, the Telecrawl 0.1.0 compatibility decision, external environment gaps, and exact verification receipts.
- [x] Mark every completed checkbox in this plan, run `git diff --check`, and commit the live plan. The handoff stays in its gitignored inbox for memory handoff ingestion:

```text
git add docs/plans/2026-07-15-all-crawlers-and-session-ingestion.md
git commit -m "chore: record crawler ingestion verification"
```

- [ ] Report the branch, commits, exact check results, live count-only compatibility results, and any environment-only blocker. Offer the four Fire completion options without merging, pushing, or deleting the worktree automatically.
