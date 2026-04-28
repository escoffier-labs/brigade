package adapter

import (
	"strings"
	"testing"
)

func TestAutoDetect_PlainStringIsBody(t *testing.T) {
	m, err := AutoDetect(strings.NewReader("just a message"))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Body != "just a message" {
		t.Errorf("body = %q, want 'just a message'", m.Body)
	}
}

func TestAutoDetect_CanonicalJSONParsesAllFields(t *testing.T) {
	in := `{"title":"T","body":"B","level":"warn","source":"s","tags":["x","y"]}`
	m, err := AutoDetect(strings.NewReader(in))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Title != "T" || m.Body != "B" || m.Level != "warn" || m.Source != "s" {
		t.Errorf("fields wrong: %+v", m)
	}
	if len(m.Tags) != 2 {
		t.Errorf("tags = %v, want 2", m.Tags)
	}
}

func TestAutoDetect_NonCanonicalJSON_ReturnsError(t *testing.T) {
	// Looks like JSON but not canonical (no body field).
	in := `{"foo":"bar","baz":42}`
	_, err := AutoDetect(strings.NewReader(in))
	if err == nil {
		t.Fatal("expected error for non-canonical JSON, got nil")
	}
}

func TestAutoDetect_EmptyInputErrors(t *testing.T) {
	_, err := AutoDetect(strings.NewReader(""))
	if err == nil {
		t.Fatal("expected error for empty input, got nil")
	}
}

func TestFromString_BuildsCanonical(t *testing.T) {
	m := FromString("hello")
	if m.Body != "hello" {
		t.Errorf("body = %q, want hello", m.Body)
	}
}
