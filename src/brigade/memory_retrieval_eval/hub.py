"""Explicit hub adapters and disposable fixture hubs for the existing harness.

No ambient fleet settings are read. External hubs are read only. A candidate
embedder is configured on the disposable hub with an explicit memory TOML file.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brigade.memory_hub_client import MemoryHubClient, event_id

from .corpus import CardDoc, repo_root


@dataclass(frozen=True)
class HubOptions:
    url: str | None = None
    token_file: Path | None = None
    scope: str = "fleet"
    binary: Path | None = None
    memory_config: Path | None = None
    raw: bool = False
    candidate_embedder: str | None = None


class HubAdapter:
    """A run must use one index, scorer, mode, weights and candidate throughout."""

    def __init__(self, client: MemoryHubClient, mode: str, options: HubOptions, cards: Sequence[CardDoc] = ()) -> None:
        self.ids = {Path(card.path).stem: card.card_id for card in cards}
        self.expected_corpus = corpus_identity(cards) if cards else None
        self.client = client
        self.mode = mode
        self.options = options
        self.binding: dict[str, Any] | None = None

    def __call__(self, query: str, limit: int) -> list[tuple[str, float]]:
        reply = self.client.call(
            "search",
            query=query,
            limit=min(limit, 20),
            mode=self.mode,
            eval=True,
            scopes=[self.options.scope],
            ranking="raw" if self.options.raw else "default",
        )
        retrieval = reply.get("retrieval", {})
        if retrieval.get("mode") != self.mode or retrieval.get("stale") or retrieval.get("degraded"):
            raise ValueError("hub eval requires the requested mode and a fresh, healthy index")
        fields = ("indexed_seq", "index_format", "scorer_version", "rrf_weights", "ranking")
        if any(reply.get(key) is None for key in fields):
            raise ValueError("hub eval response lacks index/scorer binding")
        binding = {key: reply[key] for key in fields}
        if "policy_revision" not in reply or "policy_digest" not in reply:
            raise ValueError("hub eval response lacks policy binding")
        binding["policy_revision"] = reply["policy_revision"]
        binding["policy_digest"] = reply["policy_digest"]
        identity = reply.get("corpus_identity", {}).get(self.options.scope)
        if not identity:
            raise ValueError("hub eval response lacks scope corpus identity")
        if self.expected_corpus is not None and identity != self.expected_corpus:
            raise ValueError("hub eval scope corpus differs from the fixture, including extra cards")
        binding["corpus_identity"] = identity
        binding["mode"] = self.mode
        binding["embedder_id"] = reply.get("embedder_id")
        if self.mode == "hybrid" and not binding["embedder_id"]:
            raise ValueError("hybrid eval response lacks embedder identity")
        if (
            self.mode == "hybrid"
            and self.options.candidate_embedder
            and binding["embedder_id"] != self.options.candidate_embedder
        ):
            raise ValueError("hub eval candidate embedder does not match")
        if self.binding is not None and binding != self.binding:
            raise ValueError("hub index or retrieval configuration changed during eval")
        self.binding = copy.deepcopy(binding)
        return [
            (self.ids.get(str(hit["card_id"]), str(hit["card_id"])), float(hit["score"])) for hit in reply["matches"]
        ]


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def corpus_identity(cards: Sequence[CardDoc]) -> dict[str, Any]:
    members = sorted(
        (
            Path(c.path).stem,
            hashlib.sha256((c.body.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n").encode()).hexdigest(),
        )
        for c in cards
    )
    encoded = json.dumps(members, ensure_ascii=False, separators=(",", ":")).encode()
    return {"live_count": len(members), "digest": hashlib.sha256(encoded).hexdigest()}


def wait_switching(client: MemoryHubClient, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while True:
        status = client.call("status").get("retrieval", {})
        if status.get("state") != "switching":
            return
        if time.monotonic() >= deadline:
            raise ValueError("disposable hub embedding coverage timed out while switching")
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


@contextmanager
def hub_client(options: HubOptions, cards: Sequence[CardDoc]) -> Iterator[MemoryHubClient | None]:
    if options.url:
        if options.memory_config or options.binary:
            raise ValueError("external hub options cannot start or reconfigure a hub")
        if options.token_file is None:
            raise ValueError("--hub-url requires --hub-token-file")
        client = MemoryHubClient(options.url, options.token_file.read_text(encoding="utf-8").strip())
        # Bind fixture identity to actual remote heads without mutating the hub.
        for card in cards:
            reply = client.call("card", scope=options.scope, card_id=Path(card.path).stem)
            expected = hashlib.sha256(
                (card.body.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n").encode()
            ).hexdigest()
            if reply["card"]["content_hash"] != expected:
                raise ValueError("hub fixture content differs from the selected fixture")
        yield client
        return
    binary = options.binary or Path(
        os.environ.get("BRIGADE_HUB_BINARY", repo_root() / "engines/hub/target/debug/brigade-hub")
    )
    resolved = shutil.which(str(binary))
    if resolved is None:
        yield None
        return
    # Reuse the managed-front fixture launcher, including its isolated child
    # environment, readiness deadline, port retries and process-group cleanup.
    sys.path.insert(0, str(repo_root() / "registry/skills/verify-brigade"))
    import hub_contract as hc  # type: ignore[import-not-found]

    with tempfile.TemporaryDirectory(prefix="brigade-retrieval-eval-") as directory:
        extra = ""
        if options.memory_config:
            extra = " --memory-config " + shlex.quote(str(options.memory_config.resolve()))
        template = (
            shlex.quote(resolved)
            + " --host {host} --port {port} --db {db} --token-file {token_file} --route-mode registration=native --route-mode memory=native"
            + extra
        )
        hub = hc.launch_hub(
            template, brigade=[sys.executable, "-m", "brigade"], state_dir=Path(directory), startup_timeout=30
        )
        try:
            client = MemoryHubClient(hub.url, hub.admin_token)
            seq = 0
            for card in cards:
                reply = client.call(
                    "put",
                    scope=options.scope,
                    card_id=Path(card.path).stem,
                    text=card.body,
                    client_event_id=event_id(),
                )
                seq = reply["event"]["seq"]
            # min_seq waits for the lexical index. Hybrid coverage is checked
            # by each adapter call, so a switching hub cannot pass R1.
            if cards:
                client.call("search", query="fixture readiness", mode="lexical", scopes=[options.scope], min_seq=seq)
            if options.memory_config:
                wait_switching(client)
            yield client
        finally:
            hc.stop_hub(hub)
