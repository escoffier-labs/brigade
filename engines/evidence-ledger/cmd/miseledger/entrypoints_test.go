package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"
)

// Exercise compiled entry points so argument slicing, streams, and os.Exit
// remain covered independently of the internal app's in-process tests.
func TestEvidenceExecutableCompatibility(t *testing.T) {
	bins := make([]string, 0, 2)
	for _, name := range []string{"miseledger", "brigade-evidence"} {
		filename := name
		if runtime.GOOS == "windows" {
			filename += ".exe"
		}
		bin := filepath.Join(t.TempDir(), filename)
		cmd := exec.Command("go", "build", "-o", bin, "./cmd/"+name)
		cmd.Dir = filepath.Join("..", "..")
		if output, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("build %s: %v\n%s", name, err, output)
		}
		bins = append(bins, bin)
	}

	t.Run("arguments-and-streams", func(t *testing.T) {
		env, _ := executableHome(t, false)
		for _, tc := range []struct {
			name string
			args []string
			code int
			out  string
			err  string
		}{
			{"no-args", nil, 0, "miseledger ", ""},
			{"help", []string{"--help"}, 0, "miseledger ", ""},
			{"short-help", []string{"-h"}, 0, "miseledger ", ""},
			{"help-command", []string{"help"}, 0, "miseledger ", ""},
			{"nested-help", []string{"search", "--help"}, 0, "usage: miseledger search", ""},
			{"version", []string{"version"}, 0, "miseledger 0.6.0\n", ""},
			{"unknown-with-spaces", []string{"missing command", "extra"}, 1, "", "unknown command: missing command\n"},
			{"extra-argument", []string{"status", "unexpected argument"}, 1, "", "usage: miseledger status [--json]\n"},
		} {
			t.Run(tc.name, func(t *testing.T) {
				legacy := runExecutable(t, bins[0], env, tc.args...)
				canonical := runExecutable(t, bins[1], env, tc.args...)
				if canonical != legacy {
					t.Fatalf("canonical = %#v, legacy = %#v", canonical, legacy)
				}
				if legacy.code != tc.code || !strings.HasPrefix(legacy.out, tc.out) || legacy.err != tc.err {
					t.Fatalf("result = %#v, want exit %d stdout prefix %q stderr %q", legacy, tc.code, tc.out, tc.err)
				}
			})
		}
	})

	for _, configured := range []bool{false, true} {
		name := "default-storage"
		if configured {
			name = "configured-xdg-storage"
		}
		t.Run(name, func(t *testing.T) {
			env, roots := executableHome(t, configured)
			want := map[string]string{
				"config_path": filepath.Join(roots[0], "miseledger", "config.toml"),
				"data_dir":    filepath.Join(roots[1], "miseledger"),
				"db_path":     filepath.Join(roots[1], "miseledger", "miseledger.db"),
				"cache_dir":   filepath.Join(roots[2], "miseledger"),
			}
			// Existing config must survive either executable's initialization.
			if err := os.MkdirAll(filepath.Dir(want["config_path"]), 0700); err != nil {
				t.Fatal(err)
			}
			config := []byte("# existing configuration\n")
			if err := os.WriteFile(want["config_path"], config, 0600); err != nil {
				t.Fatal(err)
			}
			// Reverse the order as well: either name can create the shared archive.
			ordered := bins
			if configured {
				ordered = []string{bins[1], bins[0]}
			}
			for _, bin := range ordered {
				result := runExecutable(t, bin, env, "init")
				if result.code != 0 || result.err != "" {
					t.Fatalf("init: %#v", result)
				}
				var payload struct {
					OK    bool              `json:"ok"`
					Paths map[string]string `json:"paths"`
				}
				if err := json.Unmarshal([]byte(result.out), &payload); err != nil {
					t.Fatal(err)
				}
				if !payload.OK || !reflect.DeepEqual(payload.Paths, want) {
					t.Fatalf("init = %#v, want paths %#v", payload, want)
				}
			}
			legacy := runExecutable(t, bins[0], env, "status", "--json")
			canonical := runExecutable(t, bins[1], env, "status", "--json")
			if legacy.code != 0 || legacy.err != "" || canonical != legacy {
				t.Fatalf("shared archive status: %#v / %#v", legacy, canonical)
			}
			if _, err := os.Stat(want["db_path"]); err != nil {
				t.Fatal(err)
			}
			if got, err := os.ReadFile(want["config_path"]); err != nil || !bytes.Equal(got, config) {
				t.Fatalf("config changed: %q, %v", got, err)
			}
			for _, root := range roots {
				if _, err := os.Stat(filepath.Join(root, "brigade-evidence")); !errors.Is(err, os.ErrNotExist) {
					t.Fatalf("unexpected canonical storage at %s: %v", root, err)
				}
			}
		})
	}
}

type executableResult struct {
	code     int
	out, err string
}

func runExecutable(t *testing.T, bin string, env []string, args ...string) executableResult {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Env = env
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	code := 0
	if err := cmd.Run(); err != nil {
		var exit *exec.ExitError
		if !errors.As(err, &exit) || ctx.Err() != nil {
			t.Fatalf("run %s %q: %v", bin, args, err)
		}
		code = exit.ExitCode()
	}
	return executableResult{code, out.String(), errb.String()}
}

func executableHome(t *testing.T, configured bool) ([]string, []string) {
	t.Helper()
	home := t.TempDir()
	values := map[string]string{"HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "", "XDG_CACHE_HOME": ""}
	roots := []string{filepath.Join(home, ".config"), filepath.Join(home, ".local", "share"), filepath.Join(home, ".cache")}
	if configured {
		for i, key := range []string{"XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"} {
			roots[i] = filepath.Join(home, key)
			values[key] = roots[i]
		}
	}
	env := []string{}
	for _, entry := range os.Environ() {
		key, _, _ := strings.Cut(entry, "=")
		if _, replaced := values[key]; !replaced {
			env = append(env, entry)
		}
	}
	for key, value := range values {
		env = append(env, key+"="+value)
	}
	return env, roots
}
