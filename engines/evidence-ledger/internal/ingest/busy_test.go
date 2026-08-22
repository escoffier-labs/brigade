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

const (
	sqliteBusy         = 5
	sqliteBusyRecovery = 261
	sqliteBusySnapshot = 517
	sqliteBusyTimeout  = 773
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
			return resultCodeError{code: sqliteBusy, msg: "database is locked (5) (SQLITE_BUSY)"}
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

func TestRetryOnBusyDoesNotRetryFreeTextBusy(t *testing.T) {
	n := 0
	err := RetryOnBusy(func() error {
		n++
		return errors.New("sourceharvest: unexpected token (5) (SQLITE_BUSY) in stderr")
	}, BusyRetryOptions{Attempts: 4, InitialWait: time.Millisecond, Sleep: func(time.Duration) {}})
	if err == nil {
		t.Fatal("expected the free-text error to be returned as-is")
	}
	if n != 1 {
		t.Fatalf("fn calls = %d, want 1 (subprocess stderr must not retry)", n)
	}
}

func TestIsBusyIgnoresBusyFamilyNumbersInFreeText(t *testing.T) {
	// Subprocess stderr and wrapped plain errors can mention (5)/(261)/(517)
	// without being a SQLite result code. Those must not classify as busy.
	for _, err := range []error{
		errors.New("import sourceharvest: database is locked (5) (SQLITE_BUSY)"),
		errors.New("scanner stderr: unexpected token (261) near harvest"),
		errors.New("helper failed: snapshot conflict (517) in log line"),
		errors.New("timeout (773) from sidecar"),
		fmt.Errorf("import sourceharvest: %s", "database is locked (5) (SQLITE_BUSY)"),
	} {
		if IsBusy(err) {
			t.Fatalf("IsBusy(%q) = true, free text must not classify as busy", err)
		}
	}
	if IsBusy(errors.New("import sourceharvest: timed out after 2m0s")) {
		t.Fatal("timeout must not be treated as retryable lock")
	}
}

func TestIsBusyRecognizesDriverResultCode(t *testing.T) {
	err := realBusyResultCode(t)
	if !IsBusy(err) {
		t.Fatalf("IsBusy(%T %v) = false, want real SQLITE_BUSY result code", err, err)
	}
	if !IsBusy(fmt.Errorf("import sourceharvest: %w", err)) {
		t.Fatal("wrapped driver busy must still classify via the result code")
	}
	if code, ok := sqliteErrorCode(err); !ok || !isBusyFamilyCode(code) {
		t.Fatalf("driver busy code = (%d, %v), want BUSY-family", code, ok)
	}
}

func TestIsBusyRecognizesBusyFamilyCodes(t *testing.T) {
	for _, code := range []int{sqliteBusy, sqliteBusyRecovery, sqliteBusySnapshot, sqliteBusyTimeout} {
		err := resultCodeError{code: code, msg: fmt.Sprintf("database is locked (%d)", code)}
		if !IsBusy(err) {
			t.Fatalf("IsBusy(code=%d) = false, want BUSY-family retryable", code)
		}
	}
	if IsBusy(resultCodeError{code: 6, msg: "database table is locked (6)"}) {
		t.Fatal("SQLITE_LOCKED (6) is not the BUSY family")
	}
	// A non-busy error whose text merely contains a parenthesized busy-family
	// number must stay false even when a Code() is present for a different rc.
	if IsBusy(resultCodeError{code: 1, msg: "SQL logic error (5) (SQLITE_BUSY) mentioned in text"}) {
		t.Fatal("non-busy result code must not classify from parenthesized text")
	}
}

func TestRetryOnBusyRetriesSnapshotThenSucceeds(t *testing.T) {
	n := 0
	err := RetryOnBusy(func() error {
		n++
		if n == 1 {
			return resultCodeError{code: sqliteBusySnapshot, msg: "database is locked (517)"}
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
		return resultCodeError{code: sqliteBusySnapshot, msg: "database is locked (517)"}
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

func TestRetryOnBusyConcurrentImportsStayBounded(t *testing.T) {
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

	const workers = 4
	start := make(chan struct{})
	var wg sync.WaitGroup
	errs := make(chan error, workers)
	opts := BusyRetryOptions{
		Attempts:     16,
		InitialWait:  5 * time.Millisecond,
		MaxWait:      50 * time.Millisecond,
		MaxTotalWait: 8 * time.Second,
	}
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			db, openErr := archive.Open(path)
			if openErr != nil {
				errs <- openErr
				return
			}
			defer db.Close()
			<-start
			jsonl := fmt.Sprintf(`{"schema":"miseledger.adapter.v1","source":{"kind":"busy-stress","name":"Busy Stress"},"collection":{"external_id":"busy:collection:%d","kind":"agent_session","name":"busy"},"item":{"external_id":"busy:item:%d","kind":"message","created_at":"2026-06-03T00:00:00Z","text":"concurrent import %d","tags":["busy"]},"actor":{"external_id":"busy:actor:%d","type":"human","name":"busy"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":"busy.jsonl","ordinal":1}}`+"\n", i, i, i, i)
			errs <- RetryOnBusy(func() error {
				_, importErr := ImportAdapterReader(db, strings.NewReader(jsonl), fmt.Sprintf("busy://stress/%d", i), "busy-stress")
				return importErr
			}, opts)
		}(i)
	}
	close(start)
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent import: %v", err)
		}
	}
}

type resultCodeError struct {
	code int
	msg  string
}

func (e resultCodeError) Error() string { return e.msg }
func (e resultCodeError) Code() int     { return e.code }

func realBusyResultCode(t *testing.T) error {
	t.Helper()
	path := t.TempDir() + "/busy-code.db"
	holder := openImmediate(t, path)
	t.Cleanup(func() { _ = holder.Close() })
	if _, err := holder.Exec("CREATE TABLE t(x INTEGER)"); err != nil {
		t.Fatal(err)
	}
	if _, err := holder.Exec("BEGIN EXCLUSIVE"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = holder.Exec("ROLLBACK") })
	other := openImmediate(t, path)
	t.Cleanup(func() { _ = other.Close() })
	_, err := other.Exec("INSERT INTO t(x) VALUES (1)")
	if err == nil {
		t.Fatal("expected a real SQLITE_BUSY result code from the held write lock")
	}
	if _, ok := sqliteErrorCode(err); !ok {
		t.Fatalf("held-lock error %T %v has no result Code()", err, err)
	}
	return err
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
