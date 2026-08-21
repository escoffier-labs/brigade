package ingest

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"

	moderncsqlite "modernc.org/sqlite"
)

// sqliteBusyPrimary is SQLITE_BUSY. Extended codes keep this in the low 8 bits:
// SQLITE_BUSY_RECOVERY=261, SQLITE_BUSY_SNAPSHOT=517, SQLITE_BUSY_TIMEOUT=773.
const sqliteBusyPrimary = 5

const (
	sqliteBusy         = 5
	sqliteBusyRecovery = 261
	sqliteBusySnapshot = 517
	sqliteBusyTimeout  = 773
)

// DefaultBusyRetries is the number of attempts (initial try plus retries)
// around an import or backfill that hits a BUSY-family lock.
const DefaultBusyRetries = 4

// DefaultBusyBackoff is the first backoff after a retryable lock. Later
// waits double, capped at DefaultBusyBackoffMax.
const DefaultBusyBackoff = 100 * time.Millisecond

// DefaultBusyBackoffMax caps the per-attempt sleep so a stuck lock fails
// in bounded time instead of stretching the daily crawl indefinitely.
const DefaultBusyBackoffMax = 400 * time.Millisecond

// DefaultBusyTotalWait is the wall-clock ceiling for RetryOnBusy, including
// per-attempt function time. busy_timeout and retries must compose to a
// few seconds, never busy_timeout * retries into minutes.
const DefaultBusyTotalWait = 4 * time.Second

// HolderDiagnosisLabel is the token a bounded SQLITE_BUSY failure must
// name so a runbook receipt points at lock-holder diagnosis instead of
// the raw driver string.
const HolderDiagnosisLabel = "holder-diagnosis"

var busyCodeInMessage = regexp.MustCompile(`\((\d+)\)`)

// BusyRetryOptions configure RetryOnBusy. Zero values use the defaults
// above. Tests inject Sleep and a short Attempts count.
type BusyRetryOptions struct {
	Attempts     int
	InitialWait  time.Duration
	MaxWait      time.Duration
	MaxTotalWait time.Duration
	Sleep        func(time.Duration)
	Now          func() time.Time
	OnRetry      func(attempt, attempts int, wait time.Duration, err error)
	Diagnose     func() string
}

// IsBusy reports whether err is a retryable SQLite BUSY-family lock.
// The primary result code is SQLITE_BUSY (5); extended codes keep that
// value in the low 8 bits (261 recovery, 517 snapshot, 773 timeout).
func IsBusy(err error) bool {
	if err == nil {
		return false
	}
	if code, ok := sqliteErrorCode(err); ok {
		return isBusyFamilyCode(code)
	}
	if code, ok := busyCodeFromMessage(err.Error()); ok {
		return isBusyFamilyCode(code)
	}
	return strings.Contains(strings.ToLower(err.Error()), "sqlite_busy")
}

func isBusyFamilyCode(code int) bool {
	switch code {
	case sqliteBusy, sqliteBusyRecovery, sqliteBusySnapshot, sqliteBusyTimeout:
		return true
	default:
		return code&0xFF == sqliteBusyPrimary
	}
}

func sqliteErrorCode(err error) (int, bool) {
	var se *moderncsqlite.Error
	if errors.As(err, &se) {
		return se.Code(), true
	}
	return 0, false
}

func busyCodeFromMessage(msg string) (int, bool) {
	matches := busyCodeInMessage.FindAllStringSubmatch(msg, -1)
	for _, m := range matches {
		if len(m) < 2 {
			continue
		}
		code, err := strconv.Atoi(m[1])
		if err != nil {
			continue
		}
		if isBusyFamilyCode(code) {
			return code, true
		}
	}
	return 0, false
}

func busyRetryDefaults(opts BusyRetryOptions) BusyRetryOptions {
	if opts.Attempts <= 0 {
		opts.Attempts = DefaultBusyRetries
	}
	if opts.InitialWait <= 0 {
		opts.InitialWait = DefaultBusyBackoff
	}
	if opts.MaxWait <= 0 {
		opts.MaxWait = DefaultBusyBackoffMax
	}
	if opts.MaxTotalWait <= 0 {
		opts.MaxTotalWait = DefaultBusyTotalWait
	}
	if opts.Sleep == nil {
		opts.Sleep = time.Sleep
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	return opts
}

// RetryOnBusy runs fn, retrying when it returns a BUSY-family lock, with
// exponential backoff and a wall-clock ceiling. Exhaustion returns an
// error that names holder-diagnosis and omits the raw SQLITE_BUSY string.
//
// Crawl import and provenance backfill write the SQLite evidence archive,
// not the outcome JSONL digest chain. #566's records.jsonl.lock therefore
// does not serialize these paths; SQLITE_BUSY retry is the coordination
// with concurrent miseledger writers (receipt capture, handoff-ingest).
func RetryOnBusy(fn func() error, opts BusyRetryOptions) error {
	opts = busyRetryDefaults(opts)
	wait := opts.InitialWait
	started := opts.Now()
	var last error
	for attempt := 1; attempt <= opts.Attempts; attempt++ {
		last = fn()
		if last == nil || !IsBusy(last) {
			return last
		}
		elapsed := opts.Now().Sub(started)
		if attempt == opts.Attempts || elapsed >= opts.MaxTotalWait {
			break
		}
		if remain := opts.MaxTotalWait - elapsed; wait > remain {
			wait = remain
		}
		if wait <= 0 {
			break
		}
		if opts.OnRetry != nil {
			opts.OnRetry(attempt, opts.Attempts, wait, last)
		}
		opts.Sleep(wait)
		wait *= 2
		if wait > opts.MaxWait {
			wait = opts.MaxWait
		}
	}
	diag := HolderDiagnosisLabel + ": holder=unknown"
	if opts.Diagnose != nil {
		if text := strings.TrimSpace(opts.Diagnose()); text != "" {
			diag = text
		}
	}
	return fmt.Errorf("evidence database still locked after %d retries; %s", opts.Attempts, diag)
}

// DiagnoseLockHolder names the evidence database and any process that
// currently has it (or a WAL sidecar) open. Used when a bounded retry
// gives up so the failure names the holder-diagnosis step.
func DiagnoseLockHolder(dbPath string) string {
	abs := dbPath
	if dbPath != "" {
		if resolved, err := filepath.Abs(dbPath); err == nil {
			abs = resolved
		}
	}
	if abs == "" {
		return HolderDiagnosisLabel + ": db=unknown holder=unknown"
	}
	holders := lockHolders(abs)
	if len(holders) == 0 {
		return fmt.Sprintf("%s: db=%s holder=unknown", HolderDiagnosisLabel, abs)
	}
	return fmt.Sprintf("%s: db=%s %s", HolderDiagnosisLabel, abs, strings.Join(holders, "; "))
}

func lockHolders(dbPath string) []string {
	targets := map[string]bool{
		dbPath:          true,
		dbPath + "-wal": true,
		dbPath + "-shm": true,
	}
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return nil
	}
	seen := map[string]string{}
	for _, entry := range entries {
		name := entry.Name()
		if !isProcPID(name) {
			continue
		}
		fdDir := filepath.Join("/proc", name, "fd")
		fds, err := os.ReadDir(fdDir)
		if err != nil {
			continue
		}
		for _, fd := range fds {
			dest, err := os.Readlink(filepath.Join(fdDir, fd.Name()))
			if err != nil {
				continue
			}
			if !targets[dest] {
				continue
			}
			if _, ok := seen[name]; ok {
				continue
			}
			cmd := readProcCmdline(filepath.Join("/proc", name, "cmdline"))
			seen[name] = cmd
		}
	}
	if len(seen) == 0 {
		return nil
	}
	out := make([]string, 0, len(seen))
	for pid, cmd := range seen {
		if cmd == "" {
			out = append(out, "pid="+pid)
			continue
		}
		out = append(out, "pid="+pid+" cmd="+cmd)
	}
	return out
}

func isProcPID(name string) bool {
	if name == "" {
		return false
	}
	for _, r := range name {
		if !unicode.IsDigit(r) {
			return false
		}
	}
	return true
}

func readProcCmdline(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil || len(raw) == 0 {
		return ""
	}
	parts := strings.Split(string(raw), "\x00")
	cleaned := make([]string, 0, len(parts))
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		cleaned = append(cleaned, filepath.Base(part))
	}
	if len(cleaned) == 0 {
		return ""
	}
	text := strings.Join(cleaned, " ")
	if len(text) > 120 {
		return text[:120]
	}
	return text
}
