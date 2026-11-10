"""The hybrid configuration Compose setup writes starts the hub (#2467).

ops/fleet-hub/bin/hub-setup.py decides the backend with faked probes, the
local GPU path and the remote path, and writes config/hub.toml with its own
`apply`. The hub is then started with that file named by `--memory-config`,
so the hub's own parser and embedder checks decide: a file they refuse stops
startup with the reason. Only the token file path changes, because the
container path setup names (/run/secrets/embed_token) does not exist here.
No embedding request is answered, so status is hybrid and may be degraded.

The second test drives the S31 `setup` check's hub start
(ops/fleet-hub/checks/s31_checks.py) with this binary on setup's own secret
files, after init and after each switch direction: a lexical config reports
`ok`, and a hybrid one whose embedder cannot be reached starts and reports
`degraded` once a card has been sent for embedding.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import memory as fixture

ROOT = Path(__file__).resolve().parents[3]
RECIPE = ROOT / "ops" / "fleet-hub"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # The dataclasses in it resolve their annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


setup = load("hub_setup_under_test", RECIPE / "bin" / "hub-setup.py")
s31 = load("s31_checks_under_test", RECIPE / "checks" / "s31_checks.py")
MODELS = setup.load_r1(RECIPE / "r1-models.json")
DIGEST = "sha256:" + "d" * 64


def gpu_host(argv: list[str]) -> tuple[int, str]:
    if argv[:2] == ["docker", "info"]:
        return 0, '{"nvidia": {}, "runc": {}}'
    if argv[:2] == ["docker", "run"]:
        return 0, "Example GPU, 20000\n"
    raise AssertionError(argv)


def no_gpu_host(argv: list[str]) -> tuple[int, str]:
    return 0, '{"runc": {}}'


def endpoint(url: str, body: dict, headers: dict) -> tuple[int, dict, dict]:
    vector = [0.1] * int(MODELS[0]["dimensions"])
    if url.endswith("/v1/embeddings"):
        return 200, {"x-model-digest": DIGEST}, {"data": [{"index": 0, "embedding": vector}]}
    return 200, {"x-model-digest": DIGEST}, {"embeddings": [vector]}


class SetupConfigStartsTheHub(unittest.TestCase):
    def decide(self, case: str, token: Path):
        common = {"post": endpoint, "models": MODELS, "embed_image": setup.DEFAULT_EMBED_IMAGE}
        if case == "local":
            return setup.decide(run=gpu_host, remote_url=None, remote_token=None, remote_api="api/embed", **common)
        url, api = {
            "remote-tailnet": ("http://embed-host.example.ts.net:11434", "api/embed"),
            "remote-https": ("https://embed.example.test/", "v1/embeddings"),
        }[case]
        return setup.decide(
            run=no_gpu_host, remote_url=url, remote_token=token, remote_api=api, skip_gpu=True, **common
        )

    def test_each_hybrid_path_starts_the_hub_in_hybrid_mode(self):
        for case, backend in (("local", "local"), ("remote-tailnet", "remote"), ("remote-https", "remote")):
            with self.subTest(case), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                token = state / "embed.token"
                token.write_text("setup-config-test-token\n", encoding="utf-8")
                token.chmod(0o600)
                decision = self.decide(case, token)
                self.assertEqual((decision.retrieval, decision.backend), ("hybrid", backend), decision.failed)
                project = state / "project"
                project.mkdir()
                setup.apply(project, dataclasses.replace(decision, token_file=str(token)), {})
                config = project / "config" / "hub.toml"
                hub = fixture.launch(state, f"--route-mode memory=native --memory-config {config}")
                try:
                    response = fixture.status(hub)
                    self.assertEqual(response.status, 200, response)
                    self.assertEqual(response.json["retrieval"]["mode"], "hybrid", response.json)
                finally:
                    fixture.hc.stop_hub(hub)

    def install(self, project: Path, decision) -> None:
        """What setup's init and switch write, through the same transaction."""
        setup.write_install(project, decision, {}, with_secrets=True)

    def accepted(self, project: Path, mode: str) -> dict:
        seen = s31.hub_on_setup_config(str(fixture.BINARY), sys.executable, project, embed=mode == "hybrid")
        self.assertEqual(seen.get("ready"), 200, seen)
        state = "degraded" if mode == "hybrid" else "ok"
        self.assertEqual((seen["retrieval"].get("mode"), seen["retrieval"].get("state")), (mode, state), seen)
        return seen

    def test_the_s31_hub_start_accepts_setup_output_after_init_and_each_switch(self):
        for case in ("local", "remote-tailnet"):
            with self.subTest(case), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                token = state / "embed.token"
                token.write_text("setup-config-test-token\n", encoding="utf-8")
                project = state / "project"
                project.mkdir()
                hybrid = self.decide(case, token)
                self.install(project, hybrid)
                seen = self.accepted(project, "hybrid")
                self.assertEqual(seen["retrieval"].get("reason"), "embedder-unavailable", seen)
                self.install(project, setup.Decision(retrieval="lexical"))
                self.accepted(project, "lexical")
                self.install(project, self.decide(case, token))
                self.accepted(project, "hybrid")


if __name__ == "__main__":
    unittest.main()
