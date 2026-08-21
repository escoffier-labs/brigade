package ingest

import (
	"database/sql"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/escoffier-labs/miseledger/internal/archive"
	_ "modernc.org/sqlite"
)

func TestRetryOnBusyImportSucceedsAfterHeldLock(t *testing.T) {
	path := t.TempDir() + "/miseledger.db"
	setup, err := archive.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := archive.Migrate(setup); err != nil {
		t.Fatal(err)
	}
	if err := setup.Close(); err != nil {
		t.Fatal(err)
	}

	holder := openImmediate(t, path)
	defer holder.Close()
	if _, err := holder.Exec("BEGIN EXCLUSIVE"); err != nil {
		t.Fatal(err)
	}

	importer := openImmediate(t, path)
	defer importer.Close()

	var retries atomic.Int32
	released := make(chan struct{})
	opts := BusyRetryOptions{
		Attempts:    4,
		InitialWait: time.Millisecond,
		MaxWait:     5 * time.Millisecond,
		OnRetry: func(attempt, attempts int, wait time.Duration, err error) {
			if !IsBusy(err) {
				t.Errorf("OnRetry err = %v, want SQLITE_BUSY", err)
			}
			if retries.Add(1) == 1 {
				if _, err := holder.Exec("COMMIT"); err != nil {
					t.Errorf("release holder: %v", err)
				}
				close(released)
			}
		},
	}

	jsonl := `{"schema":"miseledger.adapter.v1","source":{"kind":"busy-retry","name":"Busy Retry"},"collection":{"external_id":"busy:collection","kind":"agent_session","name":"busy"},"item":{"external_id":"busy:item:1","kind":"message","created_at":"2026-06-03T00:00:00Z","text":"retry after held lock","tags":["busy"]},"actor":{"external_id":"busy:actor","type":"human","name":"busy"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":"busy.jsonl","ordinal":1}}` + "\n"
	err = RetryOnBusy(func() error {
		_, importErr := ImportAdapterReader(importer, strings.NewReader(jsonl), "busy://fixture", "busy-retry")
		return importErr
	}, opts)
	if err != nil {
		t.Fatalf("import after held lock: %v", err)
	}
	if retries.Load() == 0 {
		t.Fatal("expected at least one retry while the lock was held")
	}
	select {
	case <-released:
	default:
		_, _ = holder.Exec("COMMIT")
		t.Fatal("lock holder was never released via the retry path")
	}
	var items int
	if err := importer.QueryRow(`select count(*) from items`).Scan(&items); err != nil {
		t.Fatal(err)
	}
	if items != 1 {
		t.Fatalf("items = %d, want 1 after successful retry", items)
	}
}

func TestRetryOnBusyBoundedFailureNamesHolderDiagnosis(t *testing.T) {
	path := t.TempDir() + "/miseledger.db"
	setup, err := archive.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := archive.Migrate(setup); err != nil {
		t.Fatal(err)
	}
	if err := setup.Close(); err != nil {
		t.Fatal(err)
	}

	holder := openImmediate(t, path)
	defer holder.Close()
	if _, err := holder.Exec("BEGIN EXCLUSIVE"); err != nil {
		t.Fatal(err)
	}
	defer func() { _, _ = holder.Exec("ROLLBACK") }()

	importer := openImmediate(t, path)
	defer importer.Close()

	var sleeps int
	opts := BusyRetryOptions{
		Attempts:    3,
		InitialWait: time.Millisecond,
		MaxWait:     time.Millisecond,
		Sleep:       func(time.Duration) { sleeps++ },
		Diagnose:    func() string { return DiagnoseLockHolder(path) },
	}
	jsonl := `{"schema":"miseledger.adapter.v1","source":{"kind":"busy-fail","name":"Busy Fail"},"collection":{"external_id":"busy:collection","kind":"agent_session","name":"busy"},"item":{"external_id":"busy:item:fail","kind":"message","created_at":"2026-06-03T00:00:00Z","text":"bounded failure","tags":["busy"]},"actor":{"external_id":"busy:actor","type":"human","name":"busy"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":"busy.jsonl","ordinal":1}}` + "\n"
	err = RetryOnBusy(func() error {
		_, importErr := ImportAdapterReader(importer, strings.NewReader(jsonl), "busy://fail", "busy-fail")
		return importErr
	}, opts)
	if err == nil {
		t.Fatal("expected bounded lock failure")
	}
	msg := err.Error()
	if !strings.Contains(msg, HolderDiagnosisLabel) {
		t.Fatalf("bounded failure %q must name %s", msg, HolderDiagnosisLabel)
	}
	if strings.Contains(msg, "SQLITE_BUSY") {
		t.Fatalf("bounded failure %q must not emit the raw SQLITE_BUSY string", msg)
	}
	if !strings.Contains(msg, "db=") {
		t.Fatalf("bounded failure %q must name the locked database", msg)
	}
	if sleeps != 2 {
		t.Fatalf("sleeps = %d, want 2 backoffs before the bound", sleeps)
	}
}

func TestRetryOnBusyInvokesOnRetryThenSucceeds(t *testing.T) {
	var calls []string
	var mu sync.Mutex
	opts := BusyRetryOptions{
		Attempts:    3,
		InitialWait: time.Millisecond,
		Sleep:       func(time.Duration) {},
		OnRetry: func(attempt, attempts int, wait time.Duration, err error) {
			mu.Lock()
			calls = append(calls, fmt.Sprintf("retry %d/%d", attempt, attempts))
			mu.Unlock()
		},
	}
	n := 0
	err := RetryOnBusy(func() error {
		n++
		if n == 1 {
			return errors.New("database is locked (5) (SQLITE_BUSY)")
		}
		return nil
	}, opts)
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("fn calls = %d, want 2", n)
	}
	if len(calls) != 1 || calls[0] != "retry 1/3" {
		t.Fatalf("OnRetry log = %v, want [retry 1/3]", calls)
	}
}

func TestIsBusyRecognizesDriverText(t *testing.T) {
	if !IsBusy(errors.New("import sourceharvest: database is locked (5) (SQLITE_BUSY)")) {
		t.Fatal("expected the runbook SQLITE_BUSY string to be retryable")
	}
	if IsBusy(errors.New("import sourceharvest: timed out after 2m0s")) {
		t.Fatal("timeout must not be treated as retryable lock")
	}
}

func TestIsBusyRecognizesBusyFamilyCodes(t *testing.T) {
	for _, err := range []error{
		errors.New("database is locked (5) (SQLITE_BUSY)"),
		errors.New("database is locked (517)"),
		errors.New("database is locked (261)"),
		errors.New("database is locked (773)"),
	} {
		if !IsBusy(err) {
			t.Fatalf("IsBusy(%q) = false, want BUSY-family retryable", err)
		}
	}
	if IsBusy(errors.New("database is locked (6)")) {
		t.Fatal("SQLITE_LOCKED (6) is not the BUSY family")
	}
}

func TestRetryOnBusyRetriesSnapshotThenSucceeds(t *testing.T) {
	n := 0
	err := RetryOnBusy(func() error {
		n++
		if n == 1 {
			return errors.New("database is locked (517)")
		}
		return nil
	}, BusyRetryOptions{Attempts: 3, InitialWait: time.Millisecond, Sleep: func(time.Duration) {}})
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("fn calls = %d, want 2 (retry after SQLITE_BUSY_SNAPSHOT)", n)
	}
}

func TestRetryOnBusyHonorsMaxTotalWait(t *testing.T) {
	started := time.Now()
	n := 0
	err := RetryOnBusy(func() error {
		n++
		return errors.New("database is locked (517)")
	}, BusyRetryOptions{
		Attempts:     50,
		InitialWait:  20 * time.Millisecond,
		MaxWait:      20 * time.Millisecond,
		MaxTotalWait: 80 * time.Millisecond,
	})
	elapsed := time.Since(started)
	if err == nil {
		t.Fatal("expected bounded exhaustion")
	}
	if !strings.Contains(err.Error(), HolderDiagnosisLabel) {
		t.Fatalf("bounded failure %q must name %s", err, HolderDiagnosisLabel)
	}
	if strings.Contains(err.Error(), "SQLITE_BUSY") || strings.Contains(err.Error(), "(517)") {
		t.Fatalf("bounded failure %q must not emit the raw lock string", err)
	}
	if n > 8 {
		t.Fatalf("attempts = %d, MaxTotalWait should have stopped the 50-attempt loop", n)
	}
	if elapsed > 2*time.Second {
		t.Fatalf("elapsed %s, composed wait must stay in the low seconds", elapsed)
	}
}

func openImmediate(t *testing.T, path string) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", "file:"+path+"?_pragma=busy_timeout(0)")
	if err != nil {
		t.Fatal(err)
	}
	db.SetMaxOpenConns(1)
	if err := db.Ping(); err != nil {
		t.Fatal(err)
	}
	return db
}

func TestDiagnoseLockHolderNamesThisProcess(t *testing.T) {
	path := t.TempDir() + "/miseledger.db"
	db, err := archive.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	text := DiagnoseLockHolder(path)
	if !strings.Contains(text, HolderDiagnosisLabel) {
		t.Fatalf("diagnosis %q must name %s", text, HolderDiagnosisLabel)
	}
	if !strings.Contains(text, "db=") {
		t.Fatalf("diagnosis %q must name the database", text)
	}
	if !strings.Contains(text, "pid="+itoa(os.Getpid())) && !strings.Contains(text, "holder=unknown") {
		t.Fatalf("diagnosis %q should name this pid or report unknown", text)
	}
}

func itoa(n int) string {
	return fmt.Sprintf("%d", n)
}
