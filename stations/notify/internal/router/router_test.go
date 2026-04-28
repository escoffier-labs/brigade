package router

import (
	"reflect"
	"sort"
	"testing"

	"github.com/solomonneas/agent-notify/internal/config"
)

func sortedNames(ns []string) []string {
	out := append([]string(nil), ns...)
	sort.Strings(out)
	return out
}

func baseConfig() *config.Config {
	return &config.Config{
		Channels: map[string]config.ChannelConfig{
			"tg":     {Type: "telegram"},
			"disc":   {Type: "discord"},
			"signal": {Type: "signal"},
		},
		Profiles: map[string]config.ProfileConfig{
			"agent-stop": {Channels: []string{"tg", "disc"}, Default: true},
			"error":      {Channels: []string{"tg", "disc", "signal"}},
		},
	}
}

func TestResolve_ExplicitToWins(t *testing.T) {
	got, err := Resolve(baseConfig(), "tg,signal", "agent-stop", "")
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"signal", "tg"}
	if !reflect.DeepEqual(sortedNames(got), want) {
		t.Errorf("got %v, want %v", sortedNames(got), want)
	}
}

func TestResolve_ProfileSelectsChannels(t *testing.T) {
	got, err := Resolve(baseConfig(), "", "error", "")
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"disc", "signal", "tg"}
	if !reflect.DeepEqual(sortedNames(got), want) {
		t.Errorf("got %v, want %v", sortedNames(got), want)
	}
}

func TestResolve_DefaultProfileUsedWhenNoFlags(t *testing.T) {
	got, err := Resolve(baseConfig(), "", "", "")
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"disc", "tg"}
	if !reflect.DeepEqual(sortedNames(got), want) {
		t.Errorf("got %v, want %v", sortedNames(got), want)
	}
}

func TestResolve_FallbackToAllWhenNoDefault(t *testing.T) {
	cfg := baseConfig()
	p := cfg.Profiles["agent-stop"]
	p.Default = false
	cfg.Profiles["agent-stop"] = p

	got, err := Resolve(cfg, "", "", "")
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"disc", "signal", "tg"}
	if !reflect.DeepEqual(sortedNames(got), want) {
		t.Errorf("got %v, want %v", sortedNames(got), want)
	}
}

func TestResolve_SkipFiltersResolvedList(t *testing.T) {
	got, err := Resolve(baseConfig(), "", "error", "signal")
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"disc", "tg"}
	if !reflect.DeepEqual(sortedNames(got), want) {
		t.Errorf("got %v, want %v", sortedNames(got), want)
	}
}

func TestResolve_UnknownChannelInToErrors(t *testing.T) {
	_, err := Resolve(baseConfig(), "nope", "", "")
	if err == nil {
		t.Fatal("expected error for unknown channel")
	}
}

func TestResolve_UnknownProfileErrors(t *testing.T) {
	_, err := Resolve(baseConfig(), "", "missing-profile", "")
	if err == nil {
		t.Fatal("expected error for unknown profile")
	}
}
