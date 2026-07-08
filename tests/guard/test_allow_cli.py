from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brigade.guard.cli import main
from brigade.guard.policy import load_policy


def _write_policy(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "name": "private-test",
                "defaults": {"infrastructure": "warn"},
                "allow_values": ["already-public.example"],
            },
            indent=2,
        )
        + "\n"
    )


class AllowAddTests(unittest.TestCase):
    def test_add_appends_value_and_policy_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "internal.json"
            _write_policy(policy_path)

            rc = main(["allow", "add", "git@github.com", "--policy", str(policy_path)])

            self.assertEqual(rc, 0)
            data = json.loads(policy_path.read_text())
            self.assertIn("git@github.com", data["allow_values"])
            self.assertIn("already-public.example", data["allow_values"])
            policy = load_policy(policy_path)
            self.assertIn("git@github.com", policy.allow_values)

    def test_add_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "internal.json"
            _write_policy(policy_path)

            main(["allow", "add", "dup-value-123", "--policy", str(policy_path)])
            rc = main(["allow", "add", "dup-value-123", "--policy", str(policy_path)])

            self.assertEqual(rc, 0)
            data = json.loads(policy_path.read_text())
            self.assertEqual(data["allow_values"].count("dup-value-123"), 1)

    def test_add_with_note_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "internal.json"
            _write_policy(policy_path)

            rc = main(
                [
                    "allow",
                    "add",
                    "public-fixture-string",
                    "--policy",
                    str(policy_path),
                    "--note",
                    "upstream test fixture, already public",
                ]
            )

            self.assertEqual(rc, 0)
            data = json.loads(policy_path.read_text())
            self.assertEqual(
                data["_allow_values_notes"]["public-fixture-string"],
                "upstream test fixture, already public",
            )
            load_policy(policy_path)

    def test_add_creates_allow_values_key_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "internal.json"
            policy_path.write_text(json.dumps({"name": "bare"}) + "\n")

            rc = main(["allow", "add", "brand-new-value", "--policy", str(policy_path)])

            self.assertEqual(rc, 0)
            data = json.loads(policy_path.read_text())
            self.assertEqual(data["allow_values"], ["brand-new-value"])

    def test_add_missing_policy_file_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"

            rc = main(["allow", "add", "x-value", "--policy", str(missing)])

            self.assertNotEqual(rc, 0)
            self.assertFalse(missing.exists())

    def test_list_prints_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "internal.json"
            _write_policy(policy_path)

            rc = main(["allow", "list", "--policy", str(policy_path)])

            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
