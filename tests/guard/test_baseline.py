# content-guard: allow private-ipv4 file
# content-guard: allow loopback-ipv4 file
# content-guard: allow api-key-assignment file
# content-guard: allow bearer-token file
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from brigade.guard.baseline import (
    BASELINE_SCHEMA_VERSION,
    Baseline,
    BaselineEntry,
    filter_findings,
    fingerprint_for,
    init_baseline,
    load_baseline,
    save_baseline,
)
from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy


ROOT = Path(__file__).resolve().parents[2]


class BaselineModuleTests(unittest.TestCase):
    def test_init_baseline_captures_current_findings(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # content-guard: allow all
            (root / "leak.md").write_text("Service is localhost:5204 and 192.168.99.91.\n")
            (root / "clean.md").write_text("Nothing to see here.\n")

            baseline = init_baseline(root, policy=Policy())

        rule_ids = {e.rule_id for e in baseline.entries}
        self.assertIn("localhost-port", rule_ids)
        self.assertIn("private-ipv4", rule_ids)
        # Only the leak file has entries.
        paths = {e.path for e in baseline.entries}
        self.assertEqual(paths, {"leak.md"})

    def test_init_baseline_skips_allow_comment_findings(self) -> None:
        # Findings already neutralized by inline allow directives don't need
        # baseline entries; capturing them just adds noise.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "doc.md").write_text("<!-- content-guard: allow localhost-port -->\nlocalhost:5204\n")

            baseline = init_baseline(root, policy=Policy())

        self.assertEqual(baseline.entries, [])

    def test_init_baseline_skips_excluded_dirs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # content-guard: allow localhost-port
            leak_text = "localhost:5204\n"
            (root / "real.md").write_text(leak_text)
            for excluded in ("node_modules", ".git", "dist", ".venv"):
                sub = root / excluded
                sub.mkdir()
                (sub / "leak.md").write_text(leak_text)

            baseline = init_baseline(root, policy=Policy())

        paths = {e.path for e in baseline.entries}
        self.assertEqual(paths, {"real.md"})

    def test_baseline_save_and_load_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # content-guard: allow all
            (root / "leak.md").write_text("Service is localhost:5204.\n")
            baseline = init_baseline(root, policy=Policy())

            out_path = root / "baseline.json"
            save_baseline(baseline, out_path)
            loaded = load_baseline(out_path)

        self.assertEqual(loaded.version, BASELINE_SCHEMA_VERSION)
        self.assertEqual(loaded.created_at, baseline.created_at)
        self.assertEqual(len(loaded.entries), len(baseline.entries))
        for original, restored in zip(baseline.entries, loaded.entries, strict=True):
            self.assertEqual(original, restored)

    def test_baseline_file_has_expected_top_level_schema(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = Baseline(
                entries=[
                    BaselineEntry(
                        path="docs/foo.md",
                        rule_id="localhost-port",
                        match="localhost:5204",
                        line=3,
                        fingerprint=fingerprint_for("localhost-port", "localhost:5204"),
                    )
                ],
                created_at="2026-05-22T00:00:00+00:00",
            )
            out_path = root / "baseline.json"
            save_baseline(baseline, out_path)
            payload = json.loads(out_path.read_text())

        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["created_at"], "2026-05-22T00:00:00+00:00")
        self.assertEqual(len(payload["entries"]), 1)
        self.assertEqual(payload["entries"][0]["rule_id"], "localhost-port")

    def test_filter_findings_removes_baselined_findings_only(self) -> None:
        # content-guard: allow all
        text = "localhost:5204 and 192.168.99.91\n"
        result = scan_text(text, policy=Policy())

        baseline = Baseline(
            entries=[
                BaselineEntry(
                    path="leak.md",
                    rule_id="localhost-port",
                    match="localhost:5204",
                    line=1,
                    fingerprint=fingerprint_for("localhost-port", "localhost:5204"),
                )
            ],
            created_at="2026-05-22T00:00:00+00:00",
        )

        kept = filter_findings(result.findings, baseline, "leak.md")
        rule_ids = {f.rule_id for f in kept}
        # localhost-port is baselined and should be filtered out.
        self.assertNotIn("localhost-port", rule_ids)
        # private-ipv4 is NOT in the baseline and must remain.
        self.assertIn("private-ipv4", rule_ids)

    def test_filter_findings_keeps_new_findings_with_different_content(self) -> None:
        # Baseline accepts ONE specific localhost port. A different port string
        # produces a different fingerprint and must be flagged as new.
        # content-guard: allow all
        text = "old localhost:5204 and new localhost:9999\n"
        result = scan_text(text, policy=Policy())
        # Both findings should be from rule localhost-port.
        all_matches = {f.match for f in result.findings if f.rule_id == "localhost-port"}
        self.assertEqual(all_matches, {"localhost:5204", "localhost:9999"})

        baseline = Baseline(
            entries=[
                BaselineEntry(
                    path="leak.md",
                    rule_id="localhost-port",
                    match="localhost:5204",
                    line=1,
                    fingerprint=fingerprint_for("localhost-port", "localhost:5204"),
                )
            ],
            created_at="2026-05-22T00:00:00+00:00",
        )

        kept = filter_findings(result.findings, baseline, "leak.md")
        kept_matches = {f.match for f in kept if f.rule_id == "localhost-port"}
        self.assertEqual(kept_matches, {"localhost:9999"})

    def test_filter_findings_same_match_different_file_is_new(self) -> None:
        # Same string in a different file should NOT be considered baselined.
        # content-guard: allow all
        text = "localhost:5204\n"
        result = scan_text(text, policy=Policy())

        baseline = Baseline(
            entries=[
                BaselineEntry(
                    path="docs/old.md",
                    rule_id="localhost-port",
                    match="localhost:5204",
                    line=1,
                    fingerprint=fingerprint_for("localhost-port", "localhost:5204"),
                )
            ],
            created_at="2026-05-22T00:00:00+00:00",
        )

        kept = filter_findings(result.findings, baseline, "docs/new.md")
        rule_ids = {f.rule_id for f in kept}
        self.assertIn("localhost-port", rule_ids)

    def test_fingerprint_is_stable_and_unique(self) -> None:
        fp1 = fingerprint_for("localhost-port", "localhost:5204")
        fp2 = fingerprint_for("localhost-port", "localhost:5204")
        fp3 = fingerprint_for("localhost-port", "localhost:5205")
        fp4 = fingerprint_for("private-ipv4", "localhost:5204")

        self.assertEqual(fp1, fp2)
        self.assertNotEqual(fp1, fp3)
        self.assertNotEqual(fp1, fp4)
        self.assertEqual(len(fp1), 16)

    def test_baseline_load_rejects_invalid_schema(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"

            # Not JSON at all.
            path.write_text("not json {")
            with self.assertRaises(ValueError):
                load_baseline(path)

            # Root is not an object.
            path.write_text("[]")
            with self.assertRaises(ValueError):
                load_baseline(path)

            # Missing 'entries'.
            path.write_text(json.dumps({"version": 1, "created_at": "x"}))
            with self.assertRaises(ValueError):
                load_baseline(path)

            # Entries not a list.
            path.write_text(json.dumps({"version": 1, "created_at": "x", "entries": {}}))
            with self.assertRaises(ValueError):
                load_baseline(path)

            # Entry missing required field.
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "created_at": "x",
                        "entries": [{"path": "a", "rule_id": "b", "match": "c"}],
                    }
                )
            )
            with self.assertRaises(ValueError):
                load_baseline(path)

    def test_baseline_load_rejects_unsupported_version(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps({"version": 999, "created_at": "x", "entries": []}))
            with self.assertRaises(ValueError) as ctx:
                load_baseline(path)
            self.assertIn("version", str(ctx.exception))

            # Non-integer version.
            path.write_text(json.dumps({"version": "1", "created_at": "x", "entries": []}))
            with self.assertRaises(ValueError):
                load_baseline(path)


class BaselineCliTests(unittest.TestCase):
    """End-to-end CLI tests for `content-guard baseline` and `scan --baseline`.

    Kept here (not in test_cli.py) to avoid collisions with a concurrent
    subagent editing test_cli.py for the audit subcommand.
    """

    def test_baseline_init_writes_json_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # content-guard: allow all
            (root / "leak.md").write_text("Service is localhost:5204.\n")

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard",
                    "baseline",
                    "init",
                    str(root),
                ],
                cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            baseline_path = root / ".content-guard-baseline.json"
            self.assertTrue(baseline_path.exists())
            payload = json.loads(baseline_path.read_text())
            self.assertEqual(payload["version"], 1)
            self.assertGreater(len(payload["entries"]), 0)
            rule_ids = {e["rule_id"] for e in payload["entries"]}
            self.assertIn("localhost-port", rule_ids)

    def test_baseline_init_respects_output_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # content-guard: allow all
            (root / "leak.md").write_text("Service is localhost:5204.\n")
            custom_out = root / "custom" / "baseline.json"

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard",
                    "baseline",
                    "init",
                    str(root),
                    "--output",
                    str(custom_out),
                ],
                cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            self.assertTrue(custom_out.exists())
            self.assertFalse((root / ".content-guard-baseline.json").exists())

    def test_scan_with_baseline_suppresses_known_findings(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "leak.md"
            # content-guard: allow all
            target.write_text("Service is localhost:5204.\n")

            # Create baseline first.
            init_proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard",
                    "baseline",
                    "init",
                    str(root),
                ],
                cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(init_proc.returncode, 0, msg=init_proc.stderr)
            baseline_path = root / ".content-guard-baseline.json"
            self.assertTrue(baseline_path.exists())

            # Now scan the same file with the baseline - should be clean.
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard",
                    "scan",
                    str(target),
                    "--baseline",
                    str(baseline_path),
                    "--json",
                ],
                cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertFalse(payload["blocked"])
            self.assertEqual(payload["findings"], [])

    def test_scan_with_baseline_still_flags_new_findings(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "leak.md"
            # content-guard: allow all
            target.write_text("Service is localhost:5204.\n")

            # Capture baseline.
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard",
                    "baseline",
                    "init",
                    str(root),
                ],
                cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=True,
            )
            baseline_path = root / ".content-guard-baseline.json"

            # Add a NEW violation that is not in the baseline.
            # content-guard: allow all
            target.write_text("Service is localhost:5204 and new leak localhost:9999.\n")

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade.guard",
                    "scan",
                    str(target),
                    "--baseline",
                    str(baseline_path),
                    "--json",
                ],
                cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 1, msg=proc.stdout + proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["blocked"])
            matches = [f["match"] for f in payload["findings"]]
            self.assertIn("localhost:9999", matches)
            self.assertNotIn("localhost:5204", matches)


if __name__ == "__main__":
    unittest.main()
