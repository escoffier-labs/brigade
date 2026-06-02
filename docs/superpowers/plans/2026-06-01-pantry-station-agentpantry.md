# Pantry Station / agentpantry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register the Go tool `agentpantry` with Brigade as the single managed tool on a new `pantry` station, so `brigade add pantry` / `brigade doctor` can install and health-check it.

**Architecture:** Brigade shells out to `agentpantry status --json` (a new machine-readable mode with an exit-code convention) via the existing managed-tool pattern. agentpantry stays its own Go repo (moved to the `escoffier-labs` org), independently `go install`-able. No library coupling back into Brigade.

**Tech Stack:** Go 1.x (agentpantry, stdlib `flag`/`encoding/json`), Python 3.10+ (Brigade, pytest).

**Two repos:**
- Repo A: `~/repos/agentpantry` (Go) - Tasks 1-2
- Repo B: `~/repos/brigade` (Python) - Tasks 3-6

**Models for subagents:** opus (sonnet acceptable). Never haiku.

---

## File Structure

agentpantry repo:
- Modify: `cmd/agentpantry/main.go` - `cmdStatus` gains `--json` + exit-code convention; add `encoding/json`, `errors` imports.
- Create: `cmd/agentpantry/status_test.go` - build-once-and-exec test of the status contract.
- Modify: `go.mod` + all `*.go` import paths - org move `solomonneas` -> `escoffier-labs`.
- Modify: `CHANGELOG.md`, `README.md` (install URL).

brigade repo:
- Modify: `src/brigade/registry.py` - add `PANTRY` station, add to `_BUILTIN`.
- Modify: `src/brigade/doctor.py` - add `pantry_station_checks`.
- Modify: `src/brigade/managed.py` - add `agentpantry` `ManagedTool` + `_agentpantry_doctor`; fix stale satellite URLs.
- Modify: `tests/test_registry.py`, `tests/test_managed.py`.
- Modify: `CHANGELOG.md`, `README.md`, `ROADMAP.md` (station enumeration if present).

---

## Task 1: agentpantry `status --json` contract

**Repo:** `~/repos/agentpantry`

**Files:**
- Create: `cmd/agentpantry/status_test.go`
- Modify: `cmd/agentpantry/main.go` (`cmdStatus`, imports)

Contract: `agentpantry status [--config PATH] [--json]`
- exit `0` = config present and loaded
- exit `2` = unwired (no config file at PATH) - stderr explains
- exit `1` = real error (unreadable/invalid config)
- with `--json`, stdout is the payload below; without `--json`, human output is unchanged.

JSON payload:
```json
{
  "role": "source|sink|",
  "configured": true,
  "peer": "host:port",
  "key_present": true,
  "surfaces": ["sidecar"],
  "browsers": 0,
  "allow": [],
  "deny": []
}
```

- [ ] **Step 1: Write the failing test**

Create `cmd/agentpantry/status_test.go`:

```go
package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// buildBin compiles the agentpantry binary once into a temp dir and returns its path.
func buildBin(t *testing.T) string {
	t.Helper()
	bin := filepath.Join(t.TempDir(), "agentpantry")
	cmd := exec.Command("go", "build", "-o", bin, ".")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("build failed: %v\n%s", err, out)
	}
	return bin
}

// runStatus runs the binary and returns (exitCode, stdout, stderr).
func runStatus(t *testing.T, bin string, args ...string) (int, string, string) {
	t.Helper()
	cmd := exec.Command(bin, append([]string{"status"}, args...)...)
	stdout, err := cmd.Output()
	if ee, ok := err.(*exec.ExitError); ok {
		return ee.ExitCode(), string(stdout), string(ee.Stderr)
	}
	if err != nil {
		t.Fatalf("run error: %v", err)
	}
	return 0, string(stdout), ""
}

func TestStatusJSONUnwired(t *testing.T) {
	bin := buildBin(t)
	missing := filepath.Join(t.TempDir(), "nope.toml")
	code, _, stderr := runStatus(t, bin, "--json", "--config", missing)
	if code != 2 {
		t.Fatalf("want exit 2 for missing config, got %d (stderr=%s)", code, stderr)
	}
}

func TestStatusJSONConfigured(t *testing.T) {
	bin := buildBin(t)
	dir := t.TempDir()
	cfg := filepath.Join(dir, "config.toml")
	body := "role = \"source\"\npeer = \"127.0.0.1:8787\"\nkey_path = \"" +
		filepath.Join(dir, "psk.key") + "\"\nsurfaces = [\"sidecar\"]\n"
	if err := os.WriteFile(cfg, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	code, stdout, stderr := runStatus(t, bin, "--json", "--config", cfg)
	if code != 0 {
		t.Fatalf("want exit 0, got %d (stderr=%s)", code, stderr)
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(stdout), &payload); err != nil {
		t.Fatalf("stdout is not JSON: %v\n%s", err, stdout)
	}
	if payload["role"] != "source" {
		t.Fatalf("want role=source, got %v", payload["role"])
	}
	if payload["key_present"] != false {
		t.Fatalf("want key_present=false (no key file written), got %v", payload["key_present"])
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/agentpantry && go test ./cmd/agentpantry/ -run TestStatusJSON -v`
Expected: FAIL. `TestStatusJSONUnwired` likely fails because the current binary exits `1` (config load error) not `2`; `TestStatusJSONConfigured` fails because `--json` is an undefined flag (flag package exits 2 with "flag provided but not defined: -json").

- [ ] **Step 3: Implement `--json` + exit codes**

In `cmd/agentpantry/main.go`, add `encoding/json` and `errors` to the import block:

```go
import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/solomonneas/agentpantry/internal/config"
	// ... existing internal imports unchanged ...
)
```

Replace `cmdStatus` with:

```go
func cmdStatus(args []string) error {
	fs := flag.NewFlagSet("status", flag.ExitOnError)
	cfgPath := fs.String("config", filepath.Join(config.Dir(), "config.toml"), "config path")
	jsonOut := fs.Bool("json", false, "machine-readable JSON output")
	fs.Parse(args)

	if _, statErr := os.Stat(*cfgPath); errors.Is(statErr, os.ErrNotExist) {
		fmt.Fprintln(os.Stderr, "unwired: no config at", *cfgPath)
		os.Exit(2)
	}

	c, err := config.Load(*cfgPath)
	if err != nil {
		return err // -> main exits 1
	}

	_, keyErr := os.Stat(c.KeyPath)
	keyPresent := keyErr == nil

	if *jsonOut {
		allow := c.Domains.Allow
		if allow == nil {
			allow = []string{}
		}
		deny := c.Domains.Deny
		if deny == nil {
			deny = []string{}
		}
		surfaces := c.Surfaces
		if surfaces == nil {
			surfaces = []string{}
		}
		payload := map[string]any{
			"role":        c.Role,
			"configured":  true,
			"peer":        c.Peer,
			"key_present": keyPresent,
			"surfaces":    surfaces,
			"browsers":    len(c.Browsers),
			"allow":       allow,
			"deny":        deny,
		}
		b, err := json.MarshalIndent(payload, "", "  ")
		if err != nil {
			return err
		}
		fmt.Println(string(b))
		return nil
	}

	fmt.Printf("role:     %s\npeer:     %s\nkey:      %s\nsurfaces: %v\nbrowsers: %d\nallow:    %v\ndeny:     %v\n",
		c.Role, c.Peer, c.KeyPath, c.Surfaces, len(c.Browsers), c.Domains.Allow, c.Domains.Deny)
	return nil
}
```

Note: the old `loadConfig` helper (FlagSet "cfg") is still used by `cmdSource`/`cmdSink`; leave it in place. `cmdStatus` no longer calls it.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/repos/agentpantry && go test ./cmd/agentpantry/ -run TestStatusJSON -v`
Expected: PASS (both subtests).

- [ ] **Step 5: Verify the full suite + human output still works**

Run: `cd ~/repos/agentpantry && go build ./... && go test ./...`
Expected: build clean, all tests pass.

- [ ] **Step 6: Commit**

```bash
cd ~/repos/agentpantry
git add cmd/agentpantry/main.go cmd/agentpantry/status_test.go
git commit -m "feat(status): add --json output and exit-code convention"
```

---

## Task 2: agentpantry org move (solomonneas -> escoffier-labs)

**Repo:** `~/repos/agentpantry`

**Files:** `go.mod` and every `*.go` containing the module path.

> **Manual prerequisite (out of band):** the GitHub repo must be transferred/created under the `escoffier-labs` org and a tag pushed before `go install github.com/escoffier-labs/agentpantry/cmd/agentpantry@latest` works for end users. The local rewrite below is independent and unblocks Brigade integration immediately via local builds.

- [ ] **Step 1: Rewrite the module path everywhere**

Run:
```bash
cd ~/repos/agentpantry
grep -rl 'solomonneas/agentpantry' --include='*.go' . go.mod \
  | xargs sed -i 's#solomonneas/agentpantry#escoffier-labs/agentpantry#g'
```

- [ ] **Step 2: Verify no stale references remain**

Run: `cd ~/repos/agentpantry && grep -rn 'solomonneas/agentpantry' . || echo CLEAN`
Expected: `CLEAN`.

- [ ] **Step 3: Rebuild and re-test under the new module path**

Run: `cd ~/repos/agentpantry && go build ./... && go test ./...`
Expected: build clean, all tests pass.

- [ ] **Step 4: Update README install instruction**

In `~/repos/agentpantry/README.md`, change any `go install github.com/solomonneas/agentpantry/...` (or clone URL) to `github.com/escoffier-labs/agentpantry/...`. If the README has no install line, add one:

```markdown
## Install

    go install github.com/escoffier-labs/agentpantry/cmd/agentpantry@latest
```

- [ ] **Step 5: Commit**

```bash
cd ~/repos/agentpantry
git add -A
git commit -m "chore: move module path to escoffier-labs org"
```

---

## Task 3: Brigade `pantry` station

**Repo:** `~/repos/brigade`

**Files:**
- Modify: `src/brigade/doctor.py` (add `pantry_station_checks`)
- Modify: `src/brigade/registry.py` (add `PANTRY`, extend `_BUILTIN`)
- Test: `tests/test_registry.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_registry.py`, add to `test_resolve_by_name_and_alias`:

```python
    assert registry.resolve("pantry").name == "pantry"
    assert registry.resolve("larder").name == "pantry"
```

And add a new test:

```python
def test_pantry_station_declares_agentpantry():
    pantry = registry.resolve("pantry")
    assert pantry is not None
    assert set(pantry.tools) == {"agentpantry"}
    assert callable(pantry.doctor)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/brigade && python -m pytest tests/test_registry.py -q`
Expected: FAIL (`registry.resolve("pantry")` is `None`).

- [ ] **Step 3: Add the station-check function**

In `src/brigade/doctor.py`, directly after `tokens_station_checks`:

```python
def pantry_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    # The agentpantry managed tool carries this station's signal; the station
    # itself lays down no per-workspace files.
    return []
```

- [ ] **Step 4: Register the station**

In `src/brigade/registry.py`, add after the `SECURITY` station definition:

```python
PANTRY = Station(
    name="pantry",
    summary="agent session auth sync",
    aliases=("larder",),
    doctor=_doctor.pantry_station_checks,
    tools=("agentpantry",),
)
```

Then extend the builtin tuple:

```python
_BUILTIN: Tuple[Station, ...] = (CORE, MEMORY, GUARD, TOKENS, SECURITY, PANTRY)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ~/repos/brigade && python -m pytest tests/test_registry.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd ~/repos/brigade
git add src/brigade/doctor.py src/brigade/registry.py tests/test_registry.py
git commit -m "feat(pantry): add pantry station with agentpantry tool"
```

---

## Task 4: Brigade `agentpantry` managed tool + URL fix

**Repo:** `~/repos/brigade`

**Files:**
- Modify: `src/brigade/managed.py` (add `_agentpantry_doctor`, the `ManagedTool`, fix stale URLs)
- Test: `tests/test_managed.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_managed.py`, update `test_tools_attach_to_known_stations`:

```python
def test_tools_attach_to_known_stations():
    stations = {t.station for t in managed.all_tools()}
    assert stations <= {"memory", "guard", "tokens", "pantry"}
```

Add new tests:

```python
def test_agentpantry_doctor_unwired(monkeypatch):
    t = managed.resolve("agentpantry")
    assert t is not None and t.station == "pantry"
    monkeypatch.setattr(managed.proc, "which", lambda c: "/x/" + c)

    def fake_run(args, **kw):
        return managed.proc.Result(code=2, stdout="", stderr="unwired: no config")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" and "unwired" in detail for status, _, detail in results)


def test_agentpantry_doctor_parses_status(monkeypatch):
    t = managed.resolve("agentpantry")
    monkeypatch.setattr(managed.proc, "which", lambda c: "/x/" + c)

    def fake_run(args, **kw):
        return managed.proc.Result(
            code=0,
            stdout='{"role": "source", "configured": true, "peer": "127.0.0.1:8787",'
                   ' "key_present": true, "surfaces": ["sidecar"], "browsers": 1,'
                   ' "allow": [], "deny": []}',
            stderr="",
        )

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "OK" and "agentpantry" in name for status, name, _ in results)


def test_agentpantry_never_fails_workspace(monkeypatch):
    # Advisory/operator-scoped: missing key is at most a WARN, never FAIL.
    t = managed.resolve("agentpantry")
    monkeypatch.setattr(managed.proc, "which", lambda c: "/x/" + c)

    def fake_run(args, **kw):
        return managed.proc.Result(
            code=0,
            stdout='{"role": "sink", "configured": true, "peer": "0.0.0.0:8787",'
                   ' "key_present": false, "surfaces": ["sidecar"], "browsers": 0,'
                   ' "allow": [], "deny": []}',
            stderr="",
        )

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert all(status != "FAIL" for status, _, _ in results)
    assert any(status == "WARN" for status, _, _ in results)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/repos/brigade && python -m pytest tests/test_managed.py -q`
Expected: FAIL (`managed.resolve("agentpantry")` returns `None`).

- [ ] **Step 3: Add the doctor adapter**

In `src/brigade/managed.py`, after `_bootstrap_doctor_doctor` (keep it grouped with the other operator-scoped memory adapters' style), add:

```python
# agentpantry keeps the agent's machine authenticated by syncing browser sessions
# from a daily-driver (source) to the agent host (sink). Like the memory satellites
# it inspects host-global state, so its findings are advisory and never FAIL a
# workspace doctor run.
def _agentpantry_doctor(ctx: DoctorContext) -> List[CheckResult]:
    name = "agentpantry (session auth sync)"
    r = proc.run(["agentpantry", "status", "--json"])
    if r.code == 2:
        return [(WARN, name, "installed but unwired (no config)")]
    data = r.json()
    if data is None:
        return [(WARN, name, f"unexpected output (exit {r.code})")]
    role = data.get("role") or "?"
    peer = data.get("peer") or "?"
    surfaces = data.get("surfaces") or []
    key_present = bool(data.get("key_present"))
    status = OK if key_present else WARN
    detail = (
        f"role={role}, peer={peer}, "
        f"surfaces={','.join(surfaces) or 'none'}, "
        f"key={'present' if key_present else 'MISSING'}"
    )
    return [(status, name, detail)]
```

- [ ] **Step 4: Register the tool and fix stale URLs**

In `src/brigade/managed.py`, inside the `_TOOLS` tuple, fix the two stale Python-satellite URLs and append the agentpantry entry:

```python
    ManagedTool(
        name="memory-doctor", station="memory", command="memory-doctor",
        summary="memory index health, dead-link lint, handoff counts",
        install_args=["pipx", "install", "git+https://github.com/escoffier-labs/memory-doctor"],
        wire=_noop_wire, doctor=_memory_doctor_doctor,
    ),
    ManagedTool(
        name="bootstrap-doctor", station="memory", command="bootstrap-doctor",
        summary="bootstrap-file size/limit audit",
        install_args=["pipx", "install", "git+https://github.com/escoffier-labs/bootstrap-doctor"],
        wire=_noop_wire, doctor=_bootstrap_doctor_doctor,
    ),
```

Append after the `tokenjuice` entry, before the closing `)`:

```python
    ManagedTool(
        name="agentpantry", station="pantry", command="agentpantry",
        summary="browser session auth sync (source -> sink)",
        install_args=["go", "install", "github.com/escoffier-labs/agentpantry/cmd/agentpantry@latest"],
        wire=_noop_wire, doctor=_agentpantry_doctor,
    ),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/repos/brigade && python -m pytest tests/test_managed.py -q`
Expected: PASS.

- [ ] **Step 6: Run the full Brigade suite**

Run: `cd ~/repos/brigade && python -m pytest -q`
Expected: PASS (no regressions; in particular `test_registry.py` and any doctor tests).

- [ ] **Step 7: Commit**

```bash
cd ~/repos/brigade
git add src/brigade/managed.py tests/test_managed.py
git commit -m "feat(pantry): register agentpantry managed tool; fix satellite install URLs"
```

---

## Task 5: Brigade docs

**Repo:** `~/repos/brigade`

**Files:** `CHANGELOG.md`, `README.md`, `ROADMAP.md`

- [ ] **Step 1: CHANGELOG entry**

In `CHANGELOG.md` under `## [Unreleased]` -> `### Added`, add:

```markdown
- New `pantry` station (alias `larder`) and the `agentpantry` managed tool. `brigade add pantry` installs agentpantry via `go install`, and `brigade doctor`/`brigade status` health-check it by shelling out to `agentpantry status --json`. Like the memory satellites, agentpantry inspects host-global state, so its checks are advisory and never FAIL a workspace run: an unwired install (no config) is a `WARN`, a missing pre-shared key is a `WARN`, otherwise `OK`.
```

Under `### Fixed`, add:

```markdown
- Corrected the stale `github.com/solomonneas/...` install URLs for the `memory-doctor` and `bootstrap-doctor` managed tools to their actual `escoffier-labs` org, so `brigade add memory` installs from the right repos.
```

- [ ] **Step 2: README station table**

Find the station list/table in `README.md` (search for `tokens` or `security` alongside `garde`/`pass`). Add a `pantry` row matching the existing column format, e.g.:

```markdown
| `pantry` | `larder` | agent session auth sync | `agentpantry` |
```

Match the actual column count/order of the existing table; if the README lists stations as prose or bullets instead, add `pantry` in that same style.

- [ ] **Step 3: ROADMAP (only if it enumerates stations)**

Run: `cd ~/repos/brigade && grep -n "station" ROADMAP.md | head`
If a station enumeration exists, add `pantry`. If ROADMAP does not list stations explicitly, make no change.

- [ ] **Step 4: Verify docs build/lint if applicable, then commit**

Run: `cd ~/repos/brigade && git diff --stat`
Expected: only `CHANGELOG.md`, `README.md`, and possibly `ROADMAP.md` changed.

```bash
cd ~/repos/brigade
git add CHANGELOG.md README.md ROADMAP.md
git commit -m "docs(pantry): document pantry station and agentpantry tool"
```

---

## Task 6: End-to-end smoke test

**Repos:** both. This validates the live shell-out contract (not just the monkeypatched unit tests).

- [ ] **Step 1: Build and install agentpantry locally onto PATH**

Run:
```bash
cd ~/repos/agentpantry
go build -o ~/.local/bin/agentpantry ./cmd/agentpantry
which agentpantry && agentpantry status --json --config /tmp/does-not-exist.toml; echo "exit=$?"
```
Expected: `which` resolves the binary; the status call prints `unwired: no config ...` to stderr and reports `exit=2`.

- [ ] **Step 2: Confirm Brigade detects and health-checks it**

Run:
```bash
cd ~/repos/brigade
python -m brigade doctor 2>&1 | grep -iE "agentpantry|pantry"
```
Expected: a line for the `pantry` station / agentpantry, reporting `unwired` (no config yet) as a WARN, not a FAIL, and the overall doctor run does not hard-fail on account of it.

- [ ] **Step 3: Confirm `brigade add pantry` resolves the tool**

Run:
```bash
cd ~/repos/brigade
python -m brigade add pantry --help 2>&1 | head; \
python -c "from brigade import managed; t=managed.resolve('agentpantry'); print(t.station, t.command, t.install_args)"
```
Expected: prints `pantry agentpantry ['go', 'install', 'github.com/escoffier-labs/agentpantry/cmd/agentpantry@latest']`. (Do not run the real `brigade add pantry` install here unless the escoffier-labs repo+tag exist; the unit tests and the resolve check already cover wiring.)

- [ ] **Step 4: Final full suite**

Run: `cd ~/repos/brigade && python -m pytest -q && cd ~/repos/agentpantry && go test ./...`
Expected: all pass.

- [ ] **Step 5: No commit needed** (smoke test only). If `~/.local/bin/agentpantry` should not persist, remove it: `rm ~/.local/bin/agentpantry`.

---

## Self-Review Notes

- **Spec coverage:** status --json + exit codes (Task 1), org move (Task 2), pantry station + minimal checks (Task 3), managed tool + advisory scope + URL fix (Task 4), docs (Task 5), live contract (Task 6). All spec sections mapped.
- **Cross-repo contract pinned:** the JSON shape in Task 1's payload matches the keys consumed by `_agentpantry_doctor` (Task 4) and asserted in `test_agentpantry_doctor_parses_status`.
- **Advisory scope:** `test_agentpantry_never_fails_workspace` enforces "never FAIL".
- **Known-stations test:** Task 4 Step 1 updates `test_tools_attach_to_known_stations` to include `pantry` (would otherwise break).
