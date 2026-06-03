// Package router resolves a final list of channel names from explicit flags,
// profile selection, config defaults, and the universe of configured channels.
package router

import (
	"fmt"
	"sort"
	"strings"

	"github.com/solomonneas/agent-notify/internal/config"
)

// Resolve returns the channel names to send to, applying precedence:
//  1. explicitTo (--to flag, comma-separated)
//  2. profile (--profile flag)
//  3. profile in cfg with Default=true
//  4. all configured channels
//
// Then skip (--skip flag, comma-separated) is removed from whichever wins.
func Resolve(cfg *config.Config, explicitTo, profile, skip string) ([]string, error) {
	var resolved []string

	switch {
	case explicitTo != "":
		names := splitCSV(explicitTo)
		for _, n := range names {
			if _, ok := cfg.Channels[n]; !ok {
				return nil, fmt.Errorf("--to references unknown channel %q", n)
			}
		}
		resolved = names

	case profile != "":
		p, ok := cfg.Profiles[profile]
		if !ok {
			return nil, fmt.Errorf("--profile %q not found in config", profile)
		}
		for _, n := range p.Channels {
			if _, ok := cfg.Channels[n]; !ok {
				return nil, fmt.Errorf("--profile %q references unknown channel %q", profile, n)
			}
		}
		resolved = append(resolved, p.Channels...)

	default:
		// Look for a default profile.
		for _, profileName := range sortedProfileNames(cfg) {
			p := cfg.Profiles[profileName]
			if p.Default {
				for _, n := range p.Channels {
					if _, ok := cfg.Channels[n]; !ok {
						return nil, fmt.Errorf("default profile %q references unknown channel %q", profileName, n)
					}
				}
				resolved = append(resolved, p.Channels...)
				break
			}
		}
		// Fallback to all channels.
		if len(resolved) == 0 {
			resolved = sortedChannelNames(cfg)
		}
	}

	if skip != "" {
		skipSet := make(map[string]struct{})
		for _, n := range splitCSV(skip) {
			skipSet[n] = struct{}{}
		}
		filtered := resolved[:0]
		for _, n := range resolved {
			if _, drop := skipSet[n]; !drop {
				filtered = append(filtered, n)
			}
		}
		resolved = filtered
	}

	return resolved, nil
}

func sortedProfileNames(cfg *config.Config) []string {
	names := make([]string, 0, len(cfg.Profiles))
	for n := range cfg.Profiles {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

func sortedChannelNames(cfg *config.Config) []string {
	names := make([]string, 0, len(cfg.Channels))
	for n := range cfg.Channels {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

func splitCSV(s string) []string {
	parts := strings.Split(s, ",")
	out := parts[:0]
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}
