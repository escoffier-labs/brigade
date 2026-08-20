package app

import (
	"bytes"
	"io"
	"os"
	"strings"
	"testing"
)

func TestMutationScannerTrustReviewWithoutCapabilityIsRefused(t *testing.T) {
	withTempHome(t)
	t.Setenv("BRIGADE_REQUIRE_TRUST_CAPABILITY", "")
	runOK(t, "init")
	id := insertCleanIntegrityItem(t, "UNIQUE_CAP1029_scanner_self_elevate body", "quarantined", "pending")
	digest := itemContentHashFromShow(t, id)

	t.Run("piped_empty", func(t *testing.T) {
		assertTrustReviewRefusedOnStdin(t, id, digest, bytes.NewReader(nil), "piped empty")
	})
	t.Run("dev_null_char_device", func(t *testing.T) {
		devnull, err := os.Open(os.DevNull)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = devnull.Close() })
		info, err := devnull.Stat()
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode()&os.ModeCharDevice == 0 {
			t.Fatalf("%s is not a character device; this test must exercise ModeCharDevice", os.DevNull)
		}
		assertTrustReviewRefusedOnStdin(t, id, digest, devnull, "/dev/null")
	})
	t.Run("real_pty", func(t *testing.T) {
		slave := openPtySlave(t)
		info, err := slave.Stat()
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode()&os.ModeCharDevice == 0 {
			t.Fatal("pty slave is not a character device; this test must exercise a real PTY")
		}
		assertTrustReviewRefusedOnStdin(t, id, digest, slave, "pty")
	})

	show := runJSON(t, "show", id, "--json")
	if show["trust_label"] != "quarantined" {
		t.Fatalf("item must stay quarantined after no-capability attacks: %#v", show)
	}
	status := ""
	if provenance, ok := show["provenance"].(map[string]any); ok {
		if trust, ok := provenance["trust"].(map[string]any); ok {
			if injection, ok := trust["injection"].(map[string]any); ok {
				status, _ = injection["status"].(string)
			}
		}
	}
	if status != "pending" {
		t.Fatalf("injection must stay pending after no-capability attacks: %q %#v", status, show)
	}

	positive := runTrustReviewJSON(t, id, digest, "--to-label", "verified", "--mark-injection-clean")
	if positive["to_label"] != "verified" {
		t.Fatalf("operator path with capability = %#v", positive)
	}
}

func assertTrustReviewRefusedOnStdin(t *testing.T, itemID, digest string, stdin io.Reader, label string) {
	t.Helper()
	var out, errb bytes.Buffer
	code := RunWithStdin(
		[]string{
			"trust", "review",
			"--item", itemID,
			"--content-hash", digest,
			"--to-label", "verified",
			"--mark-injection-clean",
			"--operator-command", "scanner:forged",
			"--json",
		},
		stdin,
		&out,
		&errb,
	)
	if code == 0 {
		t.Fatalf("%s stdin: scanner-forged trust review must be refused: stdout=%s stderr=%s", label, out.String(), errb.String())
	}
	if !strings.Contains(errb.String(), "capability") {
		t.Fatalf("%s stdin: refusal must name capability: %s", label, errb.String())
	}
	if strings.Contains(out.String(), `"injection_status": "clean"`) || strings.Contains(out.String(), `"to_label": "verified"`) {
		t.Fatalf("%s stdin: refusal leaked a successful review: %s", label, out.String())
	}
}
