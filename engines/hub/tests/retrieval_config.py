"""Bad operator RRF settings fail closed for memory, including lexical mode."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import memory as fixture


class RrfConfig(unittest.TestCase):
    def test_default_rrf_typo_leaves_memory_unavailable_and_other_routes_healthy(self):
        for mode in ("lexical", "hybrid"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                (state / "hub.toml").write_text(f'[memory]\nretrieval = "{mode}"\n[memory.rrf]\ndense = 0\n')
                hub = fixture.launch(state, "--route-mode memory=native")
                try:
                    result = fixture.status(hub)
                    self.assertEqual((result.status, result.json), (503, fixture.UNAVAILABLE))
                    self.assertEqual(fixture.hc.request(hub.url, "GET", "/health").status, 200)
                    self.assertIn(b"memory unavailable: memory.rrf weights must be", (state / "hub.log").read_bytes())
                finally:
                    fixture.hc.stop_hub(hub)

    def test_explicit_rrf_typo_stops_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "named.toml").write_text('[memory]\nretrieval = "lexical"\n[memory.rrf]\ndense = 0\n')
            with self.assertRaisesRegex(fixture.hc.ContractError, "memory.rrf weights must be"):
                hub = fixture.launch(state, "--route-mode memory=native --memory-config {state_dir}/named.toml")
                fixture.hc.stop_hub(hub)


if __name__ == "__main__":
    unittest.main()
