package app

import (
	"bufio"
	"bytes"
	"fmt"
	"io"
	"os/exec"
	"strings"

	"github.com/openclaw/logspine/internal/adapter"
	"github.com/openclaw/logspine/internal/ingest"
)

func cmdImportSourceHarvest(args []string, out, errw io.Writer) int {
	if len(args) < 2 {
		return fatalf(errw, "usage: spine import sourceharvest <jsonl|markdown|files|html|gitlog|json> <sourceharvest-args> [--json] [--dry-run]")
	}
	asJSON, dryRun, passArgs := splitWrapperFlags(args)
	if !hasFlag(passArgs, "out") {
		passArgs = append(passArgs, "--out", "-")
	}
	if dryRun {
		records, warnings, err := dryRunSourceHarvest(passArgs)
		if err != nil {
			return fatalf(errw, "import sourceharvest: %s", err)
		}
		result := map[string]any{"dry_run": true, "generated_records": records, "warnings": warnings}
		if asJSON {
			writeJSON(out, result)
		} else {
			fmt.Fprintf(out, "generated=%d warnings=%d\n", records, len(warnings))
		}
		return 0
	}
	db, _, err := openMigrated()
	if err != nil {
		return fatalf(errw, "import sourceharvest: %s", err)
	}
	defer db.Close()
	cmd := exec.Command("sourceharvest", passArgs...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fatalf(errw, "import sourceharvest: %s", err)
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return fatalf(errw, "import sourceharvest: %s", err)
	}
	result, importErr := ingest.ImportAdapterReader(db, stdout, "sourceharvest://"+strings.Join(passArgs, " "), "")
	waitErr := cmd.Wait()
	if importErr != nil {
		return fatalf(errw, "import sourceharvest: %s", importErr)
	}
	if waitErr != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = waitErr.Error()
		}
		return fatalf(errw, "import sourceharvest: %s", msg)
	}
	if asJSON {
		writeJSON(out, result)
	} else {
		fmt.Fprintf(out, "imported=%d warnings=%d already_known=%v source=%s\n", result.Inserted, len(result.Warnings), result.AlreadyKnown, result.SourceKind)
	}
	return 0
}

func dryRunSourceHarvest(args []string) (int, []string, error) {
	cmd := exec.Command("sourceharvest", args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return 0, nil, err
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return 0, nil, err
	}
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 0, 64*1024), 10*1024*1024)
	records := 0
	warnings := []string{}
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(bytes.TrimSpace(line)) == 0 {
			continue
		}
		if _, err := adapter.Parse(line); err != nil {
			warnings = append(warnings, err.Error())
			continue
		}
		records++
	}
	if err := scanner.Err(); err != nil {
		return 0, nil, err
	}
	if err := cmd.Wait(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		return 0, nil, fmt.Errorf("%s", msg)
	}
	return records, warnings, nil
}

func splitWrapperFlags(args []string) (bool, bool, []string) {
	asJSON := false
	dryRun := false
	pass := make([]string, 0, len(args))
	for _, arg := range args {
		switch arg {
		case "--json":
			asJSON = true
		case "--dry-run":
			dryRun = true
		default:
			pass = append(pass, arg)
		}
	}
	return asJSON, dryRun, pass
}

func hasFlag(args []string, name string) bool {
	long := "--" + name
	prefix := long + "="
	for _, arg := range args {
		if arg == long || strings.HasPrefix(arg, prefix) {
			return true
		}
	}
	return false
}
