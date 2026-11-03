# Brigade v2 embedder benchmark (S16)

Results for slice S16 of the Brigade v2 build plan: the embedder benchmark
and the default it sets. The design, its "Embedder benchmark plan" and the
decision rule are in #2211 (`docs/phase-v2-hub-architecture.md` on that
branch). Everything here was measured on 2026-11-03, and this page changes
no code.

## Result

`remote` is the default embedder backend. `hub-cpu` stays available as an
opt-in backend.

`hub-cpu` fails two of the three conditions in the decision rule with every
model tested. The pending load test (S6) therefore cannot make it the
default:

| Condition | Needed | Best `hub-cpu` result | Pass |
|---|---|---|---|
| Query p95 inside the allotment | at most 150 ms | nomic-embed-text: 159 to 181 ms at 16 tokens, 452 to 458 ms at 64 tokens | no |
| Resident memory | at least 30% of 512 MB free (at most 358 MB resident) | nomic-embed-text: 454 MB, 11% free. qwen3-embedding 0.6B and 8B are killed by the cgroup OOM killer while loading | no |
| Concurrent S6 load test | at least 4,600 writes/s | pending S6 | not run |

The default is provisional only because S6 has not run. S6 can confirm
`remote` or reject it, but no S6 result moves the default to `hub-cpu`.
These would reopen the decision:

- A larger hub allotment with more cores as well as more memory. With 2 GB,
  qwen3 0.6B loads and holds about 1.25 GB resident, but on one vCPU it
  needs 0.65 CPU seconds per 16-token query (p95 713 to 749 ms), so more
  memory alone does not bring it under 150 ms.
- A smaller or differently packaged model, for example a 4-bit
  quantization or an ONNX runtime instead of Ollama, that stays under
  358 MB resident and embeds a 64-token query in under 150 ms on one core.
  None was measured here.
- S6 showing that the remote query path costs the hub write throughput. On
  the hub side a remote query is one HTTP call, which took 0.6 ms of client
  CPU here, so this is unlikely. S6 is what checks it.

Quality points the same way. Only the 8B model passed both private halves
of R1, and the 8B model runs only on `remote` (see [Quality](#quality)).

## Method

### Hosts and isolation

- One non-production machine with an Intel Core Ultra 9 285 (8 performance
  and 16 efficiency cores), the same CPU model as the hub host. Nothing ran
  on the hub host and nothing wrote to a live hub.
- Other jobs kept the machine busy the whole time: load average 120 to 175
  on 24 cores and CPU pressure (`/proc/pressure/cpu`, some avg60) near 70%.
  To keep that out of the numbers, every embedding server and every
  benchmark client ran in its own systemd user scope with
  `CPUWeight=10000`, pinned with `taskset` to its own core. Latency is wall
  time measured by a client on a different core from the server it calls.
  A first attempt with an unpinned client showed p95 597 ms where the
  server used 0.12 CPU seconds per query, so those runs were discarded.
- `hub-cpu` emulation: a separate CPU-only Ollama 0.18.0 instance per run on
  its own loopback port, started as
  `systemd-run --user --scope -p CPUQuota=100% -p MemoryMax=512M -p MemorySwapMax=256M -p CPUWeight=10000 -- taskset -c <core> ollama serve`
  with `CUDA_VISIBLE_DEVICES` empty and `OLLAMA_NOPRUNE=1`. Requests set
  `num_gpu: 0` and `num_thread: 1`. The cgroup files were read back after
  every run (`memory.max` 536870912, `memory.swap.max` 268435456, `cpu.max`
  "100000 100000"), so all three limits were enforced and cgroup delegation
  blocked nothing. A one-core pin plus a 100% quota is what a one-core
  container gets. Main runs used a performance core. nomic also ran on an
  efficiency core, because the hub's vCPU can land on either.
- `remote`: the machine's existing Ollama service with its GPU (NVIDIA RTX
  2000 Ada, 16 GB), over loopback and never restarted. A production hub
  would reach the GPU host over the private network and add that round
  trip.
- The 512 MB cap was given to the embedding server alone. A real `hub-cpu`
  deployment also fits the hub process inside the same allotment, so these
  numbers are a lower bound on its cost.

### Data

- A copy of the frozen corpus snapshot: 1,891 cards. All work ran on the
  copy, which was deleted afterwards.
- Queries: 120 distinct texts per length, cut from card text and
  calibrated with each model's own tokenizer to within 10% of 16, 64 and
  256 tokens (means 15.9, 63.7 to 63.8, and 252.3 to 253.3). The models'
  query prefixes add 18 tokens for qwen3 and 5 for nomic on top. Hub
  queries are capped at 256 characters, so 16 and 64 tokens are the
  realistic cases and 256 tokens is a stress case.
- Quality: the existing private retrieval harness, pointed at the corpus
  copy, with 62 to 64 judged real prompts under three independent judge
  label sets and 240 synthetic queries (120 paraphrase, 120 keyword).
  Aggregate numbers only.

### What a run measured

Each configuration ran at least twice, each time from a freshly started
server.

- Cold start: for `hub-cpu`, scope launch to the first query vector. For
  `remote`, unload with `keep_alive: 0`, then time the first query vector,
  which is a model load on a running server.
- Query latency: 5 warm-up requests, then 120 timed single-text requests
  per length. Percentiles are over those 120.
- CPU seconds per query: the server cgroup's `cpu.stat` `usage_usec` delta
  over the 120 requests. For `remote` this is the GPU host's Ollama
  process. The `remote` runs also counted embed requests from other
  clients in the service log, so contention by other users of that service
  is visible.
- Document throughput: `hub-cpu` embedded a fixed random sample of cards
  (seed 20261103: 60 cards for nomic, 20 for the nomic `num_ctx` 2048
  variant, 40 for qwen3 0.6B) in batches of 8. `remote` embedded all 1,891
  cards in batches of 32. Cards are cut at 6,000 characters (2,500 for
  nomic), as the existing harness does.
- Memory: cgroup `memory.current`, `memory.peak`, `memory.swap.peak` and
  `memory.events`, plus `VmRSS` of the server and runner processes.
  Resident memory below means the RSS sum, which leaves out reclaimable page
  cache.

## Results

### Fit inside 512 MB

| Model | Ollama's own estimate | Runs | Outcome |
|---|---|---|---|
| qwen3-embedding 0.6B (Q8_0) | 761 MiB weights, 448 MiB KV cache, 140 MiB compute graph: 1.3 GiB | 2 | OOM-killed while loading |
| qwen3-embedding 0.6B, `num_ctx` 512 | 761 MiB weights, 56 MiB KV, 28 MiB graph: 845 MiB | 2 | OOM-killed while loading. The weights alone are 149% of the allotment |
| qwen3-embedding 8B (Q4_K_M) | 4.8 GiB weights: 5.7 GiB | 2 | OOM-killed while loading |
| nomic-embed-text v1.5 (F16) | 306 MiB weights, 234 MiB graph: 540 MiB | 2 | loads. 454 and 455 MB resident, 11% free |
| nomic-embed-text, `num_ctx` 2048 | same estimate | 2 | loads. 450 MB resident, 12% free |
| nomic-embed-text, efficiency core | same estimate | 2 | loads. 454 MB resident, 11% free |

Swap did not help: `memory.swap.peak` stayed under 2 MB in every run,
including the OOM-killed ones. Ollama 0.18 ignored `use_mmap: true` and
loaded the weights into anonymous memory each time. nomic runs reached the
512 MB `memory.max` during load in 2 of 6 runs and recovered by reclaiming
page cache.

### Query latency

Milliseconds, run 1 / run 2. The `hub-cpu` rows run inside the allotment.
The `remote` nomic rows are runs 3 and 4, which also counted requests from
other clients. Runs 1 and 2 gave the same shape.

| Backend | Model | Tokens | p50 | p95 | p99 | CPU s per query |
|---|---|---:|---|---|---|---|
| hub-cpu, P-core | nomic | 16 | 136 / 128 | 159 / 181 | 448 / 506 | 0.134 / 0.120 |
| hub-cpu, P-core | nomic | 64 | 418 / 403 | 458 / 452 | 537 / 466 | 0.411 / 0.385 |
| hub-cpu, P-core | nomic | 256 | 1,617 / 1,597 | 1,808 / 1,784 | 2,010 / 2,420 | 1.592 / 1.591 |
| hub-cpu, P-core, `num_ctx` 2048 | nomic | 16 | 130 / 110 | 142 / 168 | 156 / 1,161 | 0.126 / 0.108 |
| hub-cpu, P-core, `num_ctx` 2048 | nomic | 64 | 388 / 412 | 449 / 559 | 460 / 742 | 0.379 / 0.396 |
| hub-cpu, P-core, `num_ctx` 2048 | nomic | 256 | 1,544 / 1,432 | 1,769 / 1,655 | 2,407 / 1,705 | 1.474 / 1.441 |
| hub-cpu, E-core | nomic | 16 | 213 / 166 | 234 / 199 | 343 / 201 | 0.209 / 0.169 |
| hub-cpu, E-core | nomic | 64 | 631 / 540 | 784 / 607 | 1,013 / 624 | 0.613 / 0.541 |
| hub-cpu, E-core | nomic | 256 | 2,385 / 2,386 | 2,741 / 2,698 | 2,798 / 2,760 | 2.351 / 2.364 |
| remote, GPU | nomic | 16 | 67 / 63 | 457 / 415 | 652 / 665 | 1.009 / 1.105 |
| remote, GPU | nomic | 64 | 18 / 18 | 25 / 24 | 32 / 27 | 0.020 / 0.020 |
| remote, GPU | nomic | 256 | 22 / 22 | 32 / 31 | 596 / 47 | 0.024 / 0.024 |
| remote, GPU | qwen3 0.6B | 16 | 90 / 93 | 99 / 105 | 105 / 111 | 0.100 / 0.104 |
| remote, GPU | qwen3 0.6B | 64 | 91 / 95 | 104 / 110 | 109 / 112 | 0.103 / 0.105 |
| remote, GPU | qwen3 0.6B | 256 | 112 / 110 | 126 / 124 | 132 / 132 | 0.124 / 0.121 |
| remote, GPU | qwen3 8B | 16 | 124 / 126 | 141 / 142 | 146 / 147 | 0.133 / 0.136 |
| remote, GPU | qwen3 8B | 64 | 141 / 144 | 153 / 159 | 156 / 164 | 0.150 / 0.153 |
| remote, GPU | qwen3 8B | 256 | 265 / 266 | 289 / 291 | 297 / 316 | 0.269 / 0.269 |

On `hub-cpu`, latency tracks CPU time: one core spends about 6 ms per
nomic token, and the 16-token p99 tail comes and goes between runs.

On `remote`, nomic at 16 tokens took about 1 CPU second per query and had
a p95 above 400 ms, while 64 and 256 tokens took 18 to 22 ms at p50. Four
runs gave the same shape, including the two shown, during which the service
log showed only 3 to 5 embed requests from other clients. This is
consistent with the GPU backend computing very short inputs (here 21 tokens
with the prefix) on the CPU. The qwen3 prefix keeps its 16-token queries at
34 tokens, and they show no such step. The qwen3 0.6B numbers on the GPU
are flat from 16 to 64 tokens, so a fixed per-request cost of about 90 ms
dominates them on this busy host.

### qwen3 0.6B on one vCPU

qwen3 0.6B cannot load inside 512 MB, so it also ran with the same one-core
pin and 100% quota but with `MemoryMax=2G` and `num_ctx` 2048, to show what
more memory alone would buy:

| Tokens | p50 ms | p95 ms | p99 ms | CPU s per query |
|---:|---|---|---|---|
| 16 | 659 / 661 | 749 / 713 | 821 / 919 | 0.652 / 0.659 |
| 64 | 1,435 / 1,418 | 1,740 / 1,530 | 2,071 / 1,588 | 1.416 / 1.417 |
| 256 | 4,928 / 4,788 | 5,464 / 5,424 | 5,639 / 5,535 | 4.754 / 4.614 |

Resident memory was 1,273 and 1,232 MB, with a cgroup peak of 1,212 and
1,336 MB. On one core, the 300 ms query budget in the design fails on every
query.

### Cold start, throughput and memory

Run 1 / run 2.

| Backend | Model | Cold start s | Cards/s | Full 1,891-card backfill | Memory |
|---|---|---|---|---|---|
| hub-cpu, P-core | nomic | 2.51 / 0.89 | 0.37 / 0.40 (2.7 / 2.5 CPU s per card) | 80 to 85 min | 454 / 455 MB resident of 512 MB |
| hub-cpu, P-core, `num_ctx` 2048 | nomic | 0.80 / 1.42 | 0.37 / 0.41 | 77 to 85 min | 450 / 450 MB |
| hub-cpu, E-core | nomic | 0.70 / 1.50 | 0.27 / 0.25 (3.7 / 4.0 CPU s per card) | 2.0 to 2.1 h | 454 / 454 MB |
| hub-cpu, P-core, 2 GB cap | qwen3 0.6B | 2.67 / 6.50 | 0.11 / 0.10 (9.1 / 9.7 CPU s per card) | 4.8 to 5.1 h | 1,273 / 1,232 MB |
| remote, GPU | nomic | 1.44 / 0.84 | 68.8 / 69.6 | 27 s | 568 MB VRAM. None on the hub |
| remote, GPU | qwen3 0.6B | 1.13 / 1.07 | 17.3 / 17.3 | 1.8 min | 1,427 MB VRAM, about 790 MB host RSS on the GPU host. None on the hub |
| remote, GPU | qwen3 8B | 2.44 / 6.39 | 2.76 / 2.70 | 11.4 to 11.7 min | 5,954 MB VRAM, about 1.15 GB host RSS on the GPU host. None on the hub |

For `hub-cpu`, cold start includes server start (0.25 to 0.77 s), and the
spread between runs comes from whether the model file was already in the
page cache. `hub-cpu` document embedding also takes the hub's only core
for the whole backfill. During that time a query waits behind a batch, or
the scheduler has to interleave them.

CPU and GPU vectors agree: for every `hub-cpu` document sample, cosine
similarity to the GPU vector of the same card was at least 0.99999 for
nomic and at least 0.99952 for qwen3 0.6B. In all 360 sampled card vectors,
the nearest card in the GPU matrix was the card itself. Quality does not depend
on the backend.

### Embedding server stopped

The hub code does not exist yet (S15 and S17), so a small harness stood in
for the planned query path. It loads the corpus copy, builds the BM25 index
with `brigade.memory_bm25` (the tokenizer the hub must match) and the nomic
document matrix, and embeds each query within the design's 300 ms budget.
On any embedding error it answers from BM25 alone and records why, as the
design's `degraded` field does. The harness ran in its own scope with the
hub allotment (one core, 512 MB) and called a `hub-cpu` nomic server on a
different core. Each scenario sent 100 synthetic queries, in two runs:

- up: server running.
- hung: server frozen with `SIGSTOP`, so connections open and nothing
  answers.
- stopped: server scope stopped, so connections are refused.
- with breaker: after one embedding failure, skip the embedder for 30
  seconds.

| Scenario | Answered | Mode | p50 ms | p95 ms | p99 ms | max ms |
|---|---|---|---|---|---|---|
| up | 100 / 100 | hybrid 97 / 99, lexical 3 / 1 after an embed timeout | 205 / 198 | 276 / 263 | 470 / 445 | 478 / 505 |
| hung | 100 / 100 | lexical, `embedder-timeout` | 302 / 303 | 305 / 307 | 325 / 325 | 348 / 339 |
| hung, with breaker | 100 / 100 | lexical | 0.7 / 0.7 | 2.5 / 2.5 | 40 / 50 | 325 / 326 |
| stopped | 100 / 100 | lexical, `embedder-unavailable` | 1.0 / 0.9 | 2.8 / 2.7 | 25 / 25 | 38 / 36 |
| stopped, with breaker | 100 / 100 | lexical | 0.8 / 0.8 | 2.4 / 2.5 | 24 / 25 | 41 / 40 |

Every query got an answer in every scenario. With the server stopped, the
lexical answer meets the design's 20 ms fallback budget at p95. The
slowest 1 or 2 queries took 25 to 41 ms in this pure-Python harness. A hung
server costs the full 300 ms embed budget on every query until the hub
stops calling it. With the breaker, only the first query paid it, so S15
should include a breaker. Even with the server running, `hub-cpu` nomic
missed the 300 ms budget on 1 to 3% of synthetic queries.

### Quality

R1-style numbers from the existing harness on the corpus copy. Real
prompts: hit@5 / MRR@20 under each judge's labels (62, 62 and 64 prompts
with at least one relevant card). Synthetic: R@5 over 240 queries. Query
vectors came from `remote`. A second pass of the nomic rows with query
vectors from `hub-cpu` inside the allotment gave the same hybrid numbers
on all four sets.

| Method | Judge A | Judge B | Judge C | Synthetic all | Paraphrase | Keyword |
|---|---|---|---|---|---|---|
| BM25 | 0.661 / 0.513 | 0.742 / 0.578 | 0.703 / 0.525 | 0.812 | 0.658 | 0.967 |
| dense nomic | 0.758 / 0.624 | 0.774 / 0.670 | 0.766 / 0.622 | 0.450 | 0.467 | 0.433 |
| hybrid nomic | 0.823 / 0.673 | 0.871 / 0.751 | 0.844 / 0.678 | 0.725 | 0.642 | 0.808 |
| dense qwen3 0.6B | 0.871 / 0.691 | 0.871 / 0.716 | 0.844 / 0.645 | 0.704 | 0.750 | 0.658 |
| hybrid qwen3 0.6B | 0.903 / 0.718 | 0.935 / 0.765 | 0.828 / 0.713 | 0.829 | 0.767 | 0.892 |
| dense qwen3 8B | 0.935 / 0.818 | 0.903 / 0.859 | 0.922 / 0.771 | 0.817 | 0.883 | 0.750 |
| hybrid qwen3 8B | 0.919 / 0.780 | 0.968 / 0.872 | 0.906 / 0.755 | 0.854 | 0.775 | 0.933 |

Against the private halves of R1 (hit@5 up at least 0.10 over BM25 under
at least two of three judges, MRR not lower, and keyword R@5 at most 0.05
below BM25):

| Hybrid model | hit@5 gain over BM25 | MRR | Keyword R@5 vs BM25 | Private R1 |
|---|---|---|---|---|
| nomic | +0.16, +0.13, +0.14 | up under all three | -0.159 | fails on keyword |
| qwen3 0.6B | +0.24, +0.19, +0.13 | up under all three | -0.075 | fails on keyword |
| qwen3 8B | +0.26, +0.23, +0.20 | up under all three | -0.034 | passes |

These are untuned RRF weights (1.0 each). S17 tunes them and runs the
public-fixture half of R1.

## Not measured

- Concurrent hub load test: **pending S6**. S6 adds the load harness in
  `engines/hub/bench/`. To rerun this part, start the Rust hub and a
  `hub-cpu` embedding server together inside one allotment scope (the
  `systemd-run` line above, with both processes in it). Run the S6
  single-event `POST /events` load at 16 to 64 clients with one connection
  per request. In parallel, send hybrid `memory search` queries at a fixed
  rate (for example 2 per second) from another core. Record writes/s and
  write p99 with and without the query stream, then repeat with the
  embedding server on the GPU host (`remote`). The rule needs at least
  4,600 writes/s with embedding running.
- `remote` over the real private network. Everything here used loopback.
- Other runtimes and quantizations, such as 4-bit GGUF, ONNX or a
  llama.cpp server. Only the three models already installed were tested.
- The public-fixture half of R1, and R0. Both need the `hub-hybrid` and
  `hub-lexical` eval adapters from S17.
- qwen3 0.6B and 8B on an efficiency core, and the 8B model on one vCPU
  outside the allotment.
- A first-result (top-1) comparison, which belongs to R2 and S21.
