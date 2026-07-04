package app

import (
	"path/filepath"
	"testing"
)

// TestForkDiff exercises the ActiveGraph-inspired branch (fork) + structural
// diff: snapshot the ledger, import more into the live one, and confirm the
// diff reports exactly the added evidence while the live ledger is untouched.
func TestForkDiff(t *testing.T) {
	withTempHome(t)
	discrawl := repoPath(t, "testdata/adapters/discrawl.fixture.jsonl")
	agent := repoPath(t, "testdata/adapters/agent-session.fixture.jsonl")

	runOK(t, "init")
	runOK(t, "import", "adapter", discrawl, "--source", "discrawl")
	base := runJSON(t, "status", "--json")["items"].(float64)

	branchA := filepath.Join(t.TempDir(), "branchA.db")
	forkOut := runJSON(t, "fork", branchA, "--json")
	if forkOut["forked"] != true || forkOut["items"].(float64) != base {
		t.Fatalf("fork branchA = %v, want items=%v", forkOut, base)
	}
	assertPrivate(t, branchA)

	// Diverge the live ledger; the branch stays frozen at the discrawl-only state.
	runOK(t, "import", "adapter", agent, "--source", "codex")
	total := runJSON(t, "status", "--json")["items"].(float64)
	added := total - base
	if added <= 0 {
		t.Fatalf("expected the agent import to add items (base=%v total=%v)", base, total)
	}

	branchB := filepath.Join(t.TempDir(), "branchB.db")
	runOK(t, "fork", branchB, "--json")

	// A -> B: everything the codex import added shows up as added, nothing removed/changed.
	d := runJSON(t, "diff", branchA, branchB, "--json")
	items := d["items"].(map[string]any)
	if items["added"].(float64) != added {
		t.Fatalf("diff A->B added = %v, want %v", items["added"], added)
	}
	if items["removed"].(float64) != 0 || items["changed"].(float64) != 0 {
		t.Fatalf("diff A->B should only add: %v", items)
	}

	// B -> A: the same items now read as removed (direction is honored).
	rev := runJSON(t, "diff", branchB, branchA, "--json")["items"].(map[string]any)
	if rev["removed"].(float64) != added || rev["added"].(float64) != 0 {
		t.Fatalf("diff B->A should only remove: %v", rev)
	}

	// Identical states diff to nothing.
	branchC := filepath.Join(t.TempDir(), "branchC.db")
	runOK(t, "fork", branchC, "--json")
	same := runJSON(t, "diff", branchB, branchC, "--json")["items"].(map[string]any)
	if same["added"].(float64) != 0 || same["removed"].(float64) != 0 || same["changed"].(float64) != 0 {
		t.Fatalf("identical branches should diff clean: %v", same)
	}

	// The live ledger is unchanged by any diff/fork.
	if runJSON(t, "status", "--json")["items"].(float64) != total {
		t.Fatalf("diff/fork mutated the live ledger")
	}

	// fork refuses to clobber an existing file.
	if code, _, _ := run("fork", branchA, "--json"); code == 0 {
		t.Fatalf("fork overwrote an existing branch")
	}

	// diff against a missing file fails cleanly rather than treating it as empty.
	if code, _, _ := run("diff", filepath.Join(t.TempDir(), "nope.db"), "--json"); code == 0 {
		t.Fatalf("diff against a missing branch should fail")
	}
}
