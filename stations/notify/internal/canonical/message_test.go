package canonical

import (
	"testing"
)

func TestMessageValidate_RequiresBody(t *testing.T) {
	m := Message{Title: "no body"}
	err := m.Validate()
	if err == nil {
		t.Fatal("expected validation error for missing body, got nil")
	}
}

func TestMessageValidate_AcceptsValidLevels(t *testing.T) {
	for _, lvl := range []string{"info", "warn", "error", "success", ""} {
		m := Message{Body: "ok", Level: lvl}
		if err := m.Validate(); err != nil {
			t.Errorf("level %q should be valid: %v", lvl, err)
		}
	}
}

func TestMessageValidate_RejectsBadLevel(t *testing.T) {
	m := Message{Body: "ok", Level: "critical"}
	if err := m.Validate(); err == nil {
		t.Fatal("expected validation error for level=critical, got nil")
	}
}

func TestMessageValidate_DefaultsLevelToInfo(t *testing.T) {
	m := Message{Body: "ok"}
	if err := m.Validate(); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Level != "info" {
		t.Errorf("expected level=info, got %q", m.Level)
	}
}
