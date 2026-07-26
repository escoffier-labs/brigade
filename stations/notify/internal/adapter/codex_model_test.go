package adapter

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

// writeCodexRollout creates a Codex CLI rollout JSONL file under
// CODEX_HOME/sessions/2026/07/25/rollout-<timestamp>-<thread>.jsonl and writes
// the given raw lines (one JSON record per line). It returns the file path.
func writeCodexRollout(t *testing.T, codexHome, thread, timestamp string, lines []string) string {
	t.Helper()
	dir := filepath.Join(codexHome, "sessions", "2026", "07", "25")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir rollout dir: %v", err)
	}
	path := filepath.Join(dir, fmt.Sprintf("rollout-%s-%s.jsonl", timestamp, thread))
	f, err := os.Create(path)
	if err != nil {
		t.Fatalf("create rollout: %v", err)
	}
	defer f.Close()
	w := bufio.NewWriter(f)
	for _, l := range lines {
		if _, err := w.WriteString(l + "\n"); err != nil {
			t.Fatalf("write rollout line: %v", err)
		}
	}
	if err := w.Flush(); err != nil {
		t.Fatalf("flush rollout: %v", err)
	}
	return path
}

// turnContextLine returns a JSONL record for a Codex turn_context entry with
// the given turn_id and model.
func turnContextLine(turnID, model string) string {
	rec := map[string]interface{}{
		"type": "turn_context",
		"payload": map[string]interface{}{
			"turn_id": turnID,
			"model":   model,
		},
	}
	b, err := json.Marshal(rec)
	if err != nil {
		panic(err)
	}
	return string(b)
}

func TestResolveCodexModel_MatchesExactTurn(t *testing.T) {
	codexHome := t.TempDir()
	writeCodexRollout(t, codexHome, "abc", "20260725-180000", []string{
		turnContextLine("turn-old", "gpt-5.5"),
		turnContextLine("turn-7", "gpt-5.6-sol"),
	})
	t.Setenv("CODEX_HOME", codexHome)
	got := resolveCodexModel("abc", "turn-7")
	if got != "gpt-5.6-sol" {
		t.Errorf("resolveCodexModel = %q, want gpt-5.6-sol", got)
	}
}

func TestResolveCodexModel_DoesNotUseAnotherTurn(t *testing.T) {
	codexHome := t.TempDir()
	writeCodexRollout(t, codexHome, "abc", "20260725-180000", []string{
		turnContextLine("turn-old", "gpt-5.5"),
	})
	t.Setenv("CODEX_HOME", codexHome)
	got := resolveCodexModel("abc", "turn-7")
	if got != "" {
		t.Errorf("resolveCodexModel = %q, want empty (must not fall back to another turn)", got)
	}
}

func TestResolveCodexModel_IgnoresBadIdentifiersAndMalformedRecords(t *testing.T) {
	t.Run("malformed JSON skipped", func(t *testing.T) {
		codexHome := t.TempDir()
		writeCodexRollout(t, codexHome, "abc", "20260725-180000", []string{
			"{not valid json",
			turnContextLine("turn-7", "gpt-5.6-sol"),
		})
		t.Setenv("CODEX_HOME", codexHome)
		got := resolveCodexModel("abc", "turn-7")
		if got != "gpt-5.6-sol" {
			t.Errorf("resolveCodexModel = %q, want gpt-5.6-sol after skipping malformed line", got)
		}
	})

	t.Run("path traversal thread rejected", func(t *testing.T) {
		codexHome := t.TempDir()
		writeCodexRollout(t, codexHome, "abc", "20260725-180000", []string{
			turnContextLine("turn-7", "gpt-5.6-sol"),
		})
		t.Setenv("CODEX_HOME", codexHome)
		got := resolveCodexModel("../abc", "turn-7")
		if got != "" {
			t.Errorf("resolveCodexModel = %q for ../thread, want empty", got)
		}
	})

	t.Run("empty turn rejected", func(t *testing.T) {
		codexHome := t.TempDir()
		writeCodexRollout(t, codexHome, "abc", "20260725-180000", []string{
			turnContextLine("turn-7", "gpt-5.6-sol"),
		})
		t.Setenv("CODEX_HOME", codexHome)
		got := resolveCodexModel("abc", "")
		if got != "" {
			t.Errorf("resolveCodexModel = %q for empty turn, want empty", got)
		}
	})
}

func TestModelFromCodexRollout_UnreadablePathIsEmpty(t *testing.T) {
	dir := t.TempDir()
	// Passing a directory: os.Open succeeds but reading fails; must return empty.
	got := modelFromCodexRollout(dir, "turn-7")
	if got != "" {
		t.Errorf("modelFromCodexRollout(dir) = %q, want empty", got)
	}
}

func TestCodexNotify_ResolvesModelFromSession(t *testing.T) {
	codexHome := t.TempDir()
	writeCodexRollout(t, codexHome, "abc", "20260725-180000", []string{
		turnContextLine("turn-old", "gpt-5.5"),
		turnContextLine("turn-7", "gpt-5.6-sol"),
	})
	t.Setenv("CODEX_HOME", codexHome)
	in := []byte(`{"type":"agent-turn-complete","thread-id":"abc","turn-id":"turn-7","last-assistant-message":"Built OK."}`)
	m, err := CodexNotifyFromBytes(in)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Model != "gpt-5.6-sol" {
		t.Errorf("model = %q, want gpt-5.6-sol resolved from session rollout", m.Model)
	}
}
