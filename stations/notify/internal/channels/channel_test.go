package channels

import (
	"context"
	"testing"

	"github.com/escoffier-labs/agent-notify/internal/canonical"
)

type fakeChannel struct {
	sent []canonical.Message
	err  error
}

func (f *fakeChannel) Name() string { return "fake" }
func (f *fakeChannel) Type() string { return "fake" }
func (f *fakeChannel) Send(_ context.Context, m canonical.Message) error {
	if f.err != nil {
		return f.err
	}
	f.sent = append(f.sent, m)
	return nil
}

func TestRegistryRegisterAndGet(t *testing.T) {
	r := NewRegistry()
	fc := &fakeChannel{}
	r.Register("a", fc)
	got, ok := r.Get("a")
	if !ok {
		t.Fatal("expected channel registered")
	}
	if got.Name() != "fake" {
		t.Errorf("expected name=fake, got %s", got.Name())
	}
}

func TestRegistryGet_MissingReturnsFalse(t *testing.T) {
	r := NewRegistry()
	if _, ok := r.Get("missing"); ok {
		t.Fatal("expected ok=false for missing channel")
	}
}

func TestRegistryAll_ReturnsRegisteredNames(t *testing.T) {
	r := NewRegistry()
	r.Register("a", &fakeChannel{})
	r.Register("b", &fakeChannel{})
	names := r.AllNames()
	if len(names) != 2 {
		t.Fatalf("expected 2 names, got %d", len(names))
	}
}
