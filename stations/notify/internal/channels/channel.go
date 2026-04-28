// Package channels defines the Channel interface and a registry that maps
// channel names (from config) to their implementations.
package channels

import (
	"context"
	"sort"

	"github.com/solomonneas/agent-notify/internal/canonical"
)

// Channel is the contract every channel adapter implements.
type Channel interface {
	// Name returns the user-facing channel name (from config), e.g. "telegram-personal".
	Name() string
	// Type returns the channel type string, e.g. "discord", "telegram", "signal".
	Type() string
	// Send dispatches one message. Implementations must respect ctx cancellation.
	Send(ctx context.Context, m canonical.Message) error
}

// Registry maps channel names to Channel implementations.
type Registry struct {
	channels map[string]Channel
}

func NewRegistry() *Registry {
	return &Registry{channels: make(map[string]Channel)}
}

func (r *Registry) Register(name string, c Channel) {
	r.channels[name] = c
}

func (r *Registry) Get(name string) (Channel, bool) {
	c, ok := r.channels[name]
	return c, ok
}

func (r *Registry) AllNames() []string {
	names := make([]string, 0, len(r.channels))
	for n := range r.channels {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}
