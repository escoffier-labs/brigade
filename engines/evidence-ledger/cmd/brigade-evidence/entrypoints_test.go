package main

import (
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/escoffier-labs/brigade/engines/evidence-ledger/internal/app"
)

// Exercise compiled entry points so argument slicing, streams, and os.Exit
// remain covered independently of the internal app's in-process tests.
func TestEvidenceExecutable(t *testing.T) {
	filename := app.CanonicalProgram
	if runtime.GOOS == "windows" {
		filename += ".exe"
	}
	bin := filepath.Join(t.TempDir(), filename)
	cmd := exec.Command("go", "build", "-o", bin, "./cmd/brigade-evidence")
	cmd.Dir = filepath.Join("..", "..")
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build evidence: %v\n%s", err, output)
	}

	t.Run("arguments-and-streams", func(t *testing.T) {
		env := executableHome(t)
		for _, tc := range []struct {
			name string
			args []string
			code int
			out  string
			err  string
		}{
			{"no-args", nil, 0, "PROG version | ", ""},
			{"help", []string{"--help"}, 0, "PROG version | ", ""},
			{"short-help", []string{"-h"}, 0, "PROG version | ", ""},
			{"help-command", []string{"help"}, 0, "PROG version | ", ""},
			{"nested-help", []string{"search", "--help"}, 0, "usage: PROG search", ""},
			{"version", []string{"version"}, 0, "PROG 0.6.0\n", ""},
			{"unknown-with-spaces", []string{"missing command", "extra"}, 1, "", "unknown command: missing command\n"},
			{"extra-argument", []string{"status", "unexpected argument"}, 1, "", "usage: PROG status [--json]\n"},
		} {
			t.Run(tc.name, func(t *testing.T) {
				got := runExecutable(t, bin, env, tc.args...)
				wantOut := strings.ReplaceAll(tc.out, "PROG", app.CanonicalProgram)
				wantErr := strings.ReplaceAll(tc.err, "PROG", app.CanonicalProgram)
				stdoutMatches := got.out == wantOut || wantOut != "" && strings.HasPrefix(got.out, wantOut)
				if got.code != tc.code || !stdoutMatches || got.err != wantErr {
					t.Fatalf("result = %#v, want exit %d stdout prefix %q stderr %q", got, tc.code, wantOut, wantErr)
				}
			})
		}
	})

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

func executableHome(t *testing.T) []string {
	t.Helper()
	home := t.TempDir()
	values := map[string]string{"HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "", "XDG_CACHE_HOME": ""}
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
	return env
}
