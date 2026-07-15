package app

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func TestScheduleRunExecutesConfiguredCrawlerJobs(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	config := writeScheduleConfig(t, fmt.Sprintf(`
interval = "1h"

[[jobs]]
name = "discord-fixture"
command = "import"
args = ["adapter", %q, "--source", "discrawl", "--json"]
`, repoPath(t, "testdata/adapters/discrawl.fixture.jsonl")))

	got := runJSON(t, "schedule", "run", config, "--json")
	if got["failed_jobs"].(float64) != 0 {
		t.Fatalf("schedule run failed jobs: %v", got)
	}
	if got["successful_jobs"].(float64) != 1 {
		t.Fatalf("schedule successful_jobs = %v, want 1", got["successful_jobs"])
	}
	jobs := got["jobs"].([]any)
	if len(jobs) != 1 {
		t.Fatalf("jobs len = %d, want 1: %v", len(jobs), got)
	}
	job := jobs[0].(map[string]any)
	if job["name"] != "discord-fixture" || job["exit_code"].(float64) != 0 {
		t.Fatalf("unexpected job result: %v", job)
	}
	status := runJSON(t, "status", "--json")
	if status["items"].(float64) != 2 {
		t.Fatalf("scheduled import inserted items = %v, want 2", status["items"])
	}
}

func TestScheduleRunContinuesAfterFailedJob(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	config := writeScheduleConfig(t, fmt.Sprintf(`
[[jobs]]
name = "missing-command"
command = "definitely-not-a-command"
args = []

[[jobs]]
name = "discord-fixture"
command = "import"
args = ["adapter", %q, "--source", "discrawl", "--json"]
`, repoPath(t, "testdata/adapters/discrawl.fixture.jsonl")))

	code, out, errb := run("schedule", "run", config, "--json")
	if code == 0 {
		t.Fatalf("schedule with failed job succeeded: out=%s", out)
	}
	if errb != "" {
		t.Fatalf("schedule should report per-job errors in json, stderr = %q", errb)
	}
	got := parseJSONMap(t, out)
	if got["failed_jobs"].(float64) != 1 {
		t.Fatalf("failed_jobs = %v, want 1: %v", got["failed_jobs"], got)
	}
	if got["successful_jobs"].(float64) != 1 {
		t.Fatalf("successful_jobs = %v, want 1: %v", got["successful_jobs"], got)
	}
	status := runJSON(t, "status", "--json")
	if status["items"].(float64) != 2 {
		t.Fatalf("successful job after failure did not run: %v", status)
	}
}

func TestScheduleDaemonHonorsMaxRuns(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	config := writeScheduleConfig(t, fmt.Sprintf(`
interval = "1ms"

[[jobs]]
name = "discord-fixture"
command = "import"
args = ["adapter", %q, "--source", "discrawl", "--json"]
`, repoPath(t, "testdata/adapters/discrawl.fixture.jsonl")))

	got := runJSON(t, "schedule", "daemon", config, "--interval", "1ms", "--max-runs", "2", "--json")
	if got["runs"].(float64) != 2 {
		t.Fatalf("daemon runs = %v, want 2: %v", got["runs"], got)
	}
	if got["failed_runs"].(float64) != 0 {
		t.Fatalf("daemon failed_runs = %v, want 0: %v", got["failed_runs"], got)
	}
	status := runJSON(t, "status", "--json")
	if status["items"].(float64) != 2 {
		t.Fatalf("scheduled daemon should be idempotent, items = %v, want 2", status["items"])
	}
}

func writeScheduleConfig(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "miseledger-schedule.toml")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func parseJSONMap(t *testing.T, body string) map[string]any {
	t.Helper()
	var got map[string]any
	if err := json.Unmarshal([]byte(body), &got); err != nil {
		t.Fatalf("invalid json: %v\n%s", err, body)
	}
	return got
}
